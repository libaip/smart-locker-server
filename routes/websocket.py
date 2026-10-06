"""
WebSocket事件处理 - 用于柜体屏幕端实时开锁通信
"""
import logging
import json
from datetime import datetime
from flask import request
from helpers import connected_devices, pending_lock_commands, logger


# ============================================
# [S234-20260918] 设备"任何消息"都刷新心跳（修"假离线"）
# 在线判定 = last_heartbeat 在 2 分钟内；但走长连接的设备只在注册时刷过一次、
# 之后不再发 heartbeat 消息 -> 能收指令、能回传开锁结果，却被显示成离线。
# 这里在"任何设备消息"的入口刷新一次（30 秒节流防写爆），失败只告警。
# ============================================
_hb_touch_cache = {}


def _touch_heartbeat(device_id):
    if not device_id:
        return
    try:
        import time as _t
        _did = str(device_id)
        _now = _t.time()
        if _now - _hb_touch_cache.get(_did, 0) < 30:
            return
        _hb_touch_cache[_did] = _now
        from database import get_db
        _db = get_db()
        _db.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (_did,))
        _db.commit()
        _db.close()
    except Exception as _e:
        try:
            logger.warning(f'[WS] 刷新心跳失败: device={device_id}, {_e}')
        except Exception:
            pass


