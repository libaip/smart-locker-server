"""
离线取包API - Blueprint
支持断网时APK本地验证，网络恢复后同步到服务器
"""
import logging
import json
from datetime import datetime
from flask import Blueprint, request
from database import get_db
from helpers import json_response, logger, pending_lock_commands, connected_devices

def _return_balance_to_user(cursor, order_dict):
    """离线取包/APK取件时退还保证金到用户余额"""
    deposit_amount = order_dict.get('deposit_amount', 0)
    if deposit_amount <= 0:
        return (0, order_dict.get('openid', '') or '')
    user_phone = order_dict.get('user_phone', '')
    if not user_phone:
        return
    _openid = order_dict.get('openid', '') or ''
    _unionid = order_dict.get('unionid', '') or ''
    if not _openid:
        cursor.execute('SELECT COALESCE(mp_openid, openid) as openid, unionid FROM phone_openids WHERE phone = %s ORDER BY updated_at DESC LIMIT 1', (user_phone,))
        _po = cursor.fetchone()
        if _po:
            if not _openid:
                _openid = _po.get('openid', '') or ''
            if not _unionid:
                _unionid = _po.get('unionid', '') or ''
    # 查询余额记录
    _ub = None
    if _unionid:
        cursor.execute('SELECT id FROM user_balances WHERE unionid = %s', (_unionid,))
        _ub = cursor.fetchone()
    # [Agent-modified 2026-07-04] openid优先查找余额桶
    if not _ub and _openid:
        cursor.execute('SELECT id FROM user_balances WHERE openid = %s', (_openid,))
        _ub = cursor.fetchone()
    if not _ub:
        empty_openid = ''
        cursor.execute("SELECT id FROM user_balances WHERE phone = %s AND (openid = %s OR openid IS NULL OR openid = '')", (user_phone, empty_openid))
        _ub = cursor.fetchone()
    if _ub:
        if _unionid:
            cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s, openid = %s, phone = %s WHERE unionid = %s',
                           (deposit_amount, deposit_amount, _openid, user_phone, _unionid))
        elif _openid:
            cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE openid = %s',
                           (deposit_amount, deposit_amount, _openid))
        else:
            empty_openid = ''
            cursor.execute("UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE phone = %s AND (openid = %s OR openid IS NULL OR openid = '')",
                           (deposit_amount, deposit_amount, user_phone, empty_openid))
    else:
        cursor.execute('INSERT INTO user_balances (phone, openid, unionid, balance, total_deposited, total_withdrawn, first_use_time) VALUES (%s, %s, %s, %s, %s, 0, NOW())',
                       (user_phone, _openid, _unionid, deposit_amount, deposit_amount))
    # 写入余额明细
    cursor.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING",
                   (user_phone, order_dict['id'], deposit_amount))
    cursor.execute('SELECT 1')
    cursor.fetchall()
    return (deposit_amount, _openid)
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
            if command_text:
                try:
                    cmd = json.loads(command_text)
                    valid_commands.append(cmd)
                except:
                    valid_commands.append({'order_id': str(row['order_id']) if row['order_id'] else '', 'board_no': row['board_no'], 'lock_no': row['lock_no'], 'action': 'open', 'protocol': row['protocol'], 'timestamp': row['created_at']})
            else:
                valid_commands.append({'order_id': str(row['order_id']) if row['order_id'] else '', 'board_no': row['board_no'], 'lock_no': row['lock_no'], 'action': 'open', 'protocol': row['protocol'], 'timestamp': row['created_at']})
            cursor.execute('UPDATE pending_lock_cmds SET delivered=1 WHERE id=%s', (row['id'],))
            logger.info(f'[DEBUG_POLL] device={device_id} delivered cmd id={row["id"]} command={command_text[:100]}')

        update_info = None
        try:
            from config import AUTO_UPDATE_ENABLED
            if AUTO_UPDATE_ENABLED:
                cursor.execute('SELECT * FROM apk_version ORDER BY id DESC LIMIT 1')
                apk_row = cursor.fetchone()
                if apk_row:
                    update_info = {'has_update': True, 'version_name': apk_row['version_name'], 'version_code': apk_row['version_code'], 'download_url': apk_row['download_url'], 'update_desc': apk_row['update_desc'] or '', 'force': True}
        except:
            pass

        now = datetime.now()
        cursor.execute('SELECT o.id as order_id, o.order_no, o.user_phone, o.access_code, o.deposit_amount, o.compartment_number, o.slot_size, o.cabinet_id, o.store_time FROM orders o JOIN cabinets c ON o.cabinet_id = c.id WHERE c.mainboard_device_id = %s AND o.status = 2 ORDER BY o.id DESC', (device_id,))
        orders = [dict(row) for row in cursor.fetchall()]
        conn.commit()
        conn.close()

        if update_info:
            valid_commands.append({'type': 'force_update', 'download_url': update_info['download_url'], 'version_name': update_info['version_name'], 'version_code': update_info['version_code'], 'update_desc': update_info.get('update_desc', ''), 'force': update_info.get('force', False)})

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

        return json_response({"commands": valid_commands, "orders": orders, "server_time": now.strftime("%Y-%m-%d %H:%M:%S"), "update": update_info, "mainboard_config": mainboard_config})
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
        cursor.execute('SELECT o.id as order_id, o.order_no, o.user_phone, o.access_code, o.deposit_amount, o.compartment_number, o.slot_size, o.cabinet_id, o.store_time FROM orders o JOIN cabinets c ON o.cabinet_id = c.id WHERE c.mainboard_device_id = %s AND o.status = 2 ORDER BY o.id DESC', (device_id,))
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
                    _ncur.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s ORDER BY updated_at DESC LIMIT 1', (order['user_phone'],))
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
                        _ncur2.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s ORDER BY updated_at DESC LIMIT 1', (_nphone,))
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