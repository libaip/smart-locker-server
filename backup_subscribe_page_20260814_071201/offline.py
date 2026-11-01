"""
离线取包API - Blueprint
支持断网时APK本地验证，网络恢复后同步到服务器
"""
import logging
import json
from datetime import datetime
from flask import Blueprint, request, session
from database import get_db
from helpers import json_response, logger, pending_lock_commands, connected_devices, require_auth, \
    find_user_balance_row, upsert_user_balance_row, phone_openid_rows, \
    get_mid_retrieve_config, try_increment_mid_retrieve

def _return_balance_to_user(cursor, order_dict):
    """离线取包/APK取件时退还保证金到用户余额 - 统一用 mp_openid"""
    deposit_amount = order_dict.get('deposit_amount', 0)
    if deposit_amount <= 0:
        return (0, order_dict.get('openid', '') or '')
    user_phone = order_dict.get('user_phone', '')
    if not user_phone:
        return
    _openid = order_dict.get('openid', '') or ''
    _unionid = order_dict.get('unionid', '') or ''
    _mp_openid = order_dict.get('mp_openid', '') or _openid
    # 统一用 mp_openid 查找用户余额
    if not _mp_openid and user_phone:
        _po_rows = phone_openid_rows(cursor, phone=user_phone, unionid=_unionid)
        if len(_po_rows) == 1 and _po_rows[0].get('mp_openid'):
            _mp_openid = _po_rows[0]['mp_openid']
        else:
            _ub_r = find_user_balance_row(cursor, phone=user_phone, unionid=_unionid)
            if _ub_r and _ub_r.get('mp_openid'):
                _mp_openid = _ub_r['mp_openid']
    upsert_user_balance_row(cursor, phone=user_phone, openid=_openid, unionid=_unionid,
                            mp_openid=_mp_openid, balance=deposit_amount, total_deposited=deposit_amount,
                            user_id=order_dict.get('user_id') or 0)
    # 写入余额明细
    cursor.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING",
                   (user_phone, order_dict['id'], deposit_amount))
    cursor.execute('SELECT 1')
    cursor.fetchall()
    return (deposit_amount, _mp_openid or _openid)
    # 更新订单退款标记
    cursor.execute('UPDATE orders SET refund_amount = %s, refund_mark = 1 WHERE id = %s', (deposit_amount, order_dict['id']))


bp = Blueprint('offline', __name__)


@bp.route('/lock-result', methods=['POST'])
def report_lock_result():
    """HTTP开锁结果上报"""
    try:
        data = request.get_json()
        device_id = data.get('device_id')
        order_id = data.get('order_id')
        success = data.get('success', False)
        logger.info(f'[HTTP上报] 开锁结果: device_id={device_id}, order_id={order_id}, success={success}')
        if order_id and success:
            conn = get_db()
            cursor = conn.cursor()
            try:
                oid = int(order_id)
                if oid > 2147483647 or oid < -2147483648:
                    oid = None
            except (ValueError, TypeError):
                oid = None
            if oid:
                cursor.execute('SELECT o.slot_id FROM orders o WHERE o.id = %s', (oid,))
            else:
                cursor.execute('SELECT o.slot_id FROM orders o WHERE o.order_no = %s', (str(order_id),))
            order = cursor.fetchone()
            if order and order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
                conn.commit()
            conn.close()
        return json_response({'message': '结果已记录'})
    except Exception as e:
        logger.error(f'[lock_result] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/pending-commands/<device_id>', methods=['GET'])
