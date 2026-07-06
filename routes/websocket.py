"""
WebSocket事件处理 - 用于柜体屏幕端实时开锁通信
"""
import logging
import json
from datetime import datetime
from flask import request
from helpers import connected_devices, pending_lock_commands, logger


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

    @socketio.on('force_update_ack', namespace='/')
    def ws_force_update_ack(data):
        """设备确认收到升级通知"""
        device_id = data.get('device_id', '')
        accepted = data.get('accepted', False)
        logger.info(f'[WebSocket] 设备升级确认: device_id={device_id}, accepted={accepted}')

    @socketio.on('lock_result', namespace='/')
    def ws_lock_result(data):
        """开锁结果上报"""
        device_id = data.get('device_id') or data.get('deviceId')
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
