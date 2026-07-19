"""
支付相关API - Blueprint
包含：支付渠道管理、支付/退款回调
"""
import logging
import re
import json
import random
import string
from datetime import datetime
from flask import Blueprint, request
from database import get_db
from helpers import (json_response, require_auth, get_setting, is_mock_mode, logger, _get_device_protocol,
                     get_wxpay, get_channel_wxpay, update_channel_stats, send_open_lock)
from wxpay import WxPay, ThirdPartyPay

bp = Blueprint('payment', __name__)


# ============================================
# 支付渠道管理
# ============================================

@bp.route('/payment-channels', methods=['GET'])
def list_payment_channels():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM payment_channels ORDER BY id')
        channels = [dict(ch) for ch in cursor.fetchall()]
        conn.close()
        return json_response(channels)
    except Exception as e:
        logger.error(f'[list_channels] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/payment-channels', methods=['POST'])
@require_auth
def create_payment_channel():
    try:
        data = request.get_json()
        name = data.get('name')
        channel_type = data.get('channel_type', 'wechat')
        mch_id = data.get('mch_id')
        api_key = data.get('api_key')
        app_id = data.get('app_id')
        app_secret = data.get('app_secret')
        cert_name = data.get('cert_name')
        extra_config = data.get('extra_config')
        weight = data.get('weight', 1)
        daily_limit = data.get('daily_limit', 0)
        is_active = data.get('is_active', 1)
        if not name:
            return json_response(message='渠道名称不能为空', code=400)
        if channel_type == 'wechat' and not mch_id:
            return json_response(message='微信渠道商户号不能为空', code=400)
        if channel_type == 'third_party' and not mch_id:
            return json_response(message='第三方平台AppID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO payment_channels (name, channel_type, mch_id, api_key, app_id, app_secret, cert_name, extra_config, is_active, weight, daily_limit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                       (name, channel_type, mch_id, api_key, app_id, app_secret, cert_name,
                        json.dumps(extra_config) if isinstance(extra_config, dict) else extra_config,
                        is_active, weight, daily_limit))
        conn.commit()
        channel_id = cursor.lastrowid
        conn.close()
        return json_response({'id': channel_id, 'message': '渠道创建成功'})
    except Exception as e:
        logger.error(f'[create_channel] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/payment-channels/<int:channel_id>', methods=['PUT'])