def get_pending_commands(device_id):
    """获取待处理的离线指令"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 更新设备心跳（HTTP轮询也视为在线）
        cursor.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
        conn.commit()
        valid_commands = []

        cursor.execute('SELECT * FROM pending_lock_cmds WHERE device_id=%s AND delivered=0 ORDER BY id', (device_id,))
        logger.info(f"[DEBUG_POLL] device={device_id} querying pending cmds")
        for row in cursor.fetchall():
            command_text = row['command'] if 'command' in row and row['command'] else ''
            skip = False
            if row.get('order_id'):
                _check = conn.cursor()
                _check.execute("SELECT id, status, slot_id FROM orders WHERE id::text=%s OR order_no=%s", (row['order_id'], row['order_id']))
                _ord = _check.fetchone()
                if not _ord or _ord['status'] != 2:
                    skip = True
                elif row.get('slot_id'):
                    _check.execute("SELECT order_no FROM orders WHERE slot_id=%s AND status=2 ORDER BY id DESC LIMIT 1", (row['slot_id'],))
                    _cur_ord = _check.fetchone()
                    if not _cur_ord or str(_cur_ord['order_no']) != str(row['order_id']):
                        skip = True
                if skip:
                    cursor.execute("UPDATE pending_lock_cmds SET delivered=1, status='skipped_order_ended' WHERE id=%s", (row['id'],))
                    continue
            # 指令超过5分钟作废，防止设备恢复联网后执行旧开门指令
            if not skip and row.get('created_at'):
                try:
                    _created = row['created_at']
                    if isinstance(_created, str):
                        from datetime import datetime as _dt
                        _created = _dt.fromisoformat(str(_created))
                    if (datetime.now() - _created).total_seconds() > 86400:
                        skip = True
                        cursor.execute("UPDATE pending_lock_cmds SET delivered=1, status='skipped_expired' WHERE id=%s", (row['id'],))
                        continue
                except Exception:
                    pass
            if command_text:
                try:
                    cmd = json.loads(command_text)
                    valid_commands.append(cmd)
                except:
                    valid_commands.append({'order_id': str(row['order_id']) if row['order_id'] else '', 'board_no': row['board_no'], 'lock_no': row['lock_no'], 'action': 'open', 'protocol': row['protocol'], 'timestamp': row['created_at']})
            else:
                valid_commands.append({'order_id': str(row['order_id']) if row['order_id'] else '', 'board_no': row['board_no'], 'lock_no': row['lock_no'], 'action': 'open', 'protocol': row['protocol'], 'timestamp': row['created_at']})
            cursor.execute("UPDATE pending_lock_cmds SET delivered=1, status='completed' WHERE id=%s", (row['id'],))
            logger.info(f'[DEBUG_POLL] device={device_id} delivered cmd id={row["id"]} command={command_text[:100]}')
            if len(valid_commands) >= 10:
                break


        now = datetime.now()
        cursor.execute("SELECT o.id as order_id, o.order_no, o.user_phone, o.access_code, o.compartment_number, o.cabinet_id, cs.board_no, cs.lock_no FROM orders o JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE c.mainboard_device_id = %s AND o.status = 2 ORDER BY o.id DESC", (device_id,))
        orders = [dict(row) for row in cursor.fetchall()]
        conn.commit()
        conn.close()


        # 查询该设备柜体下所有主板配置，随轮询返回给APK自动同步
        mainboard_config = []
        try:
            cursor.execute('SELECT c.id as cabinet_id FROM cabinets c WHERE c.mainboard_device_id=%s', (device_id,))
            _cab = cursor.fetchone()
            if _cab:
                cursor.execute('SELECT board_index, serial_port, baud_rate, protocol FROM mainboards WHERE cabinet_id=%s ORDER BY board_index', (_cab['cabinet_id'],))
                for _mb in cursor.fetchall():
                    mainboard_config.append({
                        'board_index': _mb['board_index'],
                        'serial_port': _mb['serial_port'],
                        'baud_rate': _mb['baud_rate'],
                        'protocol': _mb['protocol'] or 'YBM'
                    })
        except Exception as _e:
            logger.warning(f'[pending_commands] 查询主板配置失败(不影响正常功能): {_e}')

        return json_response({"commands": valid_commands, "orders": orders, "server_time": now.strftime("%Y-%m-%d %H:%M:%S"), "mainboard_config": mainboard_config})
    except Exception as e:
        logger.error(f'[pending_commands] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/active-orders/by-device/<device_id>', methods=['GET'])
def get_active_orders_by_device(device_id):
    """获取设备的活动订单"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 更新设备心跳
        cursor.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
        conn.commit()
        cursor.execute("SELECT o.id as order_id, o.order_no, o.user_phone, o.access_code, o.compartment_number, o.cabinet_id, cs.board_no, cs.lock_no FROM orders o JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE c.mainboard_device_id = %s AND o.status = 2 ORDER BY o.id DESC", (device_id,))
        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return json_response({'orders': orders, 'count': len(orders), 'device_id': device_id,
                              'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    except Exception as e:
        logger.error(f'[active_orders] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/offline-retrieve', methods=['POST'])
def offline_retrieve():
    """离线取包同步（单条）"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        order_no = data.get('order_no')
        user_phone = data.get('user_phone')
        retrieve_time = data.get('retrieve_time')
        if not order_id and not order_no:
            return json_response(message='缺少订单信息', code=400)
        conn = get_db()
        cursor = conn.cursor()
        if order_id:
            cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        else:
            cursor.execute('SELECT * FROM orders WHERE order_no = %s', (order_no,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if order['status'] != 2:
            conn.close()
            return json_response(message=f'订单已处理（当前状态: {order["status"]}）', code=400)
        if user_phone and order['user_phone'] != user_phone:
            conn.close()
            return json_response(message='手机号不匹配', code=403)
        # 5分钟保护
        if order.get('created_at') and (datetime.now() - order['created_at']).total_seconds() < 300:
            conn.close()
            return json_response(message='订单刚创建，请稍后再试', code=400)
        actual_time = retrieve_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('UPDATE orders SET status = 3, retrieve_time = %s WHERE id = %s AND status = 2', (actual_time, order['id']))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        # 退还保证金到用户余额
        _return_balance_to_user(cursor, dict(order))
        conn.commit()
        conn.close()

        # 发送结束通知和退款通知
        try:
            _notify_openid = order.get('openid', '') or ''
            if not _notify_openid:
                try:
                    _nc = get_db()
                    _ncur = _nc.cursor()
                    _ncur.execute('SELECT COALESCE(mp_openid, openid) as openid FROM users WHERE phone = %s ORDER BY updated_at DESC LIMIT 1', (order['user_phone'],))
                    _nr = _ncur.fetchone()
                    if _nr:
                        _notify_openid = _nr['openid'] or ''
                    _ncur.close()
                    _nc.close()
                except:
                    pass
            if _notify_openid:
                from helpers import send_wx_subscribe_message
                send_wx_subscribe_message(_notify_openid, 'UT0PehBf71OaahgZbqFfLPQt55BWc7tSz4D4NqCPDhE', {
                    "thing1": {"value": str(order.get("compartment_number", "")) + "号柜门"},
                    "time3": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")}
                })
                logger.info(f'[offline_retrieve] 结束通知已发送: order={order["id"]}')
                _dep = order.get('deposit_amount', 0)
                if _dep > 0:
                    send_wx_subscribe_message(_notify_openid, 'nG8Cdhn-Nym9ml4LatE9CdGXoJyyoi227vNzLMX9i8w', {
                        "amount2": {"value": str(_dep) + "元"},
                        "thing4": {"value": "押金已退至余额"},
                        "time5": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")}
                    })
                    logger.info('[offline_retrieve] 退款通知已发送')
        except Exception as ne:
            logger.error(f'[offline_retrieve发送通知失败] {ne}')
        return json_response({'message': '\u53d6\u5305\u8bb0\u5f55\u5df2\u540c\u6b65', 'order_id': order['id'], 'order_no': order['order_no'],
                              'status': 3, 'retrieve_time': actual_time})
    except Exception as e:
        logger.error(f'[offline_retrieve] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/offline-retrieve/batch', methods=['POST'])
@require_auth
def offline_retrieve_batch():
    """离线取包批量同步"""
    try:
        data = request.get_json()
        records = data.get('records', [])
        device_id = data.get('device_id')
        if not records:
            return json_response(message='无记录需要同步', code=400)
        conn = get_db()
        cursor = conn.cursor()
        results = []
        success_count = 0
        for rec in records:
            oid = rec.get('order_id')
            ono = rec.get('order_no')
            try:
                if oid:
                    cursor.execute('SELECT * FROM orders WHERE id = %s', (oid,))
                else:
                    cursor.execute('SELECT * FROM orders WHERE order_no = %s', (ono,))
                order = cursor.fetchone()
                if not order:
                    results.append({'order_id': oid, 'order_no': ono, 'status': 'not_found'})
                    continue
                if order['status'] != 2:
                    results.append({'order_id': order['id'], 'order_no': order['order_no'], 'status': 'already_processed'})
                    continue
                # 5分钟保护
                if order.get('created_at') and (datetime.now() - order['created_at']).total_seconds() < 300:
                    results.append({'order_id': order['id'], 'order_no': order['order_no'], 'status': 'too_recent'})
                    continue
                actual_time = rec.get('retrieve_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute('UPDATE orders SET status = 3, retrieve_time = %s WHERE id = %s AND status = 2', (actual_time, order['id']))
                if order['slot_id']:
                    cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
                # 退还保证金到用户余额
                _return_balance_to_user(cursor, dict(order))
                results.append({'order_id': order['id'], 'order_no': order['order_no'], 'status': 'ok'})
                success_count += 1
            except Exception as e:
                results.append({'order_id': oid, 'order_no': ono, 'status': 'error', 'message': str(e)})
        conn.commit()
        # 对每个成功结束的订单发送通知
        for _ridx, _rrec in enumerate(results):
            if _rrec['status'] != 'ok':
                continue
            try:
                _r_order_data = None
                if _ridx < len(records):
                    _r_order_data = records[_ridx]
                else:
                    continue
                _nopenid = _r_order_data.get('openid', '') or ''
                _nphone = _r_order_data.get('user_phone', '')
                if not _nopenid and _nphone:
                    try:
                        _nc2 = get_db()
                        _ncur2 = _nc2.cursor()
                        _ncur2.execute('SELECT COALESCE(mp_openid, openid) as openid FROM users WHERE phone = %s ORDER BY updated_at DESC LIMIT 1', (_nphone,))
                        _nr2 = _ncur2.fetchone()
                        if _nr2:
                            _nopenid = _nr2['openid'] or ''
                        _ncur2.close()
                        _nc2.close()
                    except:
                        pass
                if _nopenid:
                    from helpers import send_wx_subscribe_message
                    send_wx_subscribe_message(_nopenid, 'UT0PehBf71OaahgZbqFfLPQt55BWc7tSz4D4NqCPDhE', {
                        "thing1": {"value": str(_r_order_data.get("compartment_number", "")) + "号柜门"},
                        "time3": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")}
                    })
                    logger.info('[offline_batch] 结束通知已发送')
                    _dep2 = _r_order_data.get('deposit_amount', 0)
                    if _dep2 > 0:
                        send_wx_subscribe_message(_nopenid, 'nG8Cdhn-Nym9ml4LatE9CdGXoJyyoi227vNzLMX9i8w', {
                            "amount2": {"value": str(_dep2) + "元"},
                            "thing4": {"value": "押金已退至余额"},
                            "time5": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")}
                        })
                        logger.info('[offline_batch] 退款通知已发送')
            except Exception as ne:
                logger.error(f'[offline_batch发送通知失败] {ne}')

        return json_response({'total': len(records), 'success': success_count, 'results': results})
    except Exception as e:
        logger.error(f'[offline_batch] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/offline-retrieve/mid-retrieve', methods=['POST'])
def mid_retrieve():
    try:
        data = request.get_json() or {}
        records = data.get('records') or []
        order_id = data.get('order_id')
        order_no = data.get('order_no')
        logger.info(f'[mid_retrieve] order_id={order_id}, order_no={order_no}')
        conn = get_db()
        cursor = conn.cursor()
        if records:
            success = 0
            errors = []
            for rec in records:
                rid = rec.get('order_id')
                rno = rec.get('order_no')
                if not rid and not rno:
                    errors.append('no order info')
                    continue
                try:
                    if rid:
                        cursor.execute('''SELECT o.*, cs.slot_number, cs.board_no, cs.lock_no,
                                          c.mainboard_device_id, c.cabinet_code
                                          FROM orders o
                                          LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id
                                          LEFT JOIN cabinets c ON o.cabinet_id = c.id
                                          WHERE o.id = %s''', (rid,))
                    else:
                        cursor.execute('''SELECT o.*, cs.slot_number, cs.board_no, cs.lock_no,
                                          c.mainboard_device_id, c.cabinet_code
                                          FROM orders o
                                          LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id
                                          LEFT JOIN cabinets c ON o.cabinet_id = c.id
                                          WHERE o.order_no = %s''', (rno,))
                    row = cursor.fetchone()
                    if not row:
                        errors.append('order not found: %s' % (rid or rno))
                        continue
                    _inc = try_increment_mid_retrieve(cursor, row['id'], row['cabinet_id'])
                    if not _inc['allowed']:
                        errors.append('mid retrieve limit reached for order %s' % (rid or rno))
                        continue
                    rtime = rec.get('retrieve_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    compartment = row.get('compartment_number') or row.get('slot_number')
                    cursor.execute("""INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time, retrieve_time)
                                      SELECT %s,%s,%s,%s,2,%s,%s
                                      WHERE NOT EXISTS (SELECT 1 FROM storage_records
                                        WHERE user_phone=%s AND cabinet_id=%s
                                          AND COALESCE(compartment_number,'')=%s
                                          AND store_time=%s AND retrieve_time=%s)""",
                                   (row['cabinet_id'], compartment, row['user_phone'], row.get('access_code'),
                                    row.get('store_time'), rtime,
                                    row['user_phone'], row['cabinet_id'], str(compartment or ''),
                                    row.get('store_time'), rtime))
                    if row.get('mainboard_device_id'):
                        cursor.execute("""INSERT INTO door_records (device_id, board_no, lock_no, order_id, open_type, create_time)
                                          SELECT %s,%s,%s,%s,'mid_offline',%s
                                          WHERE NOT EXISTS (SELECT 1 FROM door_records
                                            WHERE device_id=%s AND board_no=%s AND lock_no=%s
                                              AND order_id=%s AND open_type='mid_offline')""",
                                       (row['mainboard_device_id'], row.get('board_no') or 1, row.get('lock_no') or 1,
                                        str(row['id']), rtime,
                                        row['mainboard_device_id'], row.get('board_no') or 1, row.get('lock_no') or 1,
                                        str(row['id'])))
                    if row.get('status') == 2:
                        cursor.execute("UPDATE orders SET logical_mark='mid' WHERE id=%s", (row['id'],))
                    success += 1
                except Exception as e:
                    logger.error(f'[mid_retrieve] record处理异常: {e}')
                    errors.append(str(e))
            conn.commit()
            conn.close()
            if errors:
                return json_response(data={'total': len(records), 'success': success, 'errors': errors}, message='partial sync errors', code=400)
            return json_response({'message': 'mid retrieve recorded', 'total': len(records), 'success': success, 'errors': errors})
        if not order_id and not order_no:
            conn.close()
            return json_response(message='no order info', code=400)
        if order_id:
            cursor.execute('SELECT id, status, user_phone, cabinet_id FROM orders WHERE id = %s', (order_id,))
        else:
            cursor.execute('SELECT id, status, user_phone, cabinet_id FROM orders WHERE order_no = %s', (order_no,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='order not found', code=404)
        if order['status'] == 2:
            _inc = try_increment_mid_retrieve(cursor, order['id'], order['cabinet_id'])
            if not _inc['allowed']:
                conn.close()
                return json_response(message='mid retrieve limit reached', code=400)
            conn.commit()
        conn.close()
        return json_response({'message': 'mid retrieve recorded', 'order_id': order['id']})
    except Exception as e:
        logger.error(f'[mid_retrieve] {e}')
        return json_response(message=str(e), code=500)