def register_websocket_handlers(socketio):
    """注册WebSocket事件处理器"""

    @socketio.on('connect', namespace='/')
    def ws_connect():
        logger.info(f'[WebSocket] 客户端连接: {request.sid}')

    @socketio.on('disconnect', namespace='/')
    def ws_disconnect():
        sid = request.sid
        device_id = None
        for did, s in list(connected_devices.items()):
            if s == sid:
                device_id = did
                del connected_devices[did]
                break
        logger.info(f'[WebSocket] 设备断开: device_id={device_id}, sid={sid}')

    @socketio.on('register', namespace='/')
    def ws_register(data):
        """设备注册"""
        device_id = data.get('device_id') or data.get('deviceId')
        app_version = data.get('version') or data.get('app_version') or data.get('Version', '')
        app_version_code = 0
        try:
            parts = app_version.split('.')
            if len(parts) == 3:
                app_version_code = int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])
        except:
            pass
        if device_id:
            connected_devices[device_id] = request.sid
            logger.info(f'[WebSocket] 设备注册: device_id={device_id}, version={app_version}')
            serial_port = 'ttyS4'
            baud_rate = 9600
            try:
                from database import get_db
                db = get_db()
                db.execute("UPDATE cabinets SET app_version=%s, app_version_code=%s, last_heartbeat=NOW() WHERE mainboard_device_id=%s",
                           (app_version, app_version_code, device_id))
                db.commit()
                cur = db.cursor()
                cur.execute(
                    "SELECT m.serial_port, m.baud_rate FROM cabinets c "
                    "JOIN mainboards m ON c.id = m.cabinet_id "
                    "WHERE c.mainboard_device_id = %s "
                    "ORDER BY m.board_index ASC LIMIT 1",
                    (device_id,))
                row = cur.fetchone()
                if row:
                    serial_port = row['serial_port']
                    baud_rate = row['baud_rate']
                db.close()
            except Exception as e:
                logger.error(f'[WebSocket] 保存版本信息失败: {e}')
            socketio.emit('register_ack', {
                'status': 'ok',
                'device_id': device_id,
                'deviceId': device_id,
                'serial_port': serial_port,
                'baud_rate': baud_rate
            }, room=request.sid, namespace='/')
            if device_id in pending_lock_commands and pending_lock_commands[device_id]:
                for cmd in pending_lock_commands[device_id]:
                    socketio.emit('open_lock', cmd, room=request.sid, namespace='/')
                    logger.info(f'[WebSocket] 发送积压开门指令: device_id={device_id}, lock={cmd.get("lock_no")}')
                pending_lock_commands[device_id] = []


    @socketio.on('heartbeat', namespace='/')
    def ws_heartbeat(data):
        """设备心跳"""
        device_id = data.get('device_id') or data.get('deviceId')
        if device_id:
            try:
                from database import get_db
                db = get_db()
                db.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
                db.commit()
                db.close()
            except Exception as e:
                logger.error(f'[WebSocket] 心跳更新失败: {e}')
            logger.debug(f'[WebSocket] 心跳: device_id={device_id}')
            # 自动检查版本并推送更新：版本以数据库 apk_version 和设备上报的 app_version_code 为准
            try:
                from database import get_db as _get_db
                _db = None
                _row_min = None
                try:
                    _db = _get_db()
                    _cur = _db.cursor()
                    _cur.execute("SELECT app_version_code FROM cabinets WHERE mainboard_device_id=%s", (device_id,))
                    _row = _cur.fetchone()
                    _cur.execute("SELECT version_code, version_name, download_url FROM apk_version ORDER BY version_code DESC LIMIT 1")
                    _apk = _cur.fetchone()
                    # [S827-20261007] 顺带取"自动升级最低版本门槛"
                    _cur.execute("SELECT setting_value FROM system_settings WHERE setting_key='apk_push_min_version_code'")
                    _row_min = _cur.fetchone()
                finally:
                    if _db is not None:
                        try:
                            _db.close()
                        except Exception:
                            pass
                if not _apk:
                    return
                app_ver_code = int(_row['app_version_code'] or 0) if _row else 0
                # [S827-20261007] 【最低版本门槛】只给 >= 门槛的设备自动推升级。
                #   老板 2026-10-07 要求"仅限 1.4.12 以上"(version_code 269)；
                #   低于门槛的老设备(1.4.7 / 1.2.46 / 版本未知)不动, 避免把老机器推挂。
                #   门槛读 system_settings.apk_push_min_version_code; 读不到/非数字/为 0
                #   -> 0 = 不限, 与改动前行为完全一致(零回归)。
                _min_code = 0
                try:
                    _mv = _row_min['setting_value'] if _row_min else None
                    if _mv is not None and str(_mv).strip().isdigit():
                        _min_code = int(str(_mv).strip())
                except Exception:
                    _min_code = 0
                if app_ver_code < _min_code:
                    logger.debug('[S827] 跳过自动升级(低于门槛): device=%s current=%s min=%s',
                                 device_id, app_ver_code, _min_code)
                    return
                if app_ver_code < int(_apk['version_code']):
                    import json as _json
                    version_info = {
                        'type': 'force_update',
                        'version_code': _apk['version_code'],
                        'version_name': _apk['version_name'],
                        'download_url': _apk['download_url'],
                        'force': True
                    }
                    socketio.emit('message', version_info, room=request.sid, namespace='/')
                    logger.info(f'[WebSocket] 自动推送更新: device_id={device_id}, current={app_ver_code}, target={_apk["version_code"]}')
            except Exception as e:
                logger.error(f'[WebSocket] 自动更新推送失败: {e}')

    @socketio.on('force_update_ack', namespace='/')
    def ws_force_update_ack(data):
        """设备确认收到升级通知"""
        device_id = data.get('device_id', '')
        _touch_heartbeat(device_id)      # [S234] 升级确认也算"活着"
        accepted = data.get('accepted', False)
        logger.info(f'[WebSocket] 设备升级确认: device_id={device_id}, accepted={accepted}')

    @socketio.on('lock_result', namespace='/')
    def ws_lock_result(data):
        """开锁结果上报"""
        device_id = data.get('device_id') or data.get('deviceId')
        _touch_heartbeat(device_id)      # [S234] 收到开锁结果也算"活着"
        order_id = data.get('order_id')
        success = data.get('success', False)
        logger.info(f'[WebSocket] 开锁结果: device_id={device_id}, order_id={order_id}, success={success}')
        if order_id and success:
            try:
                from database import get_db
                db = get_db()
                cursor = db.cursor()
                cursor.execute('SELECT slot_id FROM orders WHERE id = %s', (int(order_id),))
                order = cursor.fetchone()
                if order and order['slot_id']:
                    cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (order['slot_id'],))
                    db.commit()
                db.close()
            except Exception as e:
                logger.error(f'[WebSocket] 更新柜格状态失败: {e}')