@require_auth
def update_payment_channel(channel_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        updates, params = [], []
        for field in ['name', 'channel_type', 'mch_id', 'api_key', 'app_id', 'app_secret', 'cert_name', 'is_active', 'weight', 'daily_limit']:
            if field in data:
                if field == 'api_key' and not data[field]:
                    continue
                updates.append(f'{field} = %s')
                params.append(data[field])
        if 'extra_config' in data:
            updates.append("extra_config = %s")
            ec = data['extra_config']
            params.append(json.dumps(ec) if isinstance(ec, dict) else ec)
        if not updates:
            conn.close()
            return json_response(message='无更新内容', code=400)
        params.append(channel_id)
        cursor.execute(f"UPDATE payment_channels SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        conn.close()
        return json_response({'message': '渠道更新成功'})
    except Exception as e:
        logger.error(f'[update_channel] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/payment-channels/<int:channel_id>', methods=['DELETE'])
@require_auth
def delete_payment_channel(channel_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM orders WHERE payment_channel_id = %s', (channel_id,))
        if cursor.fetchone()[0] > 0:
            conn.close()
            return json_response(message=f'该渠道下有订单，无法删除，请禁用', code=400)
        cursor.execute('DELETE FROM payment_channels WHERE id = %s', (channel_id,))
        conn.commit()
        conn.close()
        return json_response({'message': '渠道删除成功'})
    except Exception as e:
        logger.error(f'[delete_channel] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/payment-channels/<int:channel_id>/toggle', methods=['POST'])
@require_auth
def toggle_payment_channel(channel_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT is_active FROM payment_channels WHERE id = %s', (channel_id,))
        channel = cursor.fetchone()
        if not channel:
            conn.close()
            return json_response(message='渠道不存在', code=404)
        new_status = 0 if channel['is_active'] else 1
        cursor.execute('UPDATE payment_channels SET is_active = %s WHERE id = %s', (new_status, channel_id))
        conn.commit()
        conn.close()
        return json_response({'is_active': new_status, 'message': '渠道状态已更新'})
    except Exception as e:
        logger.error(f'[toggle_channel] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/payment-channels/<int:channel_id>/reset-stats', methods=['POST'])
@require_auth
def reset_channel_stats(channel_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE payment_channels SET total_amount = 0, total_count = 0 WHERE id = %s', (channel_id,))
        conn.commit()
        conn.close()
        return json_response({'message': '统计已重置'})
    except Exception as e:
        logger.error(f'[reset_channel_stats] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 支付回调
# ============================================

@bp.route('/pay/notify', methods=['POST', 'GET'])
def pay_notify():
    """微信支付结果回调"""
    try:
        logger.info('[支付回调] 收到通知')
        xml_data = request.get_data(as_text=True)
        mock_mode = is_mock_mode()
        if mock_mode:
            return WxPay.build_pay_notify_response('SUCCESS', 'OK')

        temp_result = WxPay.xml_to_dict(xml_data)
        out_trade_no = temp_result.get('out_trade_no', '')
        # 先查订单关联的商户号，用正确的密钥验签
        notify_wxpay = None
        if out_trade_no:
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute('SELECT payment_channel_id FROM orders WHERE order_no = %s', (out_trade_no,))
                order_row = cursor.fetchone()
                if order_row and order_row['payment_channel_id']:
                    cursor.execute('SELECT * FROM payment_channels WHERE id = %s', (order_row['payment_channel_id'],))
                    ch = cursor.fetchone()
                    if ch:
                        wxpay_inst, ch_type = get_channel_wxpay(dict(ch))
                        if wxpay_inst and ch_type == 'wechat':
                            notify_wxpay = wxpay_inst
                # 没找到渠道，选一个活跃的
                _callback_channel_id = None
                if not notify_wxpay:
                    cursor.execute('SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1')
                    active_ch = cursor.fetchone()
                    if active_ch:
                        notify_wxpay, _ = get_channel_wxpay(dict(active_ch))
                        _callback_channel_id = active_ch['id']
                elif order_row and order_row['payment_channel_id']:
                    _callback_channel_id = order_row['payment_channel_id']
                conn.close()
            except Exception as e:
                logger.error(f'[支付回调] 渠道查询异常: {e}')
        if not notify_wxpay:
            logger.error('[支付回调] 无可用活跃商户')
            return 'fail', 500

        result = notify_wxpay.parse_pay_notify(xml_data)
        if result.get('return_code') != 'SUCCESS':
            return notify_wxpay.build_pay_notify_response('FAIL', result.get('return_msg')), 400

        out_trade_no = result.get('out_trade_no')
        transaction_id = result.get('transaction_id')
        trade_state = result.get('trade_state', result.get('result_code'))

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE order_no = %s', (out_trade_no,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return notify_wxpay.build_pay_notify_response('FAIL', '订单不存在'), 400
        if order['status'] in [2, 3, 4]:
            conn.close()
            return notify_wxpay.build_pay_notify_response('SUCCESS', 'OK'), 200

        if trade_state == 'SUCCESS' or result.get('result_code') == 'SUCCESS':
            # 更新订单状态和支付渠道ID
            if order.get('payment_channel_id') is None and '_callback_channel_id' in locals() and _callback_channel_id:
                cursor.execute('UPDATE orders SET status = 2, transaction_id = %s, pay_time = %s, payment_channel_id = %s WHERE id = %s',
                               (transaction_id, datetime.now(), _callback_channel_id, order['id']))
            else:
                cursor.execute('UPDATE orders SET status = 2, transaction_id = %s, pay_time = %s WHERE id = %s',
                               (transaction_id, datetime.now(), order['id']))
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (order['slot_id'],))
            cursor.execute('INSERT INTO payments (order_id, type, amount, transaction_id, status) VALUES (%s, 1, %s, %s, 1)',
                           (order['id'], order['deposit_amount'], transaction_id))
            # 更新用户余额 - 统一用 mp_openid 查找
            try:
                _o_openid = order.get('openid', '') or ''
                _o_unionid = order.get('unionid', '') or ''
                _o_mp_openid = order.get('mp_openid', '') or _o_openid
                # 统一用 mp_openid 查找
                if _o_mp_openid:
                    cursor.execute('SELECT id, phone FROM user_balances WHERE mp_openid = %s', (_o_mp_openid,))
                    ub = cursor.fetchone()
                    if not ub:
                        # 尝试从 openid 字段查找并获取 mp_openid
                        cursor.execute('SELECT id, phone, mp_openid FROM user_balances WHERE openid = %s', (_o_openid,))
                        ub = cursor.fetchone()
                        if ub and not ub.get('mp_openid'):
                            # 更新 mp_openid
                            cursor.execute('UPDATE user_balances SET mp_openid = %s WHERE id = %s', (_o_mp_openid, ub['id']))
                else:
                    cursor.execute('SELECT id, phone, mp_openid FROM user_balances WHERE phone = %s', (order['user_phone'],))
                    ub = cursor.fetchone()
                    if ub and ub.get('mp_openid'):
                        _o_mp_openid = ub['mp_openid']
                if ub:
                    cursor.execute('UPDATE user_balances SET total_deposited = total_deposited + %s, phone = %s, mp_openid = COALESCE(NULLIF(mp_openid, ''), %s) WHERE mp_openid = %s',
                                   (order['deposit_amount'], order['user_phone'], _o_mp_openid, _o_mp_openid))
                else:
                    # 没找到，先用 phone 查，避免重复
                    _dup = None
                    try:
                        cursor.execute("SELECT id FROM user_balances WHERE phone = %s LIMIT 1", (order['user_phone'],))
                        _dup = cursor.fetchone()
                    except:
                        pass
                    if _dup:
                        # phone_openids 桥接，获取正确的 mp_openid
                        try:
                            cursor.execute("SELECT mp_openid, openid FROM phone_openids WHERE phone = %s ORDER BY updated_at DESC LIMIT 1", (order['user_phone'],))
                            _po_r = cursor.fetchone()
                            if _po_r:
                                if _po_r['mp_openid']:
                                    _o_mp_openid = _po_r['mp_openid']
                                if _po_r['openid']:
                                    _o_openid = _po_r['openid']
                        except:
                            pass
                        cursor.execute('UPDATE user_balances SET openid = %s, mp_openid = %s, total_deposited = total_deposited + %s WHERE id = %s',
                                       (_o_openid, _o_mp_openid, order['deposit_amount'], _dup['id']))
                    else:
                        # 真的没找到，插入新记录
                        _wn_name = ''
                    cursor.execute("SELECT wechat_name FROM phone_openids WHERE phone = %s AND wechat_name IS NOT NULL AND wechat_name != '' LIMIT 1", (order['user_phone'],))
                    _wn_r = cursor.fetchone()
                    if _wn_r:
                        _wn_name = _wn_r['wechat_name']
                    if not _wn_name and _o_openid:
                        cursor.execute("SELECT wechat_name FROM user_profiles WHERE openid = %s AND wechat_name IS NOT NULL AND wechat_name != '' LIMIT 1", (_o_openid,))
                        _wn_r = cursor.fetchone()
                        if _wn_r:
                            _wn_name = _wn_r['wechat_name']
                    cursor.execute('INSERT INTO user_balances (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, 0, 0, %s)',
                                   (order['user_phone'], _o_openid, _o_unionid, _o_mp_openid, _wn_name, datetime.now()))
            except Exception as e:
                logger.error(f'[支付回调更新余额失败] {e}')

            # 记录开锁信息（在commit之前读取，避免DB锁）
            _open_lock_info = None
            try:
                cursor.execute('''SELECT c.mainboard_device_id, c.mainboard_source, c.name as cabinet_name, l.name as location_name, cs.board_no, cs.lock_no 
    FROM cabinets c LEFT JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON cs.id = %s WHERE c.id = %s''', (order['slot_id'], order['cabinet_id']))
                cab_info = cursor.fetchone()
                if cab_info and cab_info['mainboard_device_id']:
                    _open_lock_info = {
                        'device_id': str(cab_info['mainboard_device_id']),
                        'board_no': cab_info['board_no'] or 1,
                        'lock_no': cab_info['lock_no'] or (int(re.match(r'[A-Za-z]*(\d+)', str(order['compartment_number'] or '1')).group(1)) if order['compartment_number'] else 1),
                        'protocol': cab_info['mainboard_source'] or _get_device_protocol(str(cab_info.get('mainboard_device_id',''))) or 'YBM',
                        'order_id': order['order_no'],
                        'cabinet_name': cab_info['cabinet_name'] or '',
                        'location_name': cab_info['location_name'] or ''
                    }
            except Exception as e:
                logger.error(f'[支付回调读取开锁信息失败] {e}')
            _channel_id = order['payment_channel_id']
            _deposit_amount = order['deposit_amount']
        else:
            cursor.execute('UPDATE orders SET status = 5 WHERE id = %s', (order['id'],))
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
                conn.commit()
        # 先在DB提交前发送开门指令（先commit释放DB锁，再发开门指令）（WebSocket立即发出，独立DB连接不冲突）
        if trade_state == 'SUCCESS' or result.get('result_code') == 'SUCCESS':
            if _open_lock_info:
                # 查door_records：已有开门记录则跳过（防止与store_pay重复发送）
                _dr_cur = conn.cursor()
                _dr_cur.execute("SELECT id FROM door_records WHERE order_id=%s AND device_id=%s LIMIT 1", (_open_lock_info['order_id'], _open_lock_info['device_id']))
                _dr_exists = _dr_cur.fetchone()
                if not _dr_exists:
                    try:
                        send_open_lock(_open_lock_info['device_id'], _open_lock_info['board_no'],
                                       _open_lock_info['lock_no'], _open_lock_info['protocol'],
                                       _open_lock_info['order_id'])
                    except Exception as e:
                        logger.error(f'[支付回调开锁失败] {e}')
                else:
                    logger.info(f'[支付回调] door_records已有记录，跳过开门: order_id={_open_lock_info["order_id"]}')
            if _channel_id:
                try:
                    update_channel_stats(_channel_id, _deposit_amount)
                except Exception as e:
                    logger.error(f'[支付回调更新渠道统计失败] {e}')
        conn.commit()
        
        # 发送寄存成功订阅消息
        if trade_state == 'SUCCESS' or result.get('result_code') == 'SUCCESS':
            try:
                openid = order.get('openid')
                if not openid:
                    try:
                        cur = conn.cursor()
                        cur.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s', (order['user_phone'],))
                        r = cur.fetchone()
                        if r:
                            openid = r['openid']
                    except:
                        pass
                if openid:
                    from helpers import send_wx_subscribe_message
                    location_name = _open_lock_info.get('location_name', '智能寄存柜') if _open_lock_info else '智能寄存柜'
                    cabinet_name = _open_lock_info.get('cabinet_name', '') if _open_lock_info else ''
                    door_label = (cabinet_name + '-' if cabinet_name else '') + str(order['compartment_number']) + '号柜门'
                    subscribe_data = {
                        'thing1': {'value': location_name},
                        'thing2': {'value': door_label},
                        'thing3': {'value': str(order['deposit_amount']) + '元'},
                        'time4': {'value': datetime.now().strftime('%Y-%m-%d %H:%M')},
                        'time5': {'value': datetime.now().strftime('%Y-%m-%d %H:%M')}
                    }
                    send_wx_subscribe_message(openid, 'aUc6gRRMUXKxy94Pd6kLWaLGwzcutYMW_cQT_Hks1fg', subscribe_data)
            except Exception as e:
                logger.error(f'[支付回调发送订阅消息失败] {e}')
        
        conn.close()
        return notify_wxpay.build_pay_notify_response('SUCCESS', 'OK'), 200
    except Exception as e:
        logger.error(f'[pay_notify] {e}')
        return 'fail', 500


@bp.route('/refund/notify', methods=['POST', 'GET'])
def refund_notify():
    """微信退款回调"""
    try:
        logger.info('[退款回调] 收到通知')
        xml_data = request.get_data(as_text=True)
        mock_mode = is_mock_mode()
        if mock_mode:
            return WxPay.build_pay_notify_response('SUCCESS', 'OK')
        # 退款回调：尝试从XML获取订单号，查对应商户
        wxpay = None
        try:
            _pre = WxPay.xml_to_dict(xml_data)
            _out_refund_no = _pre.get('out_refund_no', '')
            if _out_refund_no:
                _rc = get_db().cursor()
                # 退款单号含订单ID，尝试关联
                _rc.execute("SELECT o.payment_channel_id FROM orders o WHERE o.order_no LIKE %s LIMIT 1", (_out_refund_no[:20] + '%',))
                _rr = _rc.fetchone()
                if _rr and _rr.get('payment_channel_id'):
                    _rc.execute("SELECT * FROM payment_channels WHERE id=%s", (_rr['payment_channel_id'],))
                    _ch = _rc.fetchone()
                    if _ch:
                        wxpay, _ = get_channel_wxpay(dict(_ch))
                _rc.connection.close()
        except:
            pass
        if not wxpay:
            # fallback: 选一个活跃商户
            try:
                _ac = get_db().cursor()
                _ac.execute("SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1")
                _ach = _ac.fetchone()
                if _ach:
                    wxpay, _ = get_channel_wxpay(dict(_ach))
                _ac.connection.close()
            except:
                pass
        if not wxpay:
            logger.error('[退款回调] 无可用活跃商户')
            return 'fail', 500
        result, success = wxpay.parse_refund_notify(xml_data)
        if not success:
            return wxpay.build_pay_notify_response('FAIL', '解析失败'), 400
        return wxpay.build_pay_notify_response('SUCCESS', 'OK'), 200
    except Exception as e:
        logger.error(f'[refund_notify] {e}')
        return 'fail', 500


@bp.route('/pay/notify/third-party', methods=['POST', 'GET'])
def third_party_pay_notify():
    """第三方支付回调"""
    try:
        logger.info('[第三方支付回调] 收到通知')
        if request.method == 'GET':
            params = dict(request.args)
        else:
            params = dict(request.form) if request.form else (request.get_json(silent=True) or {})

        out_trade_no = params.get('trade_order_id', params.get('out_trade_no', ''))
        if not out_trade_no:
            return 'fail', 400

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE order_no = %s', (out_trade_no,))
        order = cursor.fetchone()
        if not order or order['status'] in [2, 3, 4]:
            conn.close()
            return 'fail' if not order else 'success'

        # 验签
        if order['payment_channel_id']:
            cursor.execute('SELECT * FROM payment_channels WHERE id = %s', (order['payment_channel_id'],))
            channel = cursor.fetchone()
            if channel:
                tpp = ThirdPartyPay(appid=channel['mch_id'], appsecret=channel['api_key'], notify_url='')
                if not tpp.verify_notify(params):
                    conn.close()
                    return 'fail', 400

        if params.get('status') == 'OD' or params.get('return_code') == 'SUCCESS':
            transaction_id = params.get('transaction_id', params.get('order_id', ''))
            cursor.execute('UPDATE orders SET status = 2, transaction_id = %s, pay_time = %s WHERE id = %s',
                           (transaction_id, datetime.now(), order['id']))
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (order['slot_id'],))
            cursor.execute('INSERT INTO payments (order_id, type, amount, transaction_id, status) VALUES (%s, 1, %s, %s, 1)',
                           (order['id'], order['deposit_amount'], transaction_id))
            if order['payment_channel_id']:
                update_channel_stats(order['payment_channel_id'], order['deposit_amount'])
            conn.commit()
            conn.close()
            return 'success'
        conn.close()
        return 'fail'
    except Exception as e:
        logger.error(f'[third_party_notify] {e}')
        return 'fail', 500