def register_raw_websocket(app):
    """注册原始WebSocket端点 /ws/"""
    @app.route('/ws/', methods=['GET'])
    def ws_endpoint():
        if not request.environ.get('wsgi.websocket'):
            from flask import make_response
            return make_response('bad request', 400)
        ws = request.environ['wsgi.websocket']
        device_id = request.args.get('device_id', '')
        logger.info(f'[原始WS] 新连接: device_id={device_id}')
        if device_id:
            connected_devices[device_id] = ws
            ws.send(json.dumps({'type': 'register_ack', 'device_id': device_id, 'status': 'ok'}))
            if device_id in pending_lock_commands and pending_lock_commands[device_id]:
                for cmd in pending_lock_commands[device_id]:
                    ws.send(json.dumps({'type': 'open_lock', **cmd}))
                pending_lock_commands[device_id] = []
        else:
            ws.send(json.dumps({'type': 'error', 'message': '缺少device_id'}))
        try:
            while not ws.closed:
                msg = ws.receive()
                if msg is None:
                    break
                try:
                    data = json.loads(msg)
                    t = data.get('type', '')
                    _touch_heartbeat(data.get('device_id') or device_id)   # [S234] 任何消息都刷新心跳
                    if t == 'register':
                        did = data.get('device_id', '')
                        if did:
                            if device_id and device_id in connected_devices:
                                del connected_devices[device_id]
                            device_id = did
                            connected_devices[device_id] = ws
                            # Save version to DB
                            app_ver = data.get('version', '')
                            app_ver_code = data.get('version_code', 0)
                            if not app_ver_code:
                                try:
                                    parts = app_ver.split('.')
                                    if len(parts) == 3:
                                        app_ver_code = int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])
                                except:
                                    pass
                            try:
                                from database import get_db
                                db2 = get_db()
                                db2.execute('UPDATE cabinets SET app_version=%s, app_version_code=%s, last_heartbeat=datetime("now") WHERE mainboard_device_id=%s', (app_ver, app_ver_code, device_id))
                                db2.commit()
                                db2.close()
                                logger.info(f'[原始WS] 设备注册更新版本: device_id={device_id}, version={app_ver}, version_code={app_ver_code}')
                            except Exception as db_ex:
                                logger.error(f'[原始WS] 版本写入DB失败: {db_ex}')
                            ws.send(json.dumps({'type': 'register_ack', 'device_id': device_id, 'status': 'ok'}))
                    elif t == 'lock_result':
                        sid_val = data.get('order_id')
                        if sid_val and data.get('success'):
                            from database import get_db
                            db = get_db()
                            cur = db.cursor()
                            cur.execute('SELECT slot_id FROM orders WHERE id=%s', (int(sid_val),))
                            o = cur.fetchone()
                            if o and o['slot_id']:
                                cur.execute('UPDATE cabinet_slots SET status=2 WHERE id=%s', (o['slot_id'],))
                                db.commit()
                            db.close()
                    elif t == 'heartbeat':
                        from database import get_db
                        db = get_db()
                        db.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
                        db.commit()
                        db.close()
                except:
                    pass
        except Exception as e:
            logger.error(f'[原始WS] 异常: device_id={device_id}, {e}')
        finally:
            if device_id and device_id in connected_devices:
                del connected_devices[device_id]
            logger.info(f'[原始WS] 断开: device_id={device_id}')
        from flask import make_response
        return make_response('', 200)


def send_raw_open_lock(device_id, board_no, lock_no, protocol=None, order_id=''):
    cmd = {
        'type': 'open_lock',
        'device_id': device_id,
        'board_no': board_no,
        'lock_no': lock_no,
        'protocol': protocol,
        'order_id': order_id,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    if device_id in connected_devices:
        ws = connected_devices[device_id]
        try:
            if hasattr(ws, 'send') and not getattr(ws, 'closed', True):
                ws.send(json.dumps(cmd))
                return True
        except:
            pass
    if device_id not in pending_lock_commands:
        pending_lock_commands[device_id] = []
    pending_lock_commands[device_id].append(cmd)
    return False
