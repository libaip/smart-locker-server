import psycopg2
from psycopg2.extras import RealDictCursor
"""
用户端API - Blueprint
包含：存包、取包、押金流程、短信验证、H5存包
"""
import logging
import random
import string
import json
import qrcode
import io
import base64
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_from_directory, redirect, send_file
from database import get_db
from helpers import (json_response, get_setting, is_mock_mode, is_wechat_browser, select_payment_channel,
                     is_mobile_browser, send_open_lock, update_channel_stats, get_payment_params,
                     generate_order_no, generate_access_code, generate_sms_code,
                     logger,
                     check_withdraw_auto_approve, mark_user_withdraw, get_withhold_hours)
from models import BRAND_DEFAULTS

bp = Blueprint('user', __name__)

def _resolve_user(cursor, openid='', mp_openid='', phone='', unionid=''):
    """统一解析出 user_id。找不到则自动创建。"""
    # 1. 直接按 user_id 查（如果调用方已有）
    # 2. 按 unionid 查
    if unionid:
        try:
            cursor.execute("SELECT id, openid FROM app_users WHERE unionid = %s AND id > 0 LIMIT 1", (unionid,))
            r = cursor.fetchone()
            if r:
                canonical_id = r['id']
                _existing_openid = r['openid'] or ''
                # 更新当前记录的 phone/openid/mp_openid（如果为空）
                update_fields = []
                update_vals = []
                if phone:
                    update_fields.append("phone = %s")
                    update_vals.append(phone)
                if openid and not _existing_openid:
                    update_fields.append("openid = %s")
                    update_vals.append(openid)
                if mp_openid:
                    update_fields.append("mp_openid = %s")
                    update_vals.append(mp_openid)
                if update_fields:
                    update_vals.append(canonical_id)
                    cursor.execute(f"UPDATE app_users SET {', '.join(update_fields)} WHERE id = %s", update_vals)
                
                # 查找并合并同 phone 的重复账户
                if phone:
                    cursor.execute("SELECT id FROM app_users WHERE phone = %s AND id != %s ORDER BY id", (phone, canonical_id))
                    dupes = cursor.fetchall()
                    for dupe in dupes:
                        old_id = dupe['id']
                        # 迁移订单
                        cursor.execute("UPDATE orders SET user_id = %s WHERE user_id = %s", (canonical_id, old_id))
                        # 迁移余额（保留较大的那个）
                        cursor.execute("""
                            UPDATE user_balances 
                            SET user_id = %s 
                            WHERE user_id = %s AND phone = %s
                            AND NOT EXISTS (SELECT 1 FROM user_balances WHERE user_id = %s AND phone = %s)
                        """, (canonical_id, old_id, phone, canonical_id, phone))
                        # 如果 canonical 已有余额，删除重复的
                        cursor.execute("DELETE FROM user_balances WHERE user_id = %s AND phone = %s", (old_id, phone))
                        # 删除重复的 app_users 记录
                        cursor.execute("DELETE FROM app_users WHERE id = %s", (old_id,))
                        cursor.execute("UPDATE phone_openids SET user_id = %s WHERE user_id = %s", (canonical_id, old_id))
                        import logging
                        logging.getLogger(__name__).info(f'[合并] user_id={old_id} -> {canonical_id} (phone={phone})')
                
                cursor.connection.commit()
                return canonical_id
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'[unionid merge error] {e}')
    # 3. 按 mp_openid 查
    if mp_openid:
        try:
            cursor.execute("SELECT id FROM app_users WHERE mp_openid = %s AND id > 0 LIMIT 1", (mp_openid,))
            r = cursor.fetchone()
            if r: return r['id']
        except: pass
    # 4. 按 openid 查
    if openid:
        try:
            cursor.execute("SELECT id FROM app_users WHERE openid = %s AND id > 0 LIMIT 1", (openid,))
            r = cursor.fetchone()
            if r: return r['id']
        except: pass
    # 5. 按 phone 查
    if phone:
        try:
            cursor.execute("SELECT id FROM app_users WHERE phone = %s AND id > 0 LIMIT 1", (phone,))
            r = cursor.fetchone()
            if r: return r['id']
        except: pass
    # 6. 通过 phone_openids 间接查
    if mp_openid or openid:
        try:
            _val = mp_openid or openid
            cursor.execute("SELECT user_id FROM phone_openids WHERE (mp_openid = %s OR openid = %s) AND user_id > 0 LIMIT 1", (_val, _val))
            r = cursor.fetchone()
            if r and r['user_id']: return r['user_id']
        except: pass
    if phone:
        try:
            cursor.execute("SELECT user_id FROM phone_openids WHERE phone = %s AND user_id > 0 LIMIT 1", (phone,))
            r = cursor.fetchone()
            if r and r['user_id']: return r['user_id']
        except: pass
    # 7. 找不到，自动创建
    try:
        cursor.execute("""
            INSERT INTO app_users (unionid, phone, openid, mp_openid)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (unionid or '', phone or '', openid or '', mp_openid or ''))
        r = cursor.fetchone()
        if r:
            new_id = r['id']
            # 同步到 phone_openids
            if phone:
                cursor.execute("UPDATE phone_openids SET user_id = %s WHERE phone = %s AND user_id = 0", (new_id, phone))
            return new_id
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'[_resolve_user] 创建失败: {e}')
    return 0



# [FIX-20260719] session_key缓存，解决wx.login()导致session_key失效问题
# key=openid, value={'session_key': ..., 'ts': ...}
_session_key_cache = {}

def _cache_session_key(openid, session_key):
    import time
    _session_key_cache[openid] = {'session_key': session_key, 'ts': time.time()}
    # 清理超过10分钟的旧缓存
    now = time.time()
    expired = [k for k, v in _session_key_cache.items() if now - v['ts'] > 600]
    for k in expired:
        _session_key_cache.pop(k, None)

def _get_cached_session_key(openid):
    import time
    entry = _session_key_cache.get(openid)
    if entry and time.time() - entry['ts'] < 600:
        return entry['session_key']
    return None


def _resolve_mp_openid(cursor, mp_openid='', openid='', phone=''):
    """统一从 mp_openid/openid/phone 解析出 mp_openid，找不到返回 None"""
    # 1. 直接传入 mp_openid
    if mp_openid:
        return mp_openid
    # 2. openid 可能就是 mp_openid（小程序场景）
    if openid:
        # 先从 user_balances 按 mp_openid 查
        try:
            cursor.execute("SELECT mp_openid FROM user_balances WHERE mp_openid = %s LIMIT 1", (openid,))
            _r = cursor.fetchone()
            if _r and _r['mp_openid']:
                return _r['mp_openid']
        except Exception:
            pass
        # 再从 user_balances 按 openid 字段查，取其 mp_openid
        try:
            cursor.execute("SELECT mp_openid FROM user_balances WHERE openid = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (openid,))
            _r = cursor.fetchone()
            if _r and _r['mp_openid']:
                return _r['mp_openid']
        except Exception:
            pass
        # 从 phone_openids 按 openid 查（mp_openid 或 openid 都行）
        try:
            cursor.execute("SELECT mp_openid, openid FROM phone_openids WHERE openid = %s LIMIT 1", (openid,))
            _r = cursor.fetchone()
            if _r:
                if _r['mp_openid']:
                    return _r['mp_openid']
                elif _r['openid']:
                    return _r['openid']
        except Exception:
            pass
        # 从 phone_openids 按 mp_openid 查（小程序openid存这里）
        try:
            cursor.execute("SELECT mp_openid FROM phone_openids WHERE mp_openid = %s LIMIT 1", (openid,))
            _r = cursor.fetchone()
            if _r and _r['mp_openid']:
                return _r['mp_openid']
        except Exception:
            pass
        # 从 user_balances 按 openid 查（公众号openid可能在这里）
        try:
            cursor.execute("SELECT mp_openid, openid FROM user_balances WHERE openid = %s LIMIT 1", (openid,))
            _r = cursor.fetchone()
            if _r:
                if _r['mp_openid']:
                    return _r['mp_openid']
                elif _r['openid']:
                    return _r['openid']
        except Exception:
            pass
    # 3. 用 phone 反查 mp_openid
    if phone:
        try:
            cursor.execute("SELECT mp_openid, openid FROM phone_openids WHERE phone = %s ORDER BY updated_at DESC LIMIT 1", (phone,))
            _r = cursor.fetchone()
            if _r and _r['mp_openid']:
                return _r['mp_openid']
        except Exception:
            pass
        try:
            cursor.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (phone,))
            _r = cursor.fetchone()
            if _r and _r['mp_openid']:
                return _r['mp_openid']
        except Exception:
            pass
    return None




# ============================================
# 存包流程
# ============================================



@bp.route('/cabinet/<int:cabinet_id>/available-sizes', methods=['GET'])
def cabinet_available_sizes(cabinet_id):
    """获取柜体可用的格子尺寸列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT slot_size, COUNT(*) as total, SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) as available "
            "FROM cabinet_slots WHERE cabinet_id = %s GROUP BY slot_size ORDER BY slot_size",
            (cabinet_id,))
        sizes = []
        for row in cursor.fetchall():
            sizes.append({'size': row['slot_size'], 'total': row['total'], 'available': row['available'] or 0})
        conn.close()
        return json_response({'sizes': sizes})
    except Exception as e:
        logger.error(f'[cabinet_available_sizes] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/deposit/pay-order', methods=['POST'])
def deposit_pay_order():
    """对已存在的订单发起支付（柜门已在store/init时分配）"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='order_id不能为空', code=400)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, c.mainboard_device_id FROM orders o JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        conn.close()

        if not order:
            return json_response(message='订单不存在', code=404)
        if order['status'] != 1:
            return json_response(message='订单状态异常，无法支付', code=400)

        from helpers import select_payment_channel
        payment_channel = select_payment_channel()
        payment_channel_id = payment_channel['id'] if payment_channel else None

        openid = data.get('openid')
        pay_params = get_payment_params(order_id, order['order_no'], order['deposit_amount'],
                                         order['user_phone'], openid,
                                         payment_channel=payment_channel,
                                         payment_channel_id=payment_channel_id)

        return json_response({
            'order_id': order['id'], 'order_no': order['order_no'],
            'access_code': order['access_code'], 'cabinet_id': order['cabinet_id'],
            'compartment_number': order['compartment_number'],
            'deposit_amount': order['deposit_amount'], 'pay_params': pay_params
        })
    except Exception as e:
        logger.error(f'[deposit_pay_order] 错误: {e}')
        return json_response(message=str(e), code=500)

@bp.route('/store/init', methods=['POST'])
def store_init():
    """存包初始化 - 分配柜格并创建订单（状态=待支付）"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        slot_size = data.get('slot_size', 'M')
        user_phone = data.get('phone')
        sms_code = data.get('sms_code')
        access_code = data.get('access_code')
        openid = data.get('openid', '')
        unionid = data.get('unionid', '')
        # [FIX-20260716] 禁止改成 "or openid" 回退！openid可能是公众号openid，会导致订阅消息40003
        mp_openid = data.get('mp_openid', '')
        if not mp_openid and user_phone:
            _conn_mp = get_db()
            _cur_mp = _conn_mp.cursor()
            _cur_mp.execute("SELECT mp_openid FROM phone_openids WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (user_phone,))
            _r = _cur_mp.fetchone()
            _conn_mp.close()
            if _r and _r['mp_openid']:
                mp_openid = _r['mp_openid']

        if not all([cabinet_id, user_phone]):
            return json_response(message='参数不完整', code=400)

        # 检查设备是否在线（查 last_heartbeat，5分钟内算在线）
        # 备份位置：user.py.bak.onlinecheck
        conn0 = get_db()
        cur0 = conn0.cursor()
        cur0.execute("SELECT mainboard_device_id, last_heartbeat FROM cabinets WHERE id = %s", (cabinet_id,))
        cab0 = cur0.fetchone()
        conn0.close()
        if cab0 and cab0['mainboard_device_id']:
            hb = cab0['last_heartbeat']
            if not hb:
                return json_response(message='设备离线，请稍后再试', code=400)
            from datetime import datetime as _dt
            if (_dt.now() - hb).total_seconds() > 300:
                return json_response(message='设备离线，请稍后再试', code=400)
        # 如果没有绑定设备ID，允许仅从数据库分配


        sms_enabled = get_setting('sms_enabled', 'false').lower() == 'true'
        if sms_enabled and not sms_code:
            return json_response(message='请输入短信验证码', code=400)

        if sms_enabled:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM sms_codes WHERE phone = %s AND code = %s AND expires_at > %s ORDER BY id DESC LIMIT 1',
                           (user_phone, sms_code, datetime.now()))
            sms_record = cursor.fetchone()
            if not sms_record:
                conn.close()
                return json_response(message='短信验证码错误', code=400)
            cursor.execute('DELETE FROM sms_codes WHERE id = %s', (sms_record['id'],))
            conn.commit()
            conn.close()

        conn = get_db()
        cursor = conn.cursor()

        # 清理超过15分钟的未支付订单，释放柜门
        from datetime import timedelta
        expire_time = datetime.now() - timedelta(minutes=15)
        cursor.execute('SELECT id, slot_id FROM orders WHERE status = 1 AND store_time < %s', (expire_time,))
        expired = cursor.fetchall()
        for exp in expired:
            if exp['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s AND status = 2', (exp['slot_id'],))
            cursor.execute('DELETE FROM orders WHERE id = %s AND status = 1', (exp['id'],))
        if expired:
            conn.commit()
            logger.info(f'[store_init] 清理了 {len(expired)} 个过期未支付订单')

        # 复用同一手机号+同一柜子的未支付订单（避免重复分配柜门）
        cursor.execute('SELECT id, order_no, slot_id, compartment_number, access_code, slot_size, deposit_amount, payment_channel_id FROM orders WHERE user_phone = %s AND cabinet_id = %s AND status = 1 ORDER BY id DESC LIMIT 1', (user_phone, cabinet_id))
        existing_order = cursor.fetchone()
        if existing_order:
            conn.close()
            logger.info(f'[store_init] 复用已有未支付订单 {existing_order["id"]}, phone={user_phone}')
            return json_response({'order_id': existing_order['id'], 'order_no': existing_order['order_no'],
                                  'access_code': existing_order['access_code'], 'cabinet_id': cabinet_id,
                                  'compartment_number': existing_order['compartment_number'],
                                  'compartment_label': '',
                                  'slot_id': existing_order['slot_id'],
                                  'slot_size': existing_order.get('slot_size', ''),
                                  'deposit_amount': existing_order['deposit_amount']})

        cursor.execute('SELECT cs.*, MAX(o.store_time) as last_used_at FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id LEFT JOIN orders o ON o.slot_id = cs.id WHERE c.id = %s AND cs.status = 1 AND cs.slot_size = %s GROUP BY cs.id ORDER BY CASE WHEN MAX(o.store_time) IS NULL THEN 0 ELSE 1 END, MAX(o.store_time) ASC, cs.slot_number ASC LIMIT 1',
                       (cabinet_id, slot_size))
        slot = cursor.fetchone()
        if not slot:
            cursor.execute('SELECT cs.*, MAX(o.store_time) as last_used_at FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id LEFT JOIN orders o ON o.slot_id = cs.id WHERE c.id = %s AND cs.status = 1 GROUP BY cs.id ORDER BY CASE WHEN MAX(o.store_time) IS NULL THEN 0 ELSE 1 END, MAX(o.store_time) ASC, cs.slot_number ASC LIMIT 1',
                           (cabinet_id,))
            slot = cursor.fetchone()
        if not slot:
            conn.close()
            return json_response(message='暂无可用柜格', code=400)

        if not access_code:
            access_code = generate_access_code()
        elif len(access_code) != 4 or not access_code.isdigit():
            conn.close()
            return json_response(message='取件码必须为4位数字', code=400)

        order_no = generate_order_no()
        cursor.execute('SELECT deposit_amount FROM cabinets WHERE id = %s', (cabinet_id,))
        cab_row = cursor.fetchone()
        deposit_amount = cab_row['deposit_amount'] if cab_row and cab_row['deposit_amount'] else float(get_setting('deposit_amount', '20'))
        compartment_display = slot['slot_label'] if 'slot_label' in slot.keys() and slot['slot_label'] else (slot['display_number'] if slot['display_number'] else slot['slot_number'])

        # 选择支付渠道（轮转）
        payment_channel = select_payment_channel()
        payment_channel_id = payment_channel['id'] if payment_channel else None

        cursor.execute('INSERT INTO orders (order_no, user_phone, slot_id, cabinet_id, compartment_number, access_code, deposit_amount, status, store_time, payment_channel_id, openid, unionid, mp_openid) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s) RETURNING id',
                       (order_no, user_phone, slot['id'], cabinet_id, compartment_display, access_code, deposit_amount, datetime.now(), payment_channel_id, openid, unionid, mp_openid))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return json_response(message="订单创建失败", code=500)
        order_id = row["id"]
        conn.commit()
        conn.close()

        return json_response({'order_id': order_id, 'order_no': order_no, 'access_code': access_code,
                              'slot_id': slot['id'], 'cabinet_id': cabinet_id, 'compartment_number': compartment_display, 'compartment_label': slot['slot_label'] if 'slot_label' in slot.keys() and slot['slot_label'] else '',
                              'slot_size': slot['slot_size'], 'deposit_amount': deposit_amount})
    except Exception as e:
        import traceback; logger.error(f'[store_init] 错误: {e}'); logger.error(traceback.format_exc())
        return json_response(message=str(e), code=500)


@bp.route('/deposit/get-pay-params', methods=['POST'])
def get_pay_params_api():
    """获取已有订单的支付参数"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        phone = data.get('phone', '')
        openid = data.get('openid', '')
        unionid = data.get('unionid', '')
        wechat_name = data.get("wechat_name", "")
        if not order_id:
            return json_response(message='缺少订单ID', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if order['status'] == 2:
            conn.close()
            return json_response({'order_status': 'paid', 'order_id': order['id'], 'compartment_number': order['compartment_number']})
        if order['status'] != 1:
            conn.close()
            return json_response(message='订单状态异常', code=400)
        if wechat_name and phone:
            try:
                cursor.execute("UPDATE phone_openids SET wechat_name = %s WHERE phone = %s AND (wechat_name IS NULL OR wechat_name = \"\")", (wechat_name, phone))
                conn.commit()
            except:
                pass
        # 检查是否过期（15分钟）
        from datetime import timedelta
        if order['store_time'] and (datetime.now() - order['store_time']).total_seconds() > 900:
            # 释放柜门
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s AND status = 2', (order['slot_id'],))
            cursor.execute('DELETE FROM orders WHERE id = %s AND status = 1', (order_id,))
            conn.commit()
            conn.close()
            return json_response(message='订单已过期，请重新下单', code=400)
        conn.close()
        pay_params = get_payment_params(order_id, order['order_no'], order['deposit_amount'], phone, openid, payment_channel_id=order.get('payment_channel_id'))
        return json_response({
            'order_id': order['id'], 'order_no': order['order_no'],
            'compartment_number': order['compartment_number'],
            'access_code': order['access_code'],
            'deposit_amount': order['deposit_amount'],
            'pay_params': pay_params
        })
    except Exception as e:
        logger.error(f'[get_pay_params] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/store', methods=['POST'])
def store_legacy():
    """存包API - 简化版兼容"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        compartment_size = data.get('size', 'M')
        user_phone = data.get('phone')
        access_code = data.get('access_code', generate_access_code())
        if not all([cabinet_id, user_phone]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT cs.* FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id WHERE c.id = %s AND cs.status = 1 AND cs.slot_size = %s LIMIT 1',
                       (cabinet_id, compartment_size))
        slot = cursor.fetchone()
        if not slot:
            cursor.execute('SELECT cs.* FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id WHERE c.id = %s AND cs.status = 1 LIMIT 1', (cabinet_id,))
            slot = cursor.fetchone()
        if not slot:
            conn.close()
            return json_response(message='暂无可用柜格', code=400)
        cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (slot['id'],))
        cursor.execute('INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time) VALUES (%s, %s, %s, %s, 2, %s)',
                       (cabinet_id, slot['slot_number'], user_phone, access_code, datetime.now()))
        conn.commit()
        conn.close()
        return json_response({'cabinet_id': cabinet_id, 'compartment_number': slot['slot_number'], 'access_code': access_code, 'slot_size': slot['slot_size']})
    except Exception as e:
        logger.error(f'[store_legacy] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 取包流程
# ============================================

@bp.route('/retrieve', methods=['POST'])
def retrieve():
    """取包API - 支持两种模式：
    1. 原始模式: cabinet_id + compartment_number + access_code
    2. APK模式: phone + access_code + device_id (PickUpActivity调用)
    """
    try:
        data = request.get_json()
        access_code = data.get('access_code', '')
        phone = data.get('phone', '')
        device_id = data.get('device_id', '')
        cabinet_id = data.get('cabinet_id')
        compartment_number = data.get('compartment_number')

        conn = get_db()
        cursor = conn.cursor()

        # APK模式: phone + access_code + device_id
        if phone and access_code and device_id:
            cursor.execute(
                'SELECT o.*, c.mainboard_device_id FROM orders o '
                'JOIN cabinets c ON o.cabinet_id = c.id '
                'WHERE o.user_phone = %s AND o.access_code = %s AND c.mainboard_device_id = %s AND o.status = 2 '
                'ORDER BY CASE WHEN o.status = 2 THEN 0 ELSE 1 END, o.id DESC',
                (phone, access_code, device_id))
            orders = cursor.fetchall()
            if not orders:
                conn.close()
                return json_response(message='取件码错误或柜格已空', code=400)
            # 遍历所有订单，逐个开门
            from helpers import send_open_lock
            for order in orders:
                try:
                    slot_id = order.get("slot_id")
                    if slot_id:
                        cur2 = conn.cursor()
                        cur2.execute("SELECT board_no, lock_no, slot_number FROM cabinet_slots WHERE id = %s", (slot_id,))
                        slot = cur2.fetchone()
                        if slot:
                            did = str(order.get("mainboard_device_id", ""))
                            bn = int(slot.get("board_no", 1) or 1)
                            ln = int(slot.get("lock_no", slot.get("slot_number", 1)) or slot.get("slot_number", 1))
                            if did:
                                send_open_lock(did, bn, ln, order_id=str(order["id"]))
                except Exception as open_err:
                    logger.error(f"[retrieve] 开锁失败(order={order.get('id')}): {open_err}")
                cursor.execute('UPDATE orders SET status = 3, retrieve_time = %s WHERE id = %s',
                (datetime.now(), order['id']))
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
                _deposit_amount = order.get('deposit_amount', 0)
                if _deposit_amount > 0:
                    _r_openid = order.get('openid', '') or ''
                    _r_unionid = order.get('unionid', '') or ''
                    _r_mp_openid = order.get('mp_openid', '') or _r_openid
                    # 统一用 mp_openid 查找用户余额
                    if not _r_mp_openid:
                        _r_mp_openid = _resolve_mp_openid(cursor, mp_openid='', openid=_r_openid, phone=order['user_phone'])
                    if _r_mp_openid:
                        cursor.execute('SELECT id FROM user_balances WHERE mp_openid = %s', (_r_mp_openid,))
                        _r_ub = cursor.fetchone()
                        if _r_ub:
                            cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE mp_openid = %s',
                                           (_deposit_amount, _deposit_amount, _r_mp_openid))
                        else:
                            _wechat_name = ''
                            if _r_openid:
                                cursor.execute("SELECT wechat_name FROM user_profiles WHERE openid = %s AND wechat_name IS NOT NULL AND wechat_name != '' LIMIT 1", (_r_openid,))
                                _wn_row = cursor.fetchone()
                                if _wn_row:
                                    _wechat_name = _wn_row['wechat_name']
                            if not _wechat_name:
                                cursor.execute("SELECT wechat_name FROM phone_openids WHERE phone = %s AND wechat_name IS NOT NULL AND wechat_name != '' LIMIT 1", (order['user_phone'],))
                                _wn_row2 = cursor.fetchone()
                                if _wn_row2:
                                    _wechat_name = _wn_row2['wechat_name']
                            cursor.execute("UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE mp_openid = %s", (_deposit_amount, _deposit_amount, _r_mp_openid))
                            if cursor.rowcount == 0:
                                cursor.execute("INSERT INTO user_balances (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                                               (order['user_phone'], _r_openid, _r_unionid, _r_mp_openid, _wechat_name, _deposit_amount, _deposit_amount, datetime.now()))
                    else:
                        # 没有 mp_openid，使用 phone 查找（兼容旧数据）
                        cursor.execute("SELECT id FROM user_balances WHERE phone = %s LIMIT 1", (order['user_phone'],))
                        _r_ub = cursor.fetchone()
                        if _r_ub:
                            cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE phone = %s',
                                           (_deposit_amount, _deposit_amount, order['user_phone']))
                        else:
                            cursor.execute("INSERT INTO user_balances (phone, openid, unionid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                           (order['user_phone'], _r_openid, _r_unionid, '', _deposit_amount, _deposit_amount, datetime.now()))
                    cursor.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING",
                               (order['user_phone'], order['id'], _deposit_amount))
                cursor.execute('UPDATE orders SET refund_mark = 1 WHERE id = %s', (order["id"],))
            _openid = order.get("openid")
            if not _openid:
                try:
                    cursor.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s', (order['user_phone'],))
                    _r2 = cursor.fetchone()
                    if _r2:
                        _openid = _r2['openid']
                except:
                    pass
            if _openid:
                try:
                    from helpers import send_wx_subscribe_message
                    subscribe_data = {
                        "amount6": {"value": "¥{:.2f}".format(float(order.get("deposit_amount", 0)))},
                        "time4": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")},
                        "thing7": {"value": "已退还至小程序用户钱包"},
                        "thing2": {"value": "请自行点击此通知消息跳转“我的钱包”提现"}
                    }
                    send_wx_subscribe_message(_openid, "5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A", subscribe_data, phone=order.get("user_phone"))
                except Exception as e:
                    logger.error(f"[retrieve发送订阅消息失败] {e}")
            conn.commit()
            conn.close()
            return json_response({'message': '柜门已打开', 'order_no': order['order_no'],
                                   'order_id': order['id'], 'code': 0})

        # 原始模式: cabinet_id + compartment_number + access_code
        if cabinet_id and compartment_number and access_code:
            cursor.execute(
                'SELECT * FROM storage_records WHERE cabinet_id = %s AND compartment_number = %s AND access_code = %s AND status = 2 ORDER BY id DESC LIMIT 1',
                (cabinet_id, compartment_number, access_code))
            record = cursor.fetchone()
            if not record:
                conn.close()
                return json_response(message='取件码错误或柜格已空', code=400)
            cursor.execute('UPDATE storage_records SET status = 1, retrieve_time = %s WHERE id = %s',
                           (datetime.now(), record['id']))
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE cabinet_id = %s AND slot_number = %s',
                           (cabinet_id, compartment_number))
            conn.commit()
            conn.close()
            return json_response(message='取包成功')

        conn.close()
        return json_response(message='参数不完整', code=400)
    except Exception as e:
        logger.error(f'[retrieve] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/retrieve/verify', methods=['POST'])
def retrieve_verify():
    """取包验证 - 验证手机号和取件码"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        phone = data.get('phone')
        access_code = data.get('access_code')
        openid = data.get('openid', '')
        unionid = data.get('unionid', '')
        if not all([cabinet_id, phone, access_code]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, COALESCE(cs.slot_size, o.slot_size) as slot_size, cs.board_no, cs.lock_no FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE o.cabinet_id = %s AND o.user_phone = %s AND o.access_code = %s AND o.status IN (2, 3) ORDER BY o.id DESC LIMIT 1',
                       (cabinet_id, phone, access_code))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='验证失败，请检查手机号和取件码', code=400)
        # 如果订单已结束（连点两次），跳过处理
        if order['status'] == 3:
            _deposit = order.get('deposit_amount', 0)
            conn.close()
            return json_response({'order_id': order['id'], 'order_no': order['order_no'], 'slot_id': order['slot_id'],
                                  'compartment_number': order['compartment_number'], 'slot_size': order['slot_size'],
                                  'board_no': order['board_no'], 'lock_no': order['lock_no'],
                                  'deposit_amount': _deposit, 'store_time': order['store_time']})
        _deposit = order.get('deposit_amount', 0)
        conn.close()
        return json_response({'order_id': order['id'], 'order_no': order['order_no'], 'slot_id': order['slot_id'],
                              'compartment_number': order['compartment_number'], 'slot_size': order['slot_size'],
                              'board_no': order['board_no'], 'lock_no': order['lock_no'],
                              'deposit_amount': _deposit, 'store_time': order['store_time']})
    except Exception as e:
        logger.error(f'[retrieve_verify] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/retrieve/confirm', methods=['POST'])
def retrieve_confirm():
    """取包确认 - 继续存或结束"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        action = data.get('action')
        if not all([order_id, action]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if action == 'continue':
            conn.close()
            return json_response({'action': 'continue', 'message': '请继续使用，已为您保留柜格'})
        deposit_amount = order['deposit_amount']
        transaction_id = order['transaction_id']
        orig_status = order['status']
        # 结束订单，更新状态和退款信息
        cursor.execute('UPDATE orders SET status = 3, retrieve_time = NOW(), refund_amount = %s, refund_mark = 1 WHERE id = %s', 
                       (deposit_amount, order_id))
        cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        # 结束订单，保证金退到用户余额（不直接退微信）
        # 防重复：如果订单原状态不是status=2(使用中)，说明已被其他路径处理过，跳过余额更新
        if orig_status == 2:
            _openid = order.get('openid', '') or ''
            _mp_openid = order.get('mp_openid', '') or _openid
            # 统一用 mp_openid 查找用户余额
            if not _mp_openid:
                _mp_openid = _resolve_mp_openid(cursor, mp_openid='', openid=_openid, phone=order['user_phone'])
            _ub = None
            if _mp_openid:
                cursor.execute('SELECT id FROM user_balances WHERE mp_openid = %s', (_mp_openid,))
                _ub = cursor.fetchone()
            if _ub:
                cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE mp_openid = %s',
                               (deposit_amount, deposit_amount, _mp_openid))
            else:
                _wechat_name = ''
                if _mp_openid:
                    cursor.execute("UPDATE user_balances SET balance = balance + %s WHERE mp_openid = %s", (deposit_amount, _mp_openid))
                    if cursor.rowcount == 0:
                        cursor.execute("INSERT INTO user_balances (phone, openid, mp_openid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                       (order['user_phone'], _openid, _mp_openid, _wechat_name, deposit_amount, deposit_amount, datetime.now()))
                else:
                    cursor.execute("UPDATE user_balances SET balance = balance + %s WHERE phone = %s", (deposit_amount, order['user_phone']))
                    if cursor.rowcount == 0:
                        cursor.execute("INSERT INTO user_balances (phone, openid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s)",
                                       (order['user_phone'], _openid, _wechat_name, deposit_amount, deposit_amount, datetime.now()))
            # 插入余额明细，供提现使用
            cursor.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status, source_time) VALUES (%s, %s, %s, 'available', NOW()) ON CONFLICT (order_id) DO NOTHING",
                           (order['user_phone'], order_id, deposit_amount))
        refund_id = 'BALANCE_' + datetime.now().strftime('%Y%m%d%H%M%S')
        refund_success = True
        conn.commit()
        conn.close()
        # 发送寄存结束订阅消息
        _openid = order.get("openid")
        if not _openid:
            try:
                cursor.execute('SELECT 1')  # reset cursor state after ON CONFLICT
                cursor.fetchall()
                cursor.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s', (order['user_phone'],))
                _r2 = cursor.fetchone()
                if _r2:
                    _openid = _r2['openid']
            except:
                pass
        if order.get("user_phone"):
            try:
                from helpers import send_wx_subscribe_message
                subscribe_data = {
                    "amount6": {"value": "¥{:.2f}".format(float(order.get("deposit_amount", 0)))},
                    "time4": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")},
                    "thing7": {"value": "已退还至小程序用户钱包"},
                    "thing2": {"value": "请自行点击此通知消息跳转“我的钱包”提现"}
                }
                send_wx_subscribe_message('', "5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A", subscribe_data, phone=order.get("user_phone"))
            except Exception as e:
                logger.error(f"[retrieve_confirm发送订阅消息失败] {e}")
        if refund_success:
            return json_response({'action': 'end', 'refund_amount': deposit_amount, 'refund_id': refund_id, 'message': f'取包成功，押金¥{deposit_amount}已退至余额'})
        return json_response({'action': 'end', 'refund_amount': 0, 'message': '取包成功，但押金退款失败'}, message='取包成功，退款异常', code=200)
    except Exception as e:
        import traceback; logger.error(f'[retrieve_confirm] 错误: {e}\n{traceback.format_exc()}')
        return json_response(message=str(e), code=500)


# ============================================
# 押金存包流程
# ============================================

@bp.route('/deposit/create-order', methods=['POST'])
def create_deposit_order():
    """创建存包订单并获取微信支付参数"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        slot_size = data.get('slot_size', 'M')
        user_phone = data.get('phone')
        sms_code = data.get('sms_code')
        access_code = data.get('access_code')
        openid = data.get('openid', '')
        unionid = data.get('unionid', '')
        unionid = data.get('unionid', '')
        mp_openid = data.get('mp_openid', '')
        wechat_name = data.get('wechat_name', '')
        if not all([cabinet_id, user_phone]):
            return json_response(message='参数不完整', code=400)
        sms_enabled = get_setting('sms_enabled', 'false').lower() == 'true'
        if sms_enabled and not sms_code:
            return json_response(message='请输入短信验证码', code=400)
        if sms_enabled:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM sms_codes WHERE phone = %s AND code = %s AND expires_at > %s ORDER BY id DESC LIMIT 1',
                           (user_phone, sms_code, datetime.now()))
            sms_record = cursor.fetchone()
            if not sms_record:
                conn.close()
                return json_response(message='短信验证码错误', code=400)
            cursor.execute('DELETE FROM sms_codes WHERE id = %s', (sms_record['id'],))
            conn.commit()
            conn.close()
        conn = get_db()
        cursor = conn.cursor()
        # ===== 设备在线检测：检查 last_heartbeat（5分钟内） =====
        # 备份位置：user.py.bak.onlinecheck
        cursor.execute("SELECT mainboard_device_id, last_heartbeat FROM cabinets WHERE id = %s", (cabinet_id,))
        cab = cursor.fetchone()
        if cab and cab['mainboard_device_id']:
            hb = cab['last_heartbeat']
            if not hb or (datetime.now() - hb).total_seconds() > 300:
                conn.close()
                return json_response(message='设备离线，请稍后再试', code=400)
        # ===== 设备在线检测结束 =====
        # 清理超过15分钟的未支付订单，释放柜门
        from datetime import timedelta
        expire_time = datetime.now() - timedelta(minutes=15)
        cursor.execute('SELECT id, slot_id FROM orders WHERE status = 1 AND store_time < %s', (expire_time,))
        expired = cursor.fetchall()
        for exp in expired:
            if exp['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s AND status = 2', (exp['slot_id'],))
            cursor.execute('DELETE FROM orders WHERE id = %s AND status = 1', (exp['id'],))
        if expired:
            conn.commit()
            logger.info(f'[create_deposit_order] 清理了 {len(expired)} 个过期未支付订单')
        cursor.execute('SELECT * FROM orders WHERE user_phone = %s AND cabinet_id = %s AND status = 1 ORDER BY id DESC LIMIT 1', (user_phone, cabinet_id))
        existing_order = cursor.fetchone()
        if existing_order:
            conn.close()
            logger.info(f'[create_deposit_order] 复用已有未支付订单 {existing_order["id"]}, phone={user_phone}')
            pay_params = get_payment_params(existing_order['id'], existing_order['order_no'], existing_order['deposit_amount'], user_phone, openid, payment_channel_id=existing_order.get('payment_channel_id'))
            return json_response({
                'order_id': existing_order['id'], 'order_no': existing_order['order_no'],
                'access_code': existing_order['access_code'],
                'compartment_number': existing_order['compartment_number'],
                'compartment_label': '',
                'slot_size': existing_order.get('slot_size', ''),
                'deposit_amount': existing_order['deposit_amount'],
                'pay_params': pay_params
            })

        cursor.execute('SELECT cs.*, MAX(o.store_time) as last_used_at FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id LEFT JOIN orders o ON o.slot_id = cs.id WHERE c.id = %s AND cs.status = 1 AND cs.slot_size = %s GROUP BY cs.id ORDER BY CASE WHEN MAX(o.store_time) IS NULL THEN 0 ELSE 1 END, MAX(o.store_time) ASC, cs.slot_number ASC LIMIT 1',
                       (cabinet_id, slot_size))
        slot = cursor.fetchone()
        if not slot:
            cursor.execute('SELECT cs.*, MAX(o.store_time) as last_used_at FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id LEFT JOIN orders o ON o.slot_id = cs.id WHERE c.id = %s AND cs.status = 1 GROUP BY cs.id ORDER BY CASE WHEN MAX(o.store_time) IS NULL THEN 0 ELSE 1 END, MAX(o.store_time) ASC, cs.slot_number ASC LIMIT 1', (cabinet_id,))
            slot = cursor.fetchone()
        if not slot:
            conn.close()
            return json_response(message='暂无可用柜格', code=400)
        if not access_code:
            access_code = generate_access_code()
        elif len(access_code) != 4 or not access_code.isdigit():
            conn.close()
            return json_response(message='取件码必须为4位数字', code=400)
        order_no = generate_order_no()
        cursor.execute('SELECT deposit_amount FROM cabinets WHERE id = %s', (cabinet_id,))
        cab_row = cursor.fetchone()
        deposit_amount = cab_row['deposit_amount'] if cab_row and cab_row['deposit_amount'] else float(get_setting('deposit_amount', '20'))
        # 选择支付渠道
        from helpers import select_payment_channel
        payment_channel = select_payment_channel()
        payment_channel_id = payment_channel['id'] if payment_channel else None
        compartment_display = slot['slot_label'] if 'slot_label' in slot.keys() and slot['slot_label'] else (slot['display_number'] if slot['display_number'] else slot['slot_number'])
        # Mark slot as occupied immediately to prevent double allocation
        cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (slot['id'],))
        _wn2 = chr(39)+chr(39)
        if openid:
            _wnc3 = conn.cursor()
            _wnc3.execute("SELECT wechat_name FROM user_profiles WHERE openid = %s AND wechat_name IS NOT NULL AND wechat_name != "+chr(39)+chr(39)+" LIMIT 1", (openid,))
            _wnr3 = _wnc3.fetchone()
            if _wnr3 and _wnr3[0]:
                _wn2 = _wnr3[0]
        if not _wn2 or _wn2 == chr(39)+chr(39):
            _wnc4 = conn.cursor()
            _wnc4.execute("SELECT wechat_name FROM phone_openids WHERE phone = %s AND wechat_name IS NOT NULL AND wechat_name != "+chr(39)+chr(39)+" LIMIT 1", (user_phone,))
            _wnr4 = _wnc4.fetchone()
            if _wnr4 and _wnr4[0]:
                _wn2 = _wnr4[0]
        _cdo_uid = _resolve_user(cursor, openid=openid, mp_openid=mp_openid or openid, phone=user_phone, unionid=unionid)
        cursor.execute('INSERT INTO orders (order_no, user_phone, slot_id, cabinet_id, compartment_number, access_code, deposit_amount, status, store_time, payment_channel_id, openid, unionid, mp_openid, wechat_name, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                       (order_no, user_phone, slot['id'], cabinet_id, compartment_display, access_code, deposit_amount, datetime.now(), payment_channel_id, openid, unionid, _wn2, _cdo_uid))
        row = cursor.fetchone()
        order_id = row["id"]
        conn.commit()
        conn.close()
        pay_params = get_payment_params(order_id, order_no, deposit_amount, user_phone, openid, payment_channel=payment_channel, payment_channel_id=payment_channel_id)
        return json_response({'order_id': order_id, 'order_no': order_no, 'access_code': access_code,
                              'slot_id': slot['id'], 'cabinet_id': cabinet_id, 'compartment_number': compartment_display, 'compartment_label': slot['slot_label'] if 'slot_label' in slot.keys() and slot['slot_label'] else '',
                              'slot_size': slot['slot_size'], 'deposit_amount': deposit_amount, 'pay_params': pay_params})
    except Exception as e:
        logger.error(f'[create_deposit_order] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/store/pay', methods=['POST'])
def store_pay():
    """存包支付保证金"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if order['status'] in [2, 3, 5]:
            # 已支付的订单，开门指令已由支付回调或首次/store/pay发送，不再重复发送
            conn.close()
            return json_response(data={'order_id': order_id}, message='支付已确认')
        if order['status'] != 1:
            conn.close()
            return json_response(message='订单状态异常', code=400)
        mock_mode = is_mock_mode()
        if mock_mode:
            transaction_id = 'MOCK' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
            cursor.execute('INSERT INTO payments (order_id, type, amount, transaction_id, status) VALUES (%s, 1, %s, %s, 1)',
                           (order_id, order['deposit_amount'], transaction_id))
            cursor.execute('UPDATE orders SET status = 2, transaction_id = %s, pay_time = %s WHERE id = %s',
                           (transaction_id, datetime.now(), order_id))
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (order['slot_id'],))
            conn.commit()
            try:
                cursor2 = get_db().cursor()
                cursor2.execute('SELECT cs.board_no, cs.lock_no, c.mainboard_device_id, c.mainboard_source FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
                cab_info = cursor2.fetchone()
                cursor2.connection.close()
                if cab_info and cab_info['mainboard_device_id']:
                    # 查door_records：已有开门记录则跳过
                    _dr_cur2 = get_db().cursor()
                    _dr_cur2.execute("SELECT id FROM door_records WHERE order_id=%s AND device_id=%s LIMIT 1", (order['order_no'], str(cab_info['mainboard_device_id'])))
                    _dr_exists2 = _dr_cur2.fetchone()
                    _dr_cur2.connection.close()
                    if not _dr_exists2:
                        send_open_lock(str(cab_info['mainboard_device_id']), cab_info['board_no'] or 1, cab_info['lock_no'] or 1, cab_info['mainboard_source'] or 'YBM', order['order_no'])
            except Exception as e:
                logger.error(f'[Mock支付开锁失败] {e}')
            conn.close()
            return json_response({'order_id': order_id, 'order_no': order['order_no'], 'transaction_id': transaction_id, 'mode': 'mock', 'message': '支付成功，请取物存放'})
        else:
            # 使用订单关联的商户号查询支付结果
            from helpers import get_channel_wxpay
            wxpay = None
            _pc_id = order.get('payment_channel_id')
            if _pc_id:
                try:
                    cursor.execute('SELECT * FROM payment_channels WHERE id=%s', (_pc_id,))
                    _ch = cursor.fetchone()
                    if _ch:
                        wxpay, _ = get_channel_wxpay(dict(_ch))
                except Exception as _e:
                    logger.error(f'[deposit_query] 渠道查询异常: {_e}')
            if not wxpay:
                from helpers import select_payment_channel
                _active_ch = select_payment_channel()
                if _active_ch:
                    wxpay, _ = get_channel_wxpay(dict(_active_ch))
            if not wxpay:
                return json_response({'error': '无可用商户号'}, code=500)
            result = wxpay.order_query(out_trade_no=order['order_no'])
            if result.get('trade_state') == 'SUCCESS' or result.get('result_code') == 'SUCCESS':
                transaction_id = result.get('transaction_id')
                cursor.execute('INSERT INTO payments (order_id, type, amount, transaction_id, status) VALUES (%s, 1, %s, %s, 1)',
                               (order_id, order['deposit_amount'], transaction_id))
                # 原子更新：只有status=1时才更新，防止与支付回调重复处理
                cursor.execute('UPDATE orders SET status = 2, transaction_id = %s, pay_time = %s WHERE id = %s AND status = 1',
                               (transaction_id, datetime.now(), order_id))
                we_updated = cursor.rowcount > 0
                if order['slot_id']:
                    cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (order['slot_id'],))
                conn.commit()
                # 只有本次更新了状态才发开门指令（否则回调已发过）
                if we_updated:
                    try:
                        cursor2 = get_db().cursor()
                        cursor2.execute('SELECT cs.board_no, cs.lock_no, c.mainboard_device_id, c.mainboard_source FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
                        cab_info = cursor2.fetchone()
                        cursor2.connection.close()
                        if cab_info and cab_info['mainboard_device_id']:
                            # 查door_records：已有开门记录则跳过
                            _dr_cur2 = get_db().cursor()
                            _dr_cur2.execute("SELECT id FROM door_records WHERE order_id=%s AND device_id=%s LIMIT 1", (order['order_no'], str(cab_info['mainboard_device_id'])))
                            _dr_exists2 = _dr_cur2.fetchone()
                            _dr_cur2.connection.close()
                            if not _dr_exists2:
                                send_open_lock(str(cab_info['mainboard_device_id']), cab_info['board_no'] or 1, cab_info['lock_no'] or 1, cab_info['mainboard_source'] or 'YBM', order['order_no'])
                    except Exception as e:
                        logger.error(f'[WechatPay开锁失败] {e}')
                conn.close()
                return json_response({'order_id': order_id, 'order_no': order['order_no'], 'transaction_id': transaction_id, 'mode': 'wechat', 'message': '支付成功，请取物存放'})
            else:
                conn.close()
                return json_response(message='支付查询失败，请稍后重试', code=400)

    except Exception as e:
        logger.error(f'[store_pay] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/store/confirm', methods=['POST'])
def store_confirm():
    """存包确认"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE orders SET status = 2 WHERE id = %s AND status = 2', (order_id,))
        conn.commit()
        conn.close()
        return json_response(message='存包确认成功')
    except Exception as e:
        logger.error(f'[store_confirm] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# H5存包
# ============================================

@bp.route('/h5/store', methods=['POST'])
def h5_store():
    """H5存包API"""
    try:
        data = request.get_json()
        phone = str(data.get('phone') or data.get('user_phone') or '').strip()
        pwd = data.get('pwd', '').strip()
        device = data.get('device', '')
        if not phone or len(phone) < 11:
            return json_response(message='请输入正确的手机号', code=400)
        if not pwd or len(pwd) < 4:
            return json_response(message='请输入至少4位密码', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id, mainboard_device_id, mainboard_source, deposit_amount, group_id, cabinet_code, name, last_heartbeat FROM cabinets WHERE mainboard_device_id = %s', (device,))
        cabinet = cursor.fetchone()
        if not cabinet:
            cursor.execute('SELECT id, mainboard_device_id, mainboard_source, deposit_amount, group_id, cabinet_code, name, last_heartbeat FROM cabinets WHERE cabinet_code = %s', (device,))
            cabinet = cursor.fetchone()
        if not cabinet:
            try:
                cursor.execute('SELECT id, mainboard_device_id, mainboard_source, deposit_amount, group_id, cabinet_code, name, last_heartbeat FROM cabinets WHERE id = %s', (int(device),))
                cabinet = cursor.fetchone()
            except (ValueError, TypeError):
                pass
        if not cabinet:
            conn.close()
            return json_response(message='设备不存在', code=404)
        # 检查设备是否在线（5分钟内有心跳即认为在线）
        dev_id = cabinet['mainboard_device_id'] or ''
        if dev_id:
            hb = cabinet.get('last_heartbeat')
            if not hb or (datetime.now() - hb).total_seconds() > 300:
                conn.close()
                return json_response(message='设备离线，请稍后再试', code=400)
        cabinet_id = cabinet['id']
        # 检查网点是否允许跳转小程序
        cursor.execute("SELECT l.allow_h5_to_mp FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s", (cabinet_id,))
        _lr = cursor.fetchone()
        need_redirect = bool(_lr and _lr['allow_h5_to_mp'])
        cursor.execute('SELECT cs.*, MAX(o.store_time) as last_used_at FROM cabinet_slots cs LEFT JOIN orders o ON o.slot_id = cs.id WHERE cs.cabinet_id = %s AND cs.status = 1 GROUP BY cs.id ORDER BY CASE WHEN MAX(o.store_time) IS NULL THEN 0 ELSE 1 END, MAX(o.store_time) ASC, cs.slot_number ASC LIMIT 1', (cabinet_id,))
        slot = cursor.fetchone()
        if not slot:
            conn.close()
            return json_response(message='暂无可用柜格', code=303)
        cursor.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (slot['id'],))
        order_no = 'ORD' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=4))
        deposit = cabinet['deposit_amount'] or 0
        openid = data.get('openid', '')
        unionid = data.get('unionid', '') or ''
        wechat_name = data.get('wechat_name', '') or ''
        # 选择支付渠道（轮转）
        payment_channel = select_payment_channel()
        payment_channel_id = payment_channel['id'] if payment_channel else None
        _wn3 = wechat_name if wechat_name else chr(39)+chr(39)
        if not _wn3 and openid:
            _wnc5 = conn.cursor()
            _wnc5.execute("SELECT wechat_name FROM user_profiles WHERE openid = %s AND wechat_name IS NOT NULL AND wechat_name != "+chr(39)+chr(39)+" LIMIT 1", (openid,))
            _wnr5 = _wnc5.fetchone()
            if _wnr5 and _wnr5[0]:
                _wn3 = _wnr5[0]
        if not _wn3 or _wn3 == chr(39)+chr(39):
            _wnc6 = conn.cursor()
            _wnc6.execute("SELECT wechat_name FROM phone_openids WHERE phone = %s AND wechat_name IS NOT NULL AND wechat_name != "+chr(39)+chr(39)+" LIMIT 1", (phone,))
            _wnr6 = _wnc6.fetchone()
            if _wnr6 and _wnr6[0]:
                _wn3 = _wnr6[0]
        # 解析 user_id
        _h5_uid = _resolve_user(cursor, openid=openid, phone=phone, unionid=unionid)
        cursor.execute('INSERT INTO orders (order_no, user_phone, slot_id, cabinet_id, compartment_number, access_code, deposit_amount, status, store_time, group_id, cabinet_code, cabinet_name, slot_size, payment_channel_id, openid, unionid, wechat_name, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                       (order_no, phone, slot['id'], cabinet_id, slot['slot_number'], pwd, deposit, datetime.now(), cabinet['group_id'], cabinet['cabinet_code'], cabinet['name'], slot['slot_size'], payment_channel_id, openid, unionid, _wn3, _h5_uid))
        row = cursor.fetchone()
        order_id = row["id"]
        conn.commit()
        result = {'order_id': order_id, 'order_no': order_no, 'need_redirect': need_redirect, 'slot_number': str(slot['slot_number']), 'pwd': pwd, 'deposit': str(deposit), 'cabinet_id': cabinet_id, 'cabinet_device_id': cabinet['mainboard_device_id'], 'board_no': 1}
        try:
            pay_params = get_payment_params(order_id, order_no, deposit, phone, openid, payment_channel=payment_channel, payment_channel_id=payment_channel_id)
            result['pay_params'] = pay_params
            if pay_params.get('mode') == 'error':
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (slot['id'],))
                cursor.execute('DELETE FROM orders WHERE id = %s', (order_id,))
                conn.commit()
                logger.info(f'[h5_store] pay failed, released slot, deleted order {order_id}')
        except Exception as e:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (slot['id'],))
            cursor.execute('DELETE FROM orders WHERE id = %s', (order_id,))
            conn.commit()
            logger.error(f'[h5_store get_payment_params] {e}, released slot')
        conn.close()
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[h5_store] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 押金取物/续存/结束
# ============================================

@bp.route('/deposit/retrieve', methods=['POST'])
def deposit_retrieve():
    """取物验证"""
    try:
        data = request.get_json()
        cabinet_code = data.get('cabinet_code')
        group_code = data.get('group_code')
        phone = data.get('phone')
        access_code = data.get('access_code')
        openid = data.get('openid', '')
        unionid = data.get('unionid', '')
        if not all([phone, access_code]):
            return json_response(message='参数不完整', code=400)
        if len(access_code) != 4 or not access_code.isdigit():
            return json_response(message='取物码必须为4位数字', code=400)
        if not phone or len(phone) != 11 or not phone.startswith('1'):
            return json_response(message='请输入正确的手机号', code=400)
        conn = get_db()
        cursor = conn.cursor()
        if group_code:
            cursor.execute('SELECT id FROM cabinet_groups WHERE group_code = %s', (group_code,))
            group = cursor.fetchone()
            if not group:
                conn.close()
                return json_response(message='柜组不存在', code=404)
            cursor.execute('SELECT o.*, cs.slot_number, COALESCE(cs.slot_size, o.slot_size) as slot_size, c.cabinet_code, c.name as cabinet_name, cs.board_no, cs.lock_no, c.mainboard_device_id FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE c.group_id = %s AND o.user_phone = %s AND o.access_code = %s AND o.status = 2 ORDER BY o.id DESC',
                           (group['id'], phone, access_code))
        else:
            if not cabinet_code:
                conn.close()
                return json_response(message='柜体或柜组编号不能为空', code=400)
            cursor.execute('SELECT id FROM cabinets WHERE cabinet_code = %s', (cabinet_code,))
            cabinet = cursor.fetchone()
            if not cabinet:
                conn.close()
                return json_response(message='柜体不存在', code=404)
            cursor.execute('SELECT o.*, cs.slot_number, COALESCE(cs.slot_size, o.slot_size) as slot_size, cs.board_no, cs.lock_no, c.mainboard_device_id FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE o.cabinet_id = %s AND o.user_phone = %s AND o.access_code = %s AND o.status = 2 ORDER BY o.id DESC',
                           (cabinet['id'], phone, access_code))
        orders = cursor.fetchall()
        if not orders:
            conn.close()
            return json_response(message='手机号或取物码错误', code=400)
        conn.close()
        # 遍历所有订单，逐个开门并结束
        for order in orders:
            order_dict = dict(order)
            try:
                device_id = order_dict.get('mainboard_device_id')
                board_no = order_dict.get('board_no') or ''
                lock_no = order_dict.get('lock_no') or ''
                if device_id and board_no and lock_no:
                    send_open_lock(device_id, board_no, lock_no, order_id=order_dict.get('order_no', str(order_dict['id'])))
                    logger.info(f'[取物开门] device={device_id}, board={board_no}, lock={lock_no}, order_id={order_dict["id"]}')
                    # 取物即结束订单
                    conn2 = get_db()
                    c2 = conn2.cursor()
                    c2.execute("UPDATE orders SET status=3, retrieve_time=NOW(), refund_amount=%s, refund_mark=1 WHERE id=%s AND status=2",
                              (order_dict['deposit_amount'], order_dict['id']))
                    if order_dict.get('slot_id'):
                        c2.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s", (order_dict['slot_id'],))
                    c2.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s,%s,%s,'available') ON CONFLICT (order_id) DO NOTHING",
                              (order_dict['user_phone'], order_dict['id'], order_dict['deposit_amount']))
                    conn2.commit()
                    conn2.close()
                    # Save values for notification (defensive copy)
                    # 通过手机号查询 mp_openid
                    _n_phone = (order_dict or {}).get('user_phone', '')
                _noid = ''
                if _n_phone:
                    try:
                        _n_conn = get_db()
                        _n_cur = _n_conn.cursor()
                        _n_cur.execute("SELECT mp_openid FROM phone_openids WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (_n_phone,))
                        _n_row = _n_cur.fetchone()
                        _n_conn.close()
                        if _n_row and _n_row[0]:
                            _noid = _n_row[0]
                    except:
                        pass
                _n_amt = (order_dict or {}).get('deposit_amount', 0)
                if _noid:
                    try:
                        from helpers import send_wx_subscribe_message
                        _now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        _nsd = {
                            'amount6': {'value': '¥{:.2f}'.format(_n_amt)},
                            'time4': {'value': _now},
                            'thing7': {'value': '已退还至小程序用户钱包'},
                            'thing2': {'value': '请自行点击此通知消息跳转“我的钱包”提现'}
                        }
                        send_wx_subscribe_message(_noid, '5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A', _nsd, phone=_n_phone)
                    except Exception as _ne:
                        logger.error('[deposit_retrieve_notify1] '+ str(_ne))
                else:
                    logger.warning(f'[取物开门] 缺少设备/主板/锁号: device={device_id}, board={board_no}, lock={lock_no}')
            except Exception as open_err:
                logger.error(f'[retrieve] 处理订单{order_dict.get("id")}失败: ' + str(open_err))
            logger.info(f"[retrieve] order_id={order_dict['id']} 处理完成")
        return json_response({'order_id': order_dict['id'], 'order_no': order_dict['order_no'],
                              'cabinet_id': order_dict['cabinet_id'], 'cabinet_code': order_dict.get('cabinet_code', cabinet_code),
                              'slot_id': order['slot_id'], 'compartment_number': order['slot_number'],
                              'slot_size': order['slot_size'], 'deposit_amount': order['deposit_amount'],
                              'store_time': order['store_time'], 'group_code': group_code})
    except Exception as e:
        logger.error(f'[deposit_retrieve] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/deposit/continue-storage', methods=['POST'])
def deposit_continue_storage():
    """继续存放"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order or order['status'] != 2:
            conn.close()
            return json_response(message='订单不存在或状态异常', code=404 if not order else 400)
        cursor.execute('INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time, retrieve_time) VALUES (%s, %s, %s, %s, 2, %s, %s)',
                       (order['cabinet_id'], order['compartment_number'], order['user_phone'], order['access_code'], order['store_time'], datetime.now()))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        conn.commit()
        conn.close()
        return json_response({'message': '继续存放成功', 'order_id': order_id, 'deposit_amount': order['deposit_amount']})
    except Exception as e:
        logger.error(f'[continue_storage] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/deposit/end-storage', methods=['POST'])
def deposit_end_storage():
    """结束取物"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        from config import DATABASE_URL as _DU
        conn = psycopg2.connect(_DU, connect_timeout=10)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('SELECT o.*, cs.slot_number, cs.board_no, cs.lock_no, c.cabinet_code, c.mainboard_device_id FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order or order['status'] != 2:
            conn.close()
            return json_response(message='订单不存在或状态异常', code=404 if not order else 400)
        refund_amount = order['deposit_amount']
        compartment_number = order['slot_number'] or order['compartment_number']
        cursor.execute('INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time, retrieve_time) VALUES (%s, %s, %s, %s, 1, %s, %s)',
                       (order['cabinet_id'], order['compartment_number'], order['user_phone'], order['access_code'], order['store_time'], datetime.now()))
        # 根据网点退款模式处理
        cursor.execute('SELECT l.withdraw_mode, l.show_refunding_status FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s', (order['cabinet_id'],))
        loc_row = cursor.fetchone()
        withdraw_mode = loc_row['withdraw_mode'] if loc_row else 'auto_approve'
        
        # Merchant-phase hold check: even in auto_approve locations,
        # if merchant is in protection period, hold for manual review
        if withdraw_mode == 'auto_approve':
            _order_openid = order.get('openid', '') or ''
            _order_phone = order['user_phone']
            _needs_hold = check_withdraw_auto_approve(openid=_order_openid, phone=_order_phone)
            if _needs_hold:
                withdraw_mode = 'manual'
            else:
                mark_user_withdraw(openid=_order_openid, phone=_order_phone)
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        # 结束订单，保证金退到用户余额
        new_status = 3
        cursor.execute('UPDATE orders SET status = %s, retrieve_time = NOW(), refund_amount = %s, refund_mark = 1 WHERE id = %s', 
                       (new_status, refund_amount, order_id))
        _openid = order.get('openid', '') or ''
        _unionid = order.get('unionid', '') or ''
        _mp_openid = order.get('mp_openid', '') or _openid
        # 统一用 mp_openid 查找用户余额
        if not _mp_openid:
            _mp_openid = _resolve_mp_openid(cursor, mp_openid='', openid=_openid, phone=order['user_phone'])
        _ub = None
        if _mp_openid:
            cursor.execute('SELECT id FROM user_balances WHERE mp_openid = %s', (_mp_openid,))
            _ub = cursor.fetchone()
        if _ub:
            cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE mp_openid = %s',
                           (refund_amount, refund_amount, _mp_openid))
        else:
            # 查询wechat_name（忽略游标异常，避免阻止订单结束）
            _wechat_name2 = ''
            try:
                _c2 = conn.cursor()
                if _openid:
                    _c2.execute("SELECT wechat_name FROM user_profiles WHERE openid = %s AND wechat_name IS NOT NULL AND wechat_name != '' LIMIT 1", (_openid,))
                    _wn_row3 = _c2.fetchone()
                    if _wn_row3:
                        _wechat_name2 = _wn_row3['wechat_name']
                if not _wechat_name2:
                    _c2.execute("SELECT wechat_name FROM phone_openids WHERE phone = %s AND wechat_name IS NOT NULL AND wechat_name != '' LIMIT 1", (order['user_phone'],))
                    _wn_row4 = _c2.fetchone()
                    if _wn_row4:
                        _wechat_name2 = _wn_row4['wechat_name']
            except Exception:
                pass
            if _mp_openid:
                cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE mp_openid = %s', (refund_amount, refund_amount, _mp_openid))
                if cursor.rowcount == 0:
                    # 按mp_openid没找到，按phone找已有记录；但检查该记录是否属于其他微信
                    cursor.execute("SELECT id, mp_openid FROM user_balances WHERE phone = %s LIMIT 1", (order['user_phone'],))
                    _exist = cursor.fetchone()
                    if _exist:
                        _exist_id, _exist_mp = _exist
                        # 如果已有记录的mp_openid跟自己不同且不为空，说明是另一个微信，不共享余额
                        if _exist_mp and _exist_mp != _mp_openid and _exist_mp != order.get("openid", ""):
                            cursor.execute('INSERT INTO user_balances (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                                           (order['user_phone'], _openid, _unionid, _mp_openid, _wechat_name2, refund_amount, refund_amount, datetime.now()))
                        else:
                            cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s, mp_openid = %s WHERE id = %s', (refund_amount, refund_amount, _mp_openid, _exist_id))
                    else:
                        cursor.execute('INSERT INTO user_balances (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                                       (order['user_phone'], _openid, _unionid, _mp_openid, _wechat_name2, refund_amount, refund_amount, datetime.now()))
            else:
                # 没有mp_openid，按phone找已有记录（可能来自H5扫码存包）
                cursor.execute("SELECT id, mp_openid FROM user_balances WHERE phone = %s LIMIT 1", (order['user_phone'],))
                _exist = cursor.fetchone()
                if _exist:
                    _exist_id, _exist_mp = _exist
                    # 如果记录属于其他微信且有openid，不共享
                    if _exist_mp and _exist_mp != order.get("openid", ""):
                        cursor.execute('INSERT INTO user_balances (phone, openid, unionid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                                       (order['user_phone'], _openid, _unionid, _wechat_name2, refund_amount, refund_amount, datetime.now()))
                    else:
                        cursor.execute('UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE id = %s', (refund_amount, refund_amount, _exist_id))
                else:
                    cursor.execute('INSERT INTO user_balances (phone, openid, unionid, wechat_name, balance, total_deposited, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                                   (order['user_phone'], _openid, _unionid, _wechat_name2, refund_amount, refund_amount, datetime.now()))
        # 写入余额明细（灰度：新提现逻辑）
        cursor.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING", (order['user_phone'], order_id, refund_amount))
        cursor.execute("UPDATE orders SET logical_mark='end' WHERE id=%s", (order_id,))
        conn.commit()
        conn.close()
        # Send open_lock via send_open_lock (includes door_records)
        try:
            from helpers import send_open_lock
            device_id = order['mainboard_device_id']
            send_open_lock(device_id, order['board_no'] or 1, order['lock_no'] or 1, order_id=str(order_id), protocol=order.get('mainboard_source') or 'YBM', slot_number=order.get('compartment_number'))
            logger.info(f'[end_storage] send_open_lock called: device={device_id}, board={order["board_no"]}, lock={order["lock_no"]}')
        except Exception as we:
            logger.error(f'[end_storage] send_open_lock失败: {we}')
        # 发送寄存结束订阅消息
        _openid = order.get("openid")
        if not _openid:
            _nconn = None
            try:
                from config import DATABASE_URL as _NURL
                _nconn = psycopg2.connect(_NURL, connect_timeout=5)
                _ncur = _nconn.cursor()
                # first check user_balances mp_openid (has correct mini-program openid)
                # [FIX-20260716] 必须排除 oLhbm2 前缀（公众号openid），只保留 oWrA8 前缀的小程序openid
                _ncur.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' AND mp_openid NOT LIKE 'oLhbm2%%' ORDER BY id DESC LIMIT 1", (order['user_phone'],))
                _nrow = _ncur.fetchone()
                logger.info(f"[end_storage_debug] user_balances mp_openid query: {_nrow}")
                if _nrow and _nrow[0]:
                    _openid = _nrow[0]
                else:
                    # not found in user_balances, try phone_openids
                    _ncur.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s ORDER BY updated_at DESC LIMIT 1', (order['user_phone'],))
                    _nrow = _ncur.fetchone()
                    logger.info(f"[end_storage_debug] phone_openids query result: {_nrow}")
                    if _nrow and _nrow[0]:
                        _openid = _nrow[0]
                _ncur.close()
                _nconn.close()
            except Exception as _ne:
                logger.error(f'[end_storage_notify] lookup failed: {_ne}')
                pass
        logger.info(f"[end_storage_debug] Found openid={_openid}, sending notification for order={order_id}")
        if _openid:
            try:
                from helpers import send_wx_subscribe_message
                # 发送押金退还通知
                subscribe_data = {"amount6": {"value": "¥{:.2f}".format(float(order.get("deposit_amount", 0)))}, "time4": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")}, "thing7": {"value": "已退还至小程序用户钱包"}, "thing2": {"value": "请自行点击此通知消息跳转“我的钱包”提现"}}
                send_wx_subscribe_message('', "5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A", subscribe_data, phone=order.get("user_phone"), page="pages/mine/mine")

                logger.info(f"[deposit_end_storage] 订阅消息已发送: order={order_id}")
            except Exception as e:
                logger.error(f"[deposit_end_storage发送订阅消息失败] {e}")

        if new_status == 3 or new_status == 4:
            refund_id = 'BALANCE_' + datetime.now().strftime('%Y%m%d%H%M%S')
            return json_response({'message': '取物完成，保证金已退至余额', 'order_id': order_id, 'refund_amount': refund_amount, 'refund_id': refund_id, 'compartment_number': compartment_number})
        else:
            return json_response({'message': '取物完成，退款异常，请联系客服', 'order_id': order_id, 'refund_amount': 0, 'compartment_number': compartment_number})
    except Exception as e:
        import traceback
        logger.error(f'[end_storage] 错误: {e}')
        logger.error(f'[end_storage] 堆栈: {traceback.format_exc()}')
        return json_response(message=str(e), code=500)


@bp.route('/deposit/open-slot', methods=['POST'])
def deposit_open_slot():
    """开锁指令"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        cursor.execute('SELECT cs.*, c.cabinet_code FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id WHERE cs.id = %s', (order['slot_id'],))
        slot = cursor.fetchone()
        conn.close()
        if not slot:
            return json_response(message='柜格不存在', code=404)
        return json_response({'message': '开锁指令已发送', 'cabinet_code': slot['cabinet_code'], 'slot_number': slot['slot_number']})
    except Exception as e:
        logger.error(f'[deposit_open_slot] 错误: {e}')
        return json_response(message=str(e), code=500)



@bp.route('/deposit/mid-retrieve', methods=['POST'])
def deposit_mid_retrieve():
    """中途取物开锁"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, cs.slot_number, COALESCE(cs.slot_size, o.slot_size) as slot_size, cs.board_no, cs.lock_no, c.cabinet_code, c.mainboard_device_id FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if order['status'] != 2:
            conn.close()
            return json_response(message='订单状态不允许中途取物', code=400)
        # Record mid-retrieve
        cursor.execute('INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time, retrieve_time) VALUES (%s, %s, %s, %s, 2, %s, %s)',
                       (order['cabinet_id'], order['compartment_number'], order['user_phone'], order['access_code'], order['store_time'], datetime.now()))
        cursor.execute("UPDATE orders SET logical_mark='mid' WHERE id=%s", (order_id,))
        conn.commit()
        conn.close()
        # Send open_lock via send_open_lock (includes door_records)
        try:
            from helpers import send_open_lock
            device_id = order['mainboard_device_id']
            send_open_lock(device_id, order['board_no'] or 1, order['lock_no'] or 1, order_id=str(order_id), protocol=order.get('mainboard_source') or 'YBM', slot_number=order.get('compartment_number'))
            logger.info(f'[中途取物] send_open_lock called: device={device_id}, board={order["board_no"]}, lock={order["lock_no"]}')
        except Exception as we:
            logger.error(f'[中途取物] send_open_lock失败: {we}')
        logger.info(f'[中途取物] order_id={order_id}, compartment={order["slot_number"]}')
        return json_response({'message': '柜门已打开', 'order_id': order_id, 'compartment_number': order['slot_number'], 'cabinet_code': order['cabinet_code']})
    except Exception as e:
        logger.error(f'[mid_retrieve] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/order/<int:order_id>', methods=['GET'])
def get_user_order_detail(order_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, c.cabinet_code, c.name, c.location_id, l.name as location_name, l.withdraw_enabled, cs.slot_number, cs.slot_size as slot_size_name FROM orders o LEFT JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        cursor.execute('SELECT status FROM withdrawal_records WHERE order_id = %s ORDER BY id DESC LIMIT 1', (order_id,))
        wr = cursor.fetchone()
        cursor.execute('SELECT dr.* FROM door_records dr WHERE dr.order_id = %s ORDER BY dr.create_time', (str(order_id),))
        door_records = [dict(dr) for dr in cursor.fetchall()]
        conn.close()
        d = dict(order)
        status_map = {1: 'storing', 2: 'storing', 3: 'retrieved', 4: 'retrieved', 5: 'timeout', 6: 'retrieved'}
        status_str = status_map.get(d.get('status', 1), 'storing')
        wd_status = None
        if wr:
            wd_map = {0: 'pending', 1: 'pending', 2: 'approved', 3: 'failed'}
            wd_status = wd_map.get(wr['status'])
        result = {'order_id': d['id'], 'order_no': d.get('order_no', ''), 'cabinet_name': d.get('cabinet_name') or d.get('cabinet_code', '寄存柜'), 'cabinet_id': d.get('cabinet_id'), 'location': d.get('location_name', ''), 'slot_id': d.get('slot_id'), 'slot_no': d.get('slot_number') or d.get('slot_id'), 'slot_size': d.get('slot_size_name') or d.get('slot_size', 0), 'deposit_time': d.get('store_time', ''), 'retrieve_time': d.get('retrieve_time', ''), 'end_time': d.get('retrieve_time', ''), 'status': status_str, 'phone': d.get('user_phone', ''), 'access_code': d.get('access_code', ''), 'deposit': d.get('deposit_amount', 10) or 10, 'fee': 0, 'refund_amount': (d.get('refund_amount', 0) or 0) if d.get('status') == 4 else 0, 'withdraw_enabled': bool(d.get('withdraw_enabled', 0)), 'withdraw_status': wd_status, 'door_records': door_records}
        return json_response(data=result)
    except Exception as e:
        logger.error('[order_detail] error: ' + str(e))
        return json_response(message=str(e), code=500)

@bp.route('/order/reopen', methods=['POST'])
def order_reopen():
    """重新开锁 - 不管订单状态都能开"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='订单ID不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, cs.slot_number, COALESCE(cs.slot_size, o.slot_size) as slot_size, cs.board_no, cs.lock_no, c.cabinet_code, c.mainboard_device_id FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        compartment = order['slot_number']
        conn.close()
        # Send open_lock via send_open_lock (writes to DB + WS + HTTP poll fallback)
        try:
            from helpers import send_open_lock
            device_id = order['mainboard_device_id']
            send_open_lock(device_id, order['board_no'] or '', order['lock_no'] or '', order_id=order.get('order_no', str(order_id)), slot_number=order.get('compartment_number'))
            logger.info(f'[重新开锁] send_open_lock called: device={device_id}, board={order["board_no"]}, lock={order["lock_no"]}')
        except Exception as we:
            logger.error(f'[重新开锁] send_open_lock失败: {we}')
        # send_open_lock 已自动写入 door_records，无需重复插入
        logger.info(f'[重新开锁] order_id={order_id}, compartment={compartment}')
        return json_response({'message': '开门指令已发送', 'order_id': order_id, 'compartment_number': compartment, 'cabinet_code': order['cabinet_code']})
    except Exception as e:
        logger.error(f'[order_reopen] 错误: {e}')
        return json_response(message=str(e), code=500)

# ============================================
# 短信验证码
# ============================================

@bp.route('/sms/send', methods=['POST'])
def sms_send():
    """发送短信验证码"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        if not openid and not phone:
            return json_response(message='请先登录', code=400)
        if get_setting('sms_enabled', 'false').lower() != 'true':
            return json_response(message='短信验证未开启')
        code = generate_sms_code()
        expires_at = datetime.now() + timedelta(minutes=5)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM sms_codes WHERE phone = %s', (phone,))
        cursor.execute('INSERT INTO sms_codes (phone, code, expires_at) VALUES (%s, %s, %s)', (phone, code, expires_at))
        conn.commit()
        conn.close()
        logger.info(f'[短信模拟] 发送给 {phone} 的验证码: {code}')
        return json_response(message='验证码已发送', data={'expires_in': 300})
    except Exception as e:
        logger.error(f'[sms_send] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/sms/verify', methods=['POST'])
def sms_verify():
    """验证短信验证码"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        code = data.get('code')
        if not all([phone, code]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sms_codes WHERE phone = %s AND code = %s AND expires_at > %s ORDER BY id DESC LIMIT 1', (phone, code, datetime.now()))
        record = cursor.fetchone()
        if not record:
            conn.close()
            return json_response(message='验证码错误或已过期', code=400)
        cursor.execute('DELETE FROM sms_codes WHERE id = %s', (record['id'],))
        conn.commit()
        conn.close()
        return json_response(message='验证成功')
    except Exception as e:
        logger.error(f'[sms_verify] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 通用接口
# ============================================

@bp.route('/cabinet/<int:cabinet_id>/status', methods=['GET'])
def cabinet_status(cabinet_id):
    """柜体状态"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cabinets WHERE id = %s', (cabinet_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在', code=404)
        cursor.execute('SELECT * FROM cabinet_slots WHERE cabinet_id = %s', (cabinet_id,))
        slots = cursor.fetchall()
        conn.close()
        return json_response({
            'cabinet_id': cabinet_id, 'deposit_amount': cabinet['deposit_amount'] or 20,
            'total': len(slots), 'available': len([s for s in slots if s['status'] == 1]),
            'occupied': len([s for s in slots if s['status'] == 2]),
            'fault': len([s for s in slots if s['status'] == 3]),
            'locked': len([s for s in slots if s['status'] == 4]),
            'slots': [{'id': s['id'], 'number': s['display_number'] if s['display_number'] else s['slot_number'], 'size': s['slot_size'], 'status': s['status']} for s in slots]
        })
    except Exception as e:
        logger.error(f'[cabinet_status] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/qrcode', methods=['GET'])
def generate_qrcode():
    """生成二维码"""
    try:
        url = request.args.get('url', '')
        if not url:
            return json_response(message='缺少url参数', code=400)
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        img_b64 = base64.b64encode(buffer.getvalue()).decode()
        return json_response({'qrcode': f"data:image/png;base64,{img_b64}", 'url': url})
    except Exception as e:
        logger.error(f'[qrcode] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet/<int:cabinet_id>/qrcode', methods=['GET'])
def cabinet_qrcode(cabinet_id):
    """生成柜体二维码"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT cabinet_code FROM cabinets WHERE id = %s', (cabinet_id,))
        cabinet = cursor.fetchone()
        conn.close()
        if not cabinet:
            return json_response(message='柜体不存在', code=404)
        qr_data = f"locker://open%scode={cabinet['cabinet_code']}"
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        img_b64 = base64.b64encode(buffer.getvalue()).decode()
        return json_response({'qrcode': f"data:image/png;base64,{img_b64}", 'cabinet_code': cabinet['cabinet_code']})
    except Exception as e:
        logger.error(f'[cabinet_qrcode] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/order/<int:order_id>/pay-status', methods=['GET'])
def get_pay_status(order_id):
    """查询订单支付状态"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        conn.close()
        if not order:
            return json_response(message='订单不存在', code=404)
        status_map = {1: '待支付', 2: '使用中', 3: '已结算', 4: '已退款', 5: '已取消', 6: '退款异常'}
        # 主动查询微信支付状态兜底（如果本地状态是待支付，去微信确认）
        if order['status'] == 1 and order['payment_channel_id']:
            try:
                ch_row2 = None
                conn2 = get_db()
                cur2 = conn2.cursor()
                cur2.execute('SELECT * FROM payment_channels WHERE id = %s', (order['payment_channel_id'],))
                ch_row2 = cur2.fetchone()
                conn2.close()
                if ch_row2:
                    from helpers import get_channel_wxpay as _gchw
                    wxpay_inst2, ch_type2 = _gchw(dict(ch_row2))
                    if wxpay_inst2 and ch_type2 == 'wechat':
                        qr = wxpay_inst2.order_query(out_trade_no=order['order_no'])
                        if qr.get('trade_state') == 'SUCCESS':
                            # 微信已支付但回调丢失，手动更新
                            cur3 = conn.cursor() if False else None
                            conn3 = get_db()
                            cur3 = conn3.cursor()
                            txn_id = qr.get('transaction_id', '')
                            cur3.execute('UPDATE orders SET status = 2, transaction_id = %s, pay_time = %s WHERE id = %s',
                                         (txn_id, datetime.now(), order['id']))
                            if order['slot_id']:
                                cur3.execute('UPDATE cabinet_slots SET status = 2 WHERE id = %s', (order['slot_id'],))
                            try:
                                pass  # 支付确认不再增加余额
                            except: pass
                            cur3.execute('INSERT INTO payments (order_id, type, amount, transaction_id, status) VALUES (%s, 1, %s, %s, 1)',
                                         (order['id'], order['deposit_amount'], txn_id))
                            conn3.commit()
                            conn3.close()
                            # 开锁（H5支付成功时直接开门）
                            try:
                                from helpers import send_open_lock
                                _lc = get_db()
                                _lcu = _lc.cursor()
                                _lcu.execute("SELECT c.mainboard_device_id, c.mainboard_source, cs.board_no, cs.lock_no FROM cabinets c LEFT JOIN cabinet_slots cs ON cs.id = %s WHERE c.id = %s", (order["slot_id"], order["cabinet_id"]))
                                _ci = _lcu.fetchone()
                                _lc.close()
                                if _ci and _ci["mainboard_device_id"]:
                                    send_open_lock(
                                        str(_ci["mainboard_device_id"]),
                                        _ci["board_no"] or 1,
                                        _ci["lock_no"] or int("".join(filter(str.isdigit, str(order.get("compartment_number","") or "1")))) or 1,
                                        _ci["mainboard_source"] or "QM",
                                        order["order_no"]
                                    )
                                    logger.info("[pay-status] 开锁指令已发送: order=" + str(order["id"]))
                            except Exception as _el:
                                logger.error("[pay-status] 开锁失败: " + str(_el))
                            try:
                                update_channel_stats(order.get("payment_channel_id"), order.get("deposit_amount"))
                            except Exception:
                                pass
                            # 保存原始订单数据用于通知
                            _ps_order_id = order['id']
                            _ps_user_phone = order.get('user_phone', '')
                            _ps_openid = order.get('openid', '')
                            _ps_compartment = order.get('compartment_number', '')
                            _ps_deposit = order.get('deposit_amount', '')
                            _ps_slot_id = order.get('slot_id')
                            _ps_cabinet_id = order.get('cabinet_id')
                            order = {'id': order['id'], 'order_no': order['order_no'], 'status': 2,
                                     'transaction_id': txn_id, 'pay_time': datetime.now(),
                                     'refund_id': order['refund_id'], 'refund_time': order['refund_time']}
                            logger.info(f'[pay-status] 主动查询发现订单{order["id"]}已支付，已更新')
                            # 寄存成功通知已在 payment.py 支付回调中发送，此处不再重复发送
            except Exception as e2:
                logger.error(f'[pay-status] 主动查询微信失败: {e2}')
        return json_response({'order_id': order['id'], 'order_no': order['order_no'], 'status': order['status'],
                              'status_text': status_map.get(order['status'], '未知'), 'transaction_id': order['transaction_id'],
                              'pay_time': order['pay_time'], 'refund_id': order['refund_id'], 'refund_time': order['refund_time']})
    except Exception as e:
        logger.error(f'[get_pay_status] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet/screen-info', methods=['GET'])
def cabinet_screen_info():
    """柜体屏幕显示信息"""
    try:
        cabinet_code = request.args.get('cabinet_code')
        group_code = request.args.get('group_code')
        if not cabinet_code and not group_code:
            return json_response(message='柜组编号不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        if group_code:
            cursor.execute('SELECT cg.*, l.name as location_name, l.address as location_address, m.name as merchant_name FROM cabinet_groups cg LEFT JOIN locations l ON cg.location_id = l.id LEFT JOIN merchants m ON l.merchant_id = m.id WHERE cg.group_code = %s', (group_code,))
            group = cursor.fetchone()
            if not group:
                conn.close()
                return json_response(message='柜组不存在', code=404)
            cursor.execute('SELECT COUNT(cs.id) as total_slots, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots FROM cabinets c JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE c.group_id = %s', (group['id'],))
            slot_stats = cursor.fetchone()
            system_name = get_setting('system_name', '智能寄存柜')
            conn.close()
            return json_response({'group_id': group['id'], 'group_code': group['group_code'], 'group_name': group['name'],
                                  'location_name': group['location_name'] or '未知网点', 'location_address': group['location_address'],
                                  'system_name': system_name, 'total_slots': slot_stats['total_slots'] or 0,
                                  'available_slots': slot_stats['available_slots'] or 0, 'occupied_slots': slot_stats['occupied_slots'] or 0,
                                  'deposit_amount': float(get_setting('deposit_amount', '20'))})
        cursor.execute('SELECT c.id as cabinet_id, c.cabinet_code, c.name as cabinet_name, c.total_slots, c.mainboard_device_id, l.name as location_name, l.address as location_address, l.allow_h5_to_mp, l.force_follow_mp, l.show_qr_follow, l.h5_url, l.id as loc_id FROM cabinets c LEFT JOIN locations l ON c.location_id = l.id WHERE c.cabinet_code = %s', (cabinet_code,))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在', code=404)
        cursor.execute('SELECT COUNT(*) as available FROM cabinet_slots WHERE cabinet_id = %s AND status = 1', (cabinet['cabinet_id'],))
        available_count = cursor.fetchone()['available']
        conn.close()
        device_id = cabinet.get('mainboard_device_id', '') or ''
        need_redirect = bool(cabinet.get('allow_h5_to_mp')) if cabinet.get('allow_h5_to_mp') is not None else False
        data = {
            'cabinet_id': cabinet['cabinet_id'], 'cabinet_code': cabinet['cabinet_code'],
            'cabinet_name': cabinet['cabinet_name'], 'location_name': cabinet['location_name'],
            'location_address': cabinet['location_address'], 'total_slots': cabinet['total_slots'],
            'available_slots': available_count,
            'deposit_amount': float(get_setting('deposit_amount', '20')),
            'url': f'/h5/store?device={device_id}' if device_id else '',
            'need_redirect': need_redirect
        }
        if need_redirect and device_id:
            if cabinet.get('force_follow_mp'):
                data['mp_url'] = f'https://mp.weixin.qq.com/mp/redirect?wx_redirect=/h5/store?device={device_id}'
            else:
                data['mp_url'] = f'/h5/store?device={device_id}'
        return json_response(data)
    except Exception as e:
        logger.error(f'[cabinet_screen_info] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 投诉
# ============================================


@bp.route('/user/info', methods=['GET'])
def get_user_info():
    """获取用户信息（个人中心页）"""
    try:
        phone = request.args.get('phone', '')
        openid = request.args.get('openid', '')
        mp_openid = request.args.get('mp_openid', '')
        if not phone and not mp_openid:
            return json_response(message='请先登录', code=400)
        conn = get_db()
        cur = conn.cursor()
        # 统一用 mp_openid 查找
        _bc_mp = mp_openid or openid
        if not _bc_mp:
            _bc_mp = _resolve_mp_openid(cur, mp_openid='', openid=openid, phone=phone)
        if _bc_mp:
            cur.execute("SELECT balance FROM user_balances WHERE mp_openid = %s", (_bc_mp,))
            bal_row = cur.fetchone()
        else:
            bal_row = None  # 无法找到 mp_openid，返回0
        balance = float(bal_row['balance'] or 0) if bal_row else 0
        cur.execute("""
            SELECT c.name as cabinet_name, c.withdrawal_rules
            FROM orders o
            LEFT JOIN cabinets c ON o.cabinet_id = c.id
            WHERE o.user_phone = %s AND o.status != 1
            ORDER BY o.created_at DESC LIMIT 1
        """, (phone,))
        order_row = cur.fetchone()
        cabinet_name = order_row['cabinet_name'] if order_row else ''
        withdrawal_rules = order_row['withdrawal_rules'] if order_row and order_row.get('withdrawal_rules') else ''
        if not withdrawal_rules:
            cur.execute("SELECT withdrawal_rules FROM cabinets WHERE withdrawal_rules IS NOT NULL AND withdrawal_rules != '' LIMIT 1")
            wr_row = cur.fetchone()
            if wr_row:
                withdrawal_rules = wr_row['withdrawal_rules'] or ''
        conn.close()
        return json_response(data={
            'phone': phone,
            'balance': balance,
            'cabinet_name': cabinet_name,
            'withdrawal_rules': withdrawal_rules
        })
    except Exception as e:
        logger.error(f'[user/info] 错误: {e}')
        return json_response(message=str(e), code=500)

@bp.route('/complaints', methods=['POST'])
def create_complaint():
    """用户提交投诉"""
    try:
        data = request.get_json()
        user_phone = data.get('phone')
        complaint_type = data.get('type', 'self')
        content = data.get('content')
        order_no = data.get('order_no')
        wx_complaint_id = data.get('wx_complaint_id')
        if not all([user_phone, content]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        openid = data.get('openid', '')
        cursor.execute('INSERT INTO complaints (user_phone, type, content, order_no, wx_complaint_id, complaint_type, openid) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
                       (user_phone, complaint_type, content, order_no, wx_complaint_id, complaint_type, openid))
        row = cursor.fetchone()
        complaint_id = row["id"]
        conn.commit()
        # 投诉成功后自动加入提现白名单
        if openid:
            from helpers import add_whitelist
            add_whitelist(openid, 'complaint', -1)
        elif user_phone:
            from helpers import add_whitelist_by_phone
            add_whitelist_by_phone(user_phone, 'complaint', -1)
        conn.close()
        return json_response({'complaint_id': complaint_id, 'message': '投诉已提交，我们会尽快处理'})
    except Exception as e:
        logger.error(f'[create_complaint] 错误: {e}')
        return json_response(message=str(e), code=500)
# ============================================
# 设备串口配置（APK查询用）
# ============================================

@bp.route('/cabinet/serial-config', methods=['GET'])
def cabinet_serial_config():
    """APK查询串口配置：根据mainboard_device_id返回对应的串口和波特率"""
    try:
        device_id = request.args.get('device_id')
        if not device_id:
            return json_response(message='device_id不能为空', code=400)

        conn = get_db()
        cursor = conn.cursor()
        # 找到该设备对应的柜体，再查该柜体的主板配置
        cursor.execute('''
            SELECT m.serial_port, m.baud_rate, c.cabinet_code, c.mainboard_source
            FROM cabinets c
            JOIN mainboards m ON c.id = m.cabinet_id
            WHERE c.mainboard_device_id = %s
            ORDER BY m.board_index ASC
            LIMIT 1
        ''', (device_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            # 设备存在但无主板配置时回默认值
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT cabinet_code, mainboard_source FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
            cab = cursor.fetchone()
            conn.close()
            if cab:
                brand = cab['mainboard_source'] or 'YBM'
                from config import BRAND_DEFAULTS
                defaults = BRAND_DEFAULTS.get(brand, {'serial_port': 'ttyS2', 'baud_rate': 9600})
                return json_response({
                    'device_id': device_id,
                    'cabinet_code': cab['cabinet_code'],
                    'serial_port': defaults['serial_port'],
                    'baud_rate': defaults['baud_rate'],
                    'source': 'brand_default'
                })
            return json_response(message='设备未注册', code=404)

        return json_response({
            'device_id': device_id,
            'cabinet_code': row['cabinet_code'],
            'serial_port': row['serial_port'],
            'baud_rate': row['baud_rate'],
            'source': 'manual'
        })
    except Exception as e:
        logger.error(f'[cabinet_serial_config] 错误: {e}')
        return json_response(message=str(e), code=500)
# ============================================
# 微信小程序登录 - jscode2session
# ============================================

@bp.route('/wx/login', methods=['POST'])
def wx_login():
    """微信小程序登录 - 用code换openid"""
    try:
        data = request.get_json()
        code = data.get('code')
        if not code:
            return json_response(message='code不能为空', code=400)
        import requests
        import config
        appid = config.WX_MP_APP_ID
        secret = config.WX_MP_APP_SECRET
        url = f'https://api.weixin.qq.com/sns/jscode2session?appid={appid}&secret={secret}&js_code={code}&grant_type=authorization_code'
        resp = requests.get(url, timeout=10)
        result = resp.json()
        if 'openid' in result:
            openid_val = result["openid"]  # 小程序的mp_openid
            unionid_val = result.get("unionid", "")  # 微信返回的unionid
            openid_from_h5 = data.get('openid', '')  # H5传来的公众号openid
            
            try:
                conn2 = get_db()
                # 先尝试按openid更新（匹配h5传来的公众号openid）
                conn2.execute("UPDATE user_balances SET mp_openid = %s WHERE openid = %s AND (mp_openid IS NULL OR mp_openid = '')", (openid_val, openid_val))
                # 再尝试按现有的mp_openid更新（匹配已存的小程序openid）
                conn2.execute("UPDATE user_balances SET openid = %s, mp_openid = %s WHERE mp_openid = %s", (openid_val, openid_val, openid_val))
                # 也同步更新phone_openids（让后续能找到手机号）
                conn2.execute("UPDATE phone_openids SET openid = %s, mp_openid = %s WHERE mp_openid = %s", (openid_val, openid_val, openid_val))
                conn2.commit()
                conn2.close()
            except:
                pass
            
            # 查询手机号：先查mp_openid，找不到再查openid
            _phone = ''
            try:
                _conn = get_db()
                _cur = _conn.cursor()
                
                # 1. 先用mp_openid查
                _cur.execute("SELECT phone FROM phone_openids WHERE mp_openid = %s LIMIT 1", (openid_val,))
                _row = _cur.fetchone()
                
                # 2. 找不到，用openid查
                if not _row and openid_from_h5:
                    _cur.execute("SELECT phone FROM phone_openids WHERE openid = %s LIMIT 1", (openid_from_h5,))
                    _row = _cur.fetchone()
                
                # 3. 找到手机号
                if _row and _row[0]:
                    _phone = _row[0]
                    # 4. 如果是通过openid找到的，更新mp_openid
                    if openid_from_h5:
                        _cur.execute("UPDATE phone_openids SET mp_openid = %s WHERE openid = %s", (openid_val, openid_from_h5))
                        _conn.commit()
                
                # 5. unionid order lookup
                _unionid = result.get('unionid', '')
                if _unionid:
                    _cur.execute("SELECT user_phone FROM orders WHERE user_phone IN (SELECT phone FROM phone_openids WHERE unionid = %s AND phone IS NOT NULL AND length(phone) > 0 UNION SELECT phone FROM user_balances WHERE unionid = %s AND phone IS NOT NULL AND length(phone) > 0 UNION SELECT phone FROM app_users WHERE unionid = %s AND phone IS NOT NULL AND length(phone) > 0) ORDER BY created_at DESC LIMIT 1", (_unionid, _unionid, _unionid))
                    _o_row = _cur.fetchone()
                    if _o_row and _o_row[0] and _o_row[0] != _phone:
                        _phone = _o_row[0]
                        logger.info(f'[wx_login] order phone: {_phone[:3]}****{_phone[-4:]}')
                _conn.close()
            except Exception as _e:
                logger.error(f'[wx_login] 查询phone失败: {_e}')
            
            # 找到手机号后，更新老用户的mp_openid（解决H5和小程序openid不同导致创建重复账户的问题）
            if _phone:
                try:
                    _conn3 = get_db()
                    # 更新该手机号最早的user_balances记录的mp_openid为当前小程序openid
                    _conn3.execute("UPDATE user_balances SET mp_openid = %s WHERE phone = %s AND id = (SELECT min(id) FROM user_balances WHERE phone = %s)", (openid_val, _phone, _phone))
                    _conn3.commit()
                    _conn3.close()
                except Exception as _e2:
                    logger.error(f'[wx_login] 更新老用户mp_openid失败: {_e2}')
            
            # 自动合并重复的 user_balances 记录
            if _phone:
                try:
                    _mg = get_db()
                    _mgc = _mg.cursor()
                    _mgc.execute("SELECT COUNT(*) FROM user_balances WHERE phone = %s", (_phone,))
                    if _mgc.fetchone()[0] > 1:
                        _mgc.execute("SELECT mp_openid FROM phone_openids WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (_phone,))
                        _mgr = _mgc.fetchone()
                        if _mgr and _mgr[0]:
                            _mgc.execute("SELECT id FROM user_balances WHERE phone = %s AND mp_openid = %s LIMIT 1", (_phone, _mgr[0]))
                            _mgid = _mgc.fetchone()
                            if _mgid:
                                for _dup in _mgc.execute("SELECT id, balance, total_deposited, total_withdrawn FROM user_balances WHERE phone = %s AND id != %s", (_phone, _mgid[0])).fetchall():
                                    _mgc.execute("UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s, total_withdrawn = total_withdrawn + %s WHERE id = %s", (_dup[1], _dup[2], _dup[3], _mgid[0]))
                                    _mgc.execute("DELETE FROM user_balances WHERE id = %s", (_dup[0],))
                                _mg.commit()
                                logger.info(f'[auto_merge] \u5408\u5e76\u5b8c\u6bd5: {_phone}')
                    _mgc.close()
                    _mg.close()
                except Exception as _mge:
                    logger.warning(f'[auto_merge] \u5931\u8d25: {_mge}')
            _resp = {'openid': result['openid'], 'session_key': result.get('session_key', '')}
            # [FIX-20260719] 缓存session_key供手机号解密使用
            _cache_session_key(result['openid'], result.get('session_key', ''))
            if _phone:
                _resp['phone'] = _phone
            # 保存unionid到数据库（如果微信返回了unionid）
            if unionid_val:
                try:
                    _uc_save = get_db()
                    _ucur_save = _uc_save.cursor()
                    if _phone:
                        # 更新user_balances和phone_openids中的unionid
                        _ucur_save.execute("UPDATE user_balances SET unionid=%s WHERE phone=%s AND (unionid IS NULL OR unionid = '')", (unionid_val, _phone))
                        _ucur_save.execute("UPDATE phone_openids SET unionid=%s WHERE phone=%s AND (unionid IS NULL OR unionid = '')", (unionid_val, _phone))
                    else:
                        # 没有手机号，通过openid更新
                        _ucur_save.execute("UPDATE user_balances SET unionid=%s WHERE mp_openid=%s AND (unionid IS NULL OR unionid = '')", (unionid_val, openid_val))
                        _ucur_save.execute("UPDATE phone_openids SET unionid=%s WHERE mp_openid=%s AND (unionid IS NULL OR unionid = '')", (unionid_val, openid_val))
                    _uc_save.commit()
                    _ucur_save.close()
                    _uc_save.close()
                    logger.info(f'[wx_login] 已保存unionid: {unionid_val}, phone={_phone}')
                except Exception as _ue_save:
                    logger.error(f'[wx_login] 保存unionid失败: {_ue_save}')

            # 查询 unionid 返回给前端
            _unionid = ''
            try:
                _uc = get_db()
                _ucur = _uc.cursor()
                if _phone:
                    _ucur.execute("SELECT unionid FROM user_balances WHERE phone=%s AND unionid IS NOT NULL AND unionid != '' LIMIT 1", (_phone,))
                else:
                    _ucur.execute("SELECT unionid FROM user_balances WHERE mp_openid=%s AND unionid IS NOT NULL AND unionid != '' LIMIT 1", (result['openid'],))
                _ur = _ucur.fetchone()
                if _ur and _ur['unionid']:
                    _unionid = _ur['unionid']
                _uc.close()
            except Exception as _ue:
                logger.error(f'[wx_login] 查unionid失败: {_ue}')
            if _unionid:
                _resp['unionid'] = _unionid

            # 通过 _resolve_user 自动合并重复账户（基于unionid）
            if _unionid or _phone:
                try:
                    _auc = get_db()
                    _aucur = _auc.cursor()
                    _merged_uid = _resolve_user(_aucur, mp_openid=openid_val, phone=_phone, unionid=_unionid)
                    _auc.commit()
                    _aucur.close()
                    _auc.close()
                    logger.info(f"[wx_login] _resolve_user 完成: uid={_merged_uid}, unionid={_unionid[:8] if _unionid else 'none'}..., phone={_phone}")
                except Exception as _aue:
                    logger.error(f"[wx_login] _resolve_user 失败: {_aue}")

            return json_response(_resp)
        else:
            logger.error(f'[wx_login] 微信接口返回异常: {result}')
            return json_response(message='登录失败，请稍后重试', code=400)
    except Exception as e:
        logger.error(f'[wx_login] 错误: {e}')
        return json_response(message=str(e), code=500)

@bp.route('/wx/login-phone', methods=['POST'])
def wx_login_phone():
    """一步到位：解密手机号。支持两种模式：
    1. 传session_key（前端已有session_key时，避免wx.login导致session_key失效）
    2. 传code（旧模式，code换session_key再解密）
    """
    try:
        data = request.get_json()
        code = data.get('code', '')
        encrypted_data = data.get('encrypted_data')
        iv = data.get('iv')
        session_key_direct = data.get('session_key', '')  # [FIX-20260719] 直接用session_key
        if not encrypted_data or not iv:
            return json_response(message='参数不完整', code=400)
        
        session_key = ''
        openid = ''
        unionid = ''
        
        if session_key_direct:
            # 模式1：前端直接传session_key（推荐，避免wx.login使旧session_key失效）
            session_key = session_key_direct
            logger.info('[wx_login_phone] 使用前端传入的session_key')
        elif code:
            # 模式2：code换session_key
            import requests as _req
            import config
            appid = config.WX_MP_APP_ID
            secret = config.WX_MP_APP_SECRET
            url = f'https://api.weixin.qq.com/sns/jscode2session?appid={appid}&secret={secret}&js_code={code}&grant_type=authorization_code'
            resp = _req.get(url, timeout=10)
            result = resp.json()
            if 'openid' not in result:
                logger.error(f'[wx_login_phone] 微信接口异常: {result}')
                return json_response(message='微信登录失败', code=400)
            openid = result["openid"]
            unionid = result.get("unionid", "")
            session_key = result.get('session_key', '')
        
        if not session_key:
            return json_response(message='session_key获取失败', code=400)
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        try:
            session_key_bytes = base64.b64decode(session_key)
            encrypted_bytes = base64.b64decode(encrypted_data)
            iv_bytes = base64.b64decode(iv)
            cipher = Cipher(algorithms.AES(session_key_bytes), modes.CBC(iv_bytes), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted_bytes) + decryptor.finalize()
            pad_len = decrypted[-1]
            if pad_len > 0 and pad_len <= 16:
                decrypted = decrypted[:-pad_len]
            import json as _json
            phone_info = _json.loads(decrypted.decode('utf-8'))
            phone_number = phone_info.get('phoneNumber', '')
            if phone_number:
                logger.info(f'[wx_login_phone] 成功: {phone_number[:3]}****{phone_number[-4:]}')
                # 同步写入mp_openid（小程序openid，不会被link_openid覆盖）
                try:
                    from database import get_db
                    _conn = get_db()
                    _cur = _conn.cursor()
                    _cur.execute("INSERT INTO phone_openids (phone, mp_openid) VALUES (%s, %s) ON CONFLICT(phone) DO UPDATE SET mp_openid=excluded.mp_openid, updated_at=CURRENT_TIMESTAMP", (phone_number, openid))
                    _conn.commit()
                    _cur.close()
                    _conn.close()
                    logger.info(f'[wx_login_phone] mp_openid已写入: {phone_number[:3]}****{phone_number[-4:]} -> {openid[:8]}...')
                except Exception as _e:
                    logger.warning(f'[wx_login_phone] 写入mp_openid失败: {_e}')
                # 保存unionid到数据库
                if unionid:
                    try:
                        _uconn = get_db()
                        _ucur = _uconn.cursor()
                        _ucur.execute("UPDATE user_balances SET unionid=%s WHERE phone=%s AND (unionid IS NULL OR unionid = '')", (unionid, phone_number))
                        _ucur.execute("UPDATE phone_openids SET unionid=%s WHERE phone=%s AND (unionid IS NULL OR unionid = '')", (unionid, phone_number))
                        _uconn.commit()
                        _ucur.close()
                        _uconn.close()
                        logger.info(f'[wx_login_phone] 已保存unionid: {unionid[:8]}... for {phone_number[:3]}****')
                    except Exception as _ue:
                        logger.warning(f'[wx_login_phone] 保存unionid失败: {_ue}')
                # 通过 _resolve_user 自动合并重复账户（基于unionid）
                if unionid or phone_number:
                    try:
                        _auc2 = get_db()
                        _aucur2 = _auc2.cursor()
                        _merged_uid = _resolve_user(_aucur2, mp_openid=openid, phone=phone_number, unionid=unionid)
                        _auc2.commit()
                        _aucur2.close()
                        _auc2.close()
                        logger.info(f"[wx_login_phone] _resolve_user 完成: uid={_merged_uid}, unionid={unionid[:8] if unionid else 'none'}..., phone={phone_number[:3]}****")
                    except Exception as _aue2:
                        logger.error(f"[wx_login_phone] _resolve_user 失败: {_aue2}")
                return json_response({'openid': openid, 'phone': phone_number, 'session_key': session_key})
            else:
                logger.error(f'[wx_login_phone] 解密无手机号: {phone_info}')
                return json_response(message='解密手机号失败', code=400)
        except Exception as de:
            logger.error(f'[wx_login_phone] 解密异常: {de}')
            return json_response(message='手机号解密失败', code=400)
    except Exception as e:
        logger.error(f'[wx_login_phone] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/wx/phone', methods=['POST'])
def wx_decrypt_phone():
    """微信小程序手机号解密"""
    try:
        data = request.get_json()
        encrypted_data = data.get('encrypted_data')
        iv = data.get('iv')
        if not encrypted_data or not iv:
            return json_response(message='参数不完整', code=400)
        
        # 从请求头或参数获取session_key（前端先wx.login拿到后缓存的）
        session_key = data.get('session_key')
        if not session_key:
            return json_response(message='session_key缺失，请重新登录', code=400)
        
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        
        try:
            session_key_bytes = base64.b64decode(session_key)
            encrypted_bytes = base64.b64decode(encrypted_data)
            iv_bytes = base64.b64decode(iv)
            
            cipher = Cipher(algorithms.AES(session_key_bytes), modes.CBC(iv_bytes), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted_bytes) + decryptor.finalize()
            
            # 去除PKCS7填充
            pad_len = decrypted[-1]
            if pad_len > 0 and pad_len <= 16:
                decrypted = decrypted[:-pad_len]
            
            import json as _json
            phone_info = _json.loads(decrypted.decode('utf-8'))
            phone_number = phone_info.get('phoneNumber', '')
            
            if phone_number:
                logger.info(f'[wx_decrypt_phone] 获取手机号成功: {phone_number[:3]}****{phone_number[-4:]}')
                return json_response({'phone': phone_number})
            else:
                logger.error(f'[wx_decrypt_phone] 解密结果无手机号: {phone_info}')
                return json_response(message='获取手机号失败', code=400)
        except Exception as de:
            logger.error(f'[wx_decrypt_phone] 解密错误: {de}')
            return json_response(message='手机号解密失败', code=400)
    except Exception as e:
        logger.error(f'[wx_decrypt_phone] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 用户订单和余额API - H5个人中心用
# ============================================

@bp.route('/user/orders', methods=['GET'])
def get_user_orders():
    """获取用户订单列表"""
    try:
        phone = request.args.get('phone', '')
        openid = request.args.get('openid', '')
        
        conn = get_db()
        cur = conn.cursor()
        
        # 统一解析 user_id
        user_id = _resolve_user(cur, mp_openid=openid, phone=phone)
        
        if user_id:
            cur.execute("""
                SELECT o.id, o.order_no, o.user_phone, o.cabinet_id, o.compartment_number, o.slot_size, o.access_code,
                       o.deposit_amount, o.status, o.store_time, o.retrieve_time, o.created_at,
                       c.name as cabinet_name, c.cabinet_code,
                       l.name as location_name
                FROM orders o
                LEFT JOIN cabinets c ON o.cabinet_id = c.id
                LEFT JOIN locations l ON c.location_id = l.id
                WHERE o.user_id = %s AND o.status != 1
                ORDER BY o.created_at DESC
                LIMIT 50
            """, (user_id,))
        elif phone:
            cur.execute("""
                SELECT o.id, o.order_no, o.user_phone, o.cabinet_id, o.compartment_number, o.slot_size, o.access_code,
                       o.deposit_amount, o.status, o.store_time, o.retrieve_time, o.created_at,
                       c.name as cabinet_name, c.cabinet_code,
                       l.name as location_name
                FROM orders o
                LEFT JOIN cabinets c ON o.cabinet_id = c.id
                LEFT JOIN locations l ON c.location_id = l.id
                WHERE o.user_phone = %s AND o.status != 1
                ORDER BY o.created_at DESC
                LIMIT 50
            """, (phone,))
        else:
            conn.close()
            return json_response(message='请先登录', code=400)
        
        orders = [dict(row) for row in cur.fetchall()]
        conn.close()
        
        return json_response(data=orders)
    except Exception as e:
        logger.error(f'[user/orders] 错误: {e}')
        return json_response(message=str(e), code=500)


        return json_response(message=str(e), code=500)


@bp.route('/user/subscribe-templates', methods=['GET'])
def get_subscribe_templates():
    """返回订阅消息模板ID列表"""
    return json_response(data={
        'templates': [
            '5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A',
            'YsfB8FH4eMrISAS92oUzBhoXe178AnxP8XSA0_24YoE'
        ]
    })

@bp.route('/user/withdrawal-rules', methods=['GET'])
def get_withdrawal_rules():
    """获取提现规则"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT withdraw_enabled, withdraw_mode, click_free_count, auto_approve_time, auto_approve_day FROM locations WHERE withdraw_enabled = 1 LIMIT 1")
        row = cur.fetchone()
        conn.close()
        rules = {
            'withdraw_enabled': row['withdraw_enabled'] if row else 1,
            'withdraw_mode': row['withdraw_mode'] if row else 'auto_approve',
            'daily_limit': row['click_free_count'] if row else 3,
            'withdraw_time': '00:00-23:59',
            'arrival_time': '0-3个工作日',
        }
        return json_response(data=rules)
    except Exception as e:
        return json_response(data={
            'withdraw_enabled': 1, 'daily_limit': 3,
            'withdraw_time': '00:00-23:59', 'arrival_time': '0-3个工作日',
            'withdraw_mode': 'auto_approve'
        })

@bp.route('/user/balance', methods=['GET'])
def get_user_balance():
    """获取用户钱包余额"""
    try:
        phone = request.args.get('phone')
        openid = request.args.get('openid', '') or ''
        cabinet_id = request.args.get('cabinet_id') or ''

        conn = get_db()
        cur = conn.cursor()
        
        # 统一解析 user_id
        user_id = _resolve_user(cur, mp_openid=openid, phone=phone)
        if not user_id:
            conn.close()
            return json_response(message='用户未登录', code=400)
        
        # 用 user_id 查余额
        cur.execute("SELECT phone, balance, total_deposited, total_withdrawn, first_use_time, created_at FROM user_balances WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        
        _check_phone = row["phone"] if row else phone
        
        # 检查待处理提现
        has_pending_withdrawal = False
        cur.execute('SELECT COUNT(*) as cnt FROM withdrawal_records WHERE user_phone = %s AND status IN (0, 1)', (_check_phone,))
        wd_row = cur.fetchone()
        if wd_row and wd_row['cnt'] > 0:
            has_pending_withdrawal = True
        
        # 检查进行中的订单
        has_active_orders = False
        cur.execute('SELECT COUNT(*) as cnt FROM orders WHERE user_id = %s AND status = 2', (user_id,))
        ao_row = cur.fetchone()
        if ao_row and ao_row['cnt'] > 0:
            has_active_orders = True
        
        # 检查余额隐藏配置
        balance_hidden = False
        if cabinet_id:
            try:
                cur.execute("SELECT l.balance_hide_enabled, l.balance_hide_days FROM cabinets c LEFT JOIN locations l ON c.location_id = l.id WHERE c.id = %s", (cabinet_id,))
                loc_row = cur.fetchone()
                if loc_row and loc_row.get('balance_hide_enabled'):
                    balance_hidden = True
            except:
                pass
        
        # 查 merchant_id
        user_mch_id = None
        if row and row.get('merchant_id'):
            user_mch_id = row['merchant_id']
        conn.close()
        
        if row:
            result = dict(row)
            balance_val = float(result.get('balance', 0) or 0)
            result['available_balance'] = balance_val
            result['has_pending_withdrawal'] = has_pending_withdrawal
            result['has_active_orders'] = has_active_orders
            result['balance_hidden'] = balance_hidden
            # ====== [优化] 加入倒计时信息 ======
            if user_mch_id:
                wh = get_withhold_hours(user_mch_id)
                now_dt = datetime.now()
                arrival_dt = now_dt + timedelta(hours=wh) if wh > 0 else now_dt
                result['withhold_hours'] = wh
                result['arrival_time'] = arrival_dt.strftime('%Y-%m-%d %H:%M:%S') if wh > 0 else ''
            else:
                result['withhold_hours'] = 0
                result['arrival_time'] = ''
            # 兼容小程序字段名
            result['has_pending'] = has_pending_withdrawal
            try:
                # get has_triggered_withdraw from the row
                result['has_triggered_withdraw'] = row['has_triggered_withdraw'] if row and 'has_triggered_withdraw' in row.keys() else False
            except:
                result['has_triggered_withdraw'] = False
            return json_response(data=result)
        else:
            # 用户余额记录不存在，返回默认值
            return json_response(data={
                'phone': phone,
                'balance': 0,
                'available_balance': 0,
                'total_deposited': 0,
                'total_withdrawn': 0,
                'has_pending_withdrawal': has_pending_withdrawal,
                'has_active_orders': has_active_orders,
                'balance_hidden': balance_hidden,
                'withhold_hours': 0,
                'arrival_time': '',
                'has_pending': has_pending_withdrawal
            })
    except Exception as e:
        logger.error(f'[user/balance] 错误: {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 未付款订单超时清理
# ============================================

@bp.route('/cleanup/expired-orders', methods=['POST'])
def cleanup_expired_orders():
    """清理超过5分钟仍未付款的订单，释放柜格"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 查找status=1且创建时间超过5分钟的订单
        cursor.execute('''
            SELECT o.id, o.slot_id, o.order_no FROM orders o
            WHERE o.status = 1 AND o.store_time < NOW() - INTERVAL '1 minute'
        ''')
        expired = cursor.fetchall()
        released = 0
        for order in expired:
            if order['slot_id']:
                cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s AND status = 2', (order['slot_id'],))
                if cursor.rowcount > 0:
                    released += 1
            cursor.execute('UPDATE orders SET status = 5 WHERE id = %s', (order['id'],))
            logger.info(f'[超时清理] 订单{order["order_no"]}超时未支付，已释放柜格')
        conn.commit()
        conn.close()
        return json_response({'cleaned': len(expired), 'released_slots': released})
    except Exception as e:
        logger.error(f'[cleanup_expired] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 用户余额提现
# ============================================

@bp.route('/user/withdraw', methods=['POST'])
def user_withdraw():
    """用户从余额申请提现"""
    try:
        data = request.get_json()
        phone = str(data.get('phone') or data.get('user_phone') or '').strip()
        openid = data.get('openid', '') or ''
        unionid = data.get('unionid', '') or ''
        wechat_name = data.get('wechat_name', '') or ''
        mp_openid = data.get('mp_openid', '')
        wechat_name = data.get('wechat_name', '')
        amount = data.get('amount', 0)

        if not mp_openid and not openid and not phone:
            return json_response(message='请先登录', code=400)
        conn = get_db()
        cursor = conn.cursor()

        # 统一用 mp_openid 查找用户余额
        if not mp_openid:
            mp_openid = _resolve_mp_openid(cursor, mp_openid='', openid=openid, phone=phone)
        if not mp_openid:
            conn.close()
            return json_response(message='用户未登录', code=400)
        cursor.execute('SELECT phone, balance, total_deposited, total_withdrawn FROM user_balances WHERE mp_openid = %s LIMIT 1', (mp_openid,))
        row = cursor.fetchone()
        if row:
            phone = row['phone']
        if not row:
            conn.rollback()
            return json_response(message='用户不存在', code=404)
        
        balance = row['balance']
        try:
            cursor.execute("SELECT COALESCE(SUM(balance),0) FROM user_balances WHERE phone = %s", (phone,))
            _tb = float(cursor.fetchone()[0])
            if _tb > balance:
                balance = _tb
        except:
            pass
        if balance <= 0:
            conn.rollback()
            conn.close()
            return json_response(message='余额不足，无法提现', code=400)
        # 未传金额时自动全额提现
        if not amount or float(amount) <= 0:
            amount = float(balance)
        else:
            amount = float(amount)
        if amount > balance:
            conn.rollback()
            conn.close()
            return json_response(message='提现金额超过余额', code=400)
        
        
        # 检查用户最近使用的网点是否允许提现
        cursor.execute("""
            SELECT l.withdraw_enabled, l.withdraw_mode, l.auto_approve_rate 
            FROM orders o 
            JOIN cabinets c ON o.cabinet_id = c.id 
            JOIN locations l ON c.location_id = l.id 
            WHERE o.user_phone = %s 
            ORDER BY o.created_at DESC 
            LIMIT 1
        """, (phone,))
        loc_row = cursor.fetchone()
        if loc_row and not loc_row['withdraw_enabled']:
            conn.close()
            return json_response(message='该网点暂不支持提现', code=400)
        
        withdraw_mode = loc_row['withdraw_mode'] if loc_row else 'auto_approve'

        # Merchant-phase hold check
        if withdraw_mode == 'auto_approve':
            _order_openid = mp_openid or openid or ''
            _order_phone = phone
            _needs_hold = check_withdraw_auto_approve(openid=_order_openid, phone=_order_phone)
            if _needs_hold:
                withdraw_mode = 'manual'
            else:
                mark_user_withdraw(openid=_order_openid, phone=_order_phone)
        if withdraw_mode == 'auto_approve':
            # 自动审批模式：立即调微信退款，提现管理有记录
            sql = """SELECT bd.id, bd.order_id, bd.amount, o.transaction_id, o.openid as order_openid
            FROM user_balance_details bd
            JOIN orders o ON bd.order_id = o.id
            JOIN user_balances ub ON bd.user_phone = ub.phone
            WHERE ub.mp_openid=%s AND bd.status='available' AND o.transaction_id IS NOT NULL AND o.transaction_id != ''
            ORDER BY bd.id DESC"""
            cursor.execute(sql, (mp_openid,))
            balance_records = cursor.fetchall()
            if not balance_records:
                conn.close()
                return json_response(message='没有可退款的订单，无法提现', code=400)
            total_refundable = 0
            order_refund_plan = []
            for br in balance_records:
                refundable = float(br['amount'])
                order_refund_plan.append((br['order_id'], refundable, br))
                total_refundable += refundable
            if total_refundable <= 0:
                conn.close()
                return json_response(message='没有可退款的金额', code=400)
            actual_amount = min(float(amount), total_refundable)
            # 扣除余额
            cursor.execute('UPDATE user_balances SET balance = GREATEST(balance - %s, 0), total_withdrawn = total_withdrawn + %s WHERE phone = %s AND balance > 0',
                           (actual_amount, actual_amount, phone))
            # 立即微信退款
            from helpers import do_real_refund
            remaining = actual_amount
            first_wid = None
            all_ok = True
            for oid, refundable, br in order_refund_plan:
                if remaining <= 0.001:
                    break
                refund_this = min(remaining, refundable)
                order_openid = br.get('order_openid') or openid
                ok, rid, rmsg = do_real_refund(order_id=oid, amount=refund_this, openid=order_openid, skip_balance=True)
                if not ok:
                    all_ok = False
                st = 2 if ok else 4
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, error_msg, openid) VALUES (%s, %s, %s, %s, 1, %s, %s) RETURNING id',
                               (oid, phone, refund_this, st, None if ok else rmsg, order_openid))
                row = cursor.fetchone()
                if first_wid is None:
                    first_wid = row['id']
                if ok:
                    cursor.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount = COALESCE(refund_amount, 0) + %s WHERE id = %s', (rid, refund_this, oid))
                    cursor.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                remaining -= refund_this
            conn.commit()
            conn.close()
            if all_ok:
                if mp_openid:
                    try:
                        from helpers import send_wx_subscribe_message
                        wd_data = {
                            'amount8': {'value': '¥{:.2f}'.format(actual_amount)},
                            'time6': {'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                            'thing3': {'value': '原路退回支付账户'},
                            'thing2': {'value': '预计1-3个工作日到账，请耐心等待'}
                        }
                        send_wx_subscribe_message(mp_openid, 'YsfB8FH4eMrISAS92oUzBhoXe178AnxP8XSA0_24YoE', wd_data, phone=phone, page='pages/mine/mine')
                    except Exception as e:
                        logger.error(f'[提现通知失败] {e}')
                return json_response(data={
                    'withdrawal_id': first_wid,
                    'status': 'refunded',
                    'amount': actual_amount,
                    'message': '提现成功，已原路退回'
                })
        else:
            # 手动审批模式：按订单逐条创建待审批记录
            if not amount or float(amount) <= 0:
                amount = float(balance)
            # 从余额明细表查找可提现的记录（status='available'）
            cursor.execute("SELECT bd.id, bd.order_id, bd.amount, o.transaction_id, o.openid as order_openid FROM user_balance_details bd JOIN orders o ON bd.order_id = o.id JOIN user_balances ub ON bd.user_phone = ub.phone WHERE ub.mp_openid=%s AND bd.status='available' AND o.transaction_id IS NOT NULL AND o.transaction_id != '' ORDER BY bd.id DESC", (mp_openid,))
            balance_records = cursor.fetchall()
            if not balance_records:
                conn.close()
                return json_response(message='没有可退款的订单', code=400)
            # 计算可退总额
            total_refundable = 0
            order_plan = []
            for br in balance_records:
                refundable = float(br['amount'])
                order_plan.append((br['order_id'], refundable, br))
                total_refundable += refundable
            if total_refundable <= 0:
                conn.close()
                return json_response(message='没有可退款的金额', code=400)
            actual_amount = min(float(amount), total_refundable)
            # 检查白名单
            from helpers import check_whitelist, add_whitelist, consume_whitelist, do_real_refund
            wl_record = check_whitelist(openid) if openid else None
            if wl_record:
                # 白名单免审，直接退款
                cursor.execute('UPDATE user_balances SET balance = GREATEST(balance - %s, 0), total_withdrawn = total_withdrawn + %s WHERE phone = %s AND balance > 0', (actual_amount, actual_amount, phone))
                import json as _json_cons
                remaining = actual_amount
                _total_refunded = 0.0
                _all_order_ids = []
                _first_err = None
                _first_oid_openid = None
                for oid, refundable, br in order_plan:
                    if remaining <= 0.001: break
                    refund_this = min(remaining, refundable)
                    order_openid = br.get('order_openid') or openid
                    ok, rid, rmsg = do_real_refund(order_id=oid, amount=refund_this, openid=order_openid, skip_balance=True)
                    if not ok and _first_err is None:
                        _first_err = rmsg
                    _total_refunded += refund_this
                    _all_order_ids.append(str(oid))
                    if _first_oid_openid is None:
                        _first_oid_openid = order_openid
                    if ok:
                        cursor.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount = COALESCE(refund_amount, 0) + %s WHERE id = %s', (rid, refund_this, oid))
                        cursor.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                    remaining -= refund_this
                _st = 2 if _first_err is None else 4
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, approver, error_msg, openid, order_ids) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s) RETURNING id', (_all_order_ids[0] if _all_order_ids else None, phone, _total_refunded, _st, 'whitelist_auto', _first_err, _first_oid_openid or openid, _json_cons3.dumps(_all_order_ids)))
                first_wid = cursor.fetchone()['id']
                if wl_record['source'] == 'manual_help':
                    consume_whitelist(openid)
                conn.commit()
                conn.close()
                return json_response(data={'withdrawal_id': first_wid, 'status': 'refunded', 'amount': actual_amount, 'message': '白名单免审，已自动退款'})
            # 检查是否被拒绝后重提
            cursor.execute('SELECT COUNT(*) as cnt FROM withdrawal_records wr WHERE user_phone = %s AND status = 3', (phone,))
            reject_cnt = cursor.fetchone()['cnt']
            if reject_cnt > 0 and openid:
                add_whitelist(openid, 'reject_retry', -1)
                cursor.execute('UPDATE user_balances SET balance = GREATEST(balance - %s, 0), total_withdrawn = total_withdrawn + %s WHERE phone = %s AND balance > 0', (actual_amount, actual_amount, phone))
                import json as _json_cons3; remaining = actual_amount; _total_refunded = 0.0; _all_order_ids = []; _first_err = None; _first_oid_openid = None
                for oid, refundable, br in order_plan:
                    if remaining <= 0.001: break
                    refund_this = min(remaining, refundable)
                    order_openid = br.get('order_openid') or openid
                    ok, rid, rmsg = do_real_refund(order_id=oid, amount=refund_this, openid=order_openid, skip_balance=True)
                    if not ok and _first_err is None:
                        _first_err = rmsg
                    _total_refunded += refund_this
                    _all_order_ids.append(str(oid))
                    if _first_oid_openid is None:
                        _first_oid_openid = order_openid
                    if ok:
                        cursor.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount = COALESCE(refund_amount, 0) + %s WHERE id = %s', (rid, refund_this, oid))
                        cursor.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                    remaining -= refund_this
                _st = 2 if _first_err is None else 4
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, approver, error_msg, openid, order_ids) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s) RETURNING id', (_all_order_ids[0] if _all_order_ids else None, phone, _total_refunded, _st, 'whitelist_auto', _first_err, _first_oid_openid or openid, _json_cons3.dumps(_all_order_ids)))
                first_wid = cursor.fetchone()['id']
                conn.commit()
                conn.close()
                return json_response(data={'withdrawal_id': first_wid, 'status': 'refunded', 'amount': actual_amount, 'message': '已加入白名单，自动退款'})
            # 冻结余额（严格按phone+openid）
            cursor.execute('UPDATE user_balances SET balance = GREATEST(balance - %s, 0) WHERE mp_openid = %s',
                           (actual_amount, mp_openid))
            # 按订单逐条创建提现记录
            remaining = actual_amount
            first_wid = None
            _auto_time = None
            for oid, refundable, br in order_plan:
                if remaining <= 0.001:
                    break
                refund_this = min(remaining, refundable)
                order_openid = br.get('order_openid') or openid
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, openid, auto_approve_time) VALUES (%s, %s, %s, 0, 1, %s, %s) RETURNING id',
                               (oid, phone, refund_this, order_openid, _auto_time))
                row = cursor.fetchone()
                if first_wid is None:
                    first_wid = row["id"]
                cursor.execute("UPDATE user_balance_details SET status='pending' WHERE order_id=%s AND status='available'", (oid,))
                remaining -= refund_this
            conn.commit()
            conn.close()
            # 发送订阅消息：使用 mp_openid（已解析的公众号openid）
            if mp_openid:
                try:
                    from helpers import send_wx_subscribe_message
                    wd_data = {
                        'amount8': {'value': '¥{:.2f}'.format(actual_amount)},
                        'time6': {'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                        'thing3': {'value': '原路退回支付账户'},
                        'thing2': {'value': '预计1-3个工作日到账，请耐心等待'}
                    }
                    send_wx_subscribe_message(mp_openid, 'YsfB8FH4eMrISAS92oUzBhoXe178AnxP8XSA0_24YoE', wd_data, phone=phone, page='pages/mine/mine')
                except Exception as e:
                    logger.error(f'[提现通知失败] {e}')
            return json_response(data={
                'withdrawal_id': first_wid,
                'status': 'pending',
                'message': f'提现申请已提交（¥{actual_amount:.2f}），等待审核'
            })
    except Exception as e:
        logger.error(f'[user/withdraw] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/user/link-openid', methods=['POST'])
def link_openid():
    try:
        data = request.get_json()
        phone = data.get('phone')
        openid_h5 = data.get('openid', '')  # H5公众号openid
        mp_openid_val = data.get('mp_openid', '') or openid_h5  # 小程序openid，没传就用H5的
        unionid = data.get('unionid') or ''
        wechat_name = data.get('wechat_name') or data.get('nickName') or ''
        if not phone or not openid_h5:
            return json_response(message='参数不完整', code=400)
        from helpers import get_db
        conn = get_db()
        cursor = conn.cursor()

        # 1. 先用mp_openid查（小程序openid）
        cursor.execute("SELECT phone FROM phone_openids WHERE mp_openid = %s LIMIT 1", (mp_openid_val,))
        existing = cursor.fetchone()

        # 2. 找不到，改用H5的openid查（兼容老用户）
        if not existing:
            cursor.execute("SELECT phone FROM phone_openids WHERE openid = %s LIMIT 1", (openid_h5,))
            existing = cursor.fetchone()

        if existing:
            # 3. 找到后，更新mp_openid + wechat_name + unionid
            cursor.execute('UPDATE phone_openids SET mp_openid=%s, wechat_name=%s, unionid=%s, updated_at=CURRENT_TIMESTAMP WHERE openid=%s',
                           (mp_openid_val, wechat_name, unionid, openid_h5))
        else:
            # 没找到，正常插入
            cursor.execute('INSERT INTO phone_openids (phone, openid, mp_openid, wechat_name, unionid) VALUES (%s, %s, %s, %s, %s) ON CONFLICT(phone) DO UPDATE SET openid=excluded.openid, mp_openid=excluded.mp_openid, wechat_name=excluded.wechat_name, unionid=excluded.unionid, updated_at=CURRENT_TIMESTAMP',
                           (phone, openid_h5, mp_openid_val, wechat_name, unionid))

        # 统一用 mp_openid 更新 user_balances
        if mp_openid_val:
            cursor.execute("UPDATE user_balances SET mp_openid = %s, openid = COALESCE(NULLIF(openid, ''), %s) WHERE phone = %s AND (mp_openid IS NULL OR mp_openid = '' OR mp_openid = %s)", (mp_openid_val, openid_h5, phone, mp_openid_val))
            if cursor.rowcount == 0:
                cursor.execute("UPDATE user_balances SET mp_openid = %s, phone = %s WHERE openid = %s AND (mp_openid IS NULL OR mp_openid = '')", (mp_openid_val, phone, openid_h5))
        if unionid:
            cursor.execute('UPDATE user_balances SET unionid=%s WHERE mp_openid=%s', (unionid, mp_openid_val))

        conn.commit()
        conn.close()
        return json_response(message='关联成功')
    except Exception as e:
        logger.error(f'[link_openid] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/user/link-mp-openid', methods=['POST'])
def link_mp_openid_from_mini():
    """小程序subscribe页面调用：用wx.login的code换取mp_openid，并绑定到手机号"""
    try:
        data = request.get_json()
        code = data.get('code')
        phone = data.get('phone', '')
        if not code:
            return json_response(message='code不能为空', code=400)
        import requests as req
        import config
        appid = config.WX_MP_APP_ID
        secret = config.WX_MP_APP_SECRET
        url = f'https://api.weixin.qq.com/sns/jscode2session?appid={appid}&secret={secret}&js_code={code}&grant_type=authorization_code'
        resp = req.get(url, timeout=10)
        result = resp.json()
        mp_openid = result.get('openid', '')
        unionid = result.get('unionid', '')
        gzh_openid = data.get('gzh_openid', '')
        nickname = data.get('nickname', '')
        if not mp_openid:
            logger.error(f'[link_mp_openid] jscode2session失败: {result}')
            return json_response(message='获取小程序openid失败', code=500)
        
        from helpers import get_db
        conn = get_db()
        cursor = conn.cursor()
        
        # 更新 phone_openids 的 mp_openid（按手机号匹配）
        if phone:
            cursor.execute('SELECT id FROM phone_openids WHERE phone = %s LIMIT 1', (phone,))
            existing = cursor.fetchone()
            if existing:
                cursor.execute('UPDATE phone_openids SET mp_openid = %s, unionid = COALESCE(NULLIF(%s, \'\'), unionid), gzh_openid = COALESCE(NULLIF(%s, \'\'), gzh_openid), wechat_name = COALESCE(NULLIF(%s, \'\'), wechat_name) WHERE phone = %s', (mp_openid, unionid, gzh_openid, nickname, phone))
            else:
                cursor.execute('INSERT INTO phone_openids (phone, mp_openid, unionid, gzh_openid, wechat_name) VALUES (%s, %s, %s, %s, %s)', (phone, mp_openid, unionid, gzh_openid, nickname))
        
        # 更新 user_balances 的 mp_openid（按手机号匹配，排除已有正确mp_openid的记录）
        if phone:
            cursor.execute("UPDATE user_balances SET mp_openid = %s WHERE phone = %s AND (mp_openid IS NULL OR mp_openid = chr(39)||chr(39) OR mp_openid LIKE chr(39)oLhbm2%%chr(39))", (mp_openid, phone))
        if phone and mp_openid:

            cursor.execute("UPDATE app_users SET mp_openid = %s WHERE phone = %s AND (mp_openid IS NULL OR mp_openid = chr(39)||chr(39))", (mp_openid, phone))
        # ?????????
        if phone and nickname:
            cursor.execute("UPDATE orders SET wechat_name = %s WHERE user_phone = %s AND (wechat_name IS NULL OR wechat_name = chr(39)||chr(39))", (nickname, phone))
            cursor.execute("UPDATE app_users SET nickname = %s WHERE phone = %s AND (nickname IS NULL OR nickname = chr(39)||chr(39))", (nickname, phone))

        
        conn.commit()
        conn.close()
        logger.info(f'[link_mp_openid] 成功绑定 mp_openid={mp_openid[:8]}... phone={phone}')
        return json_response(message='绑定成功', data={'mp_openid': mp_openid})
    except Exception as e:
        logger.error(f'[link_mp_openid] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/user/sync-mp-openid', methods=['POST'])
def sync_mp_openid():
    """小程序个人中心加载时，用小程序 ID 匹配并更新 user_balances.mp_openid"""
    try:
        data = request.get_json()
        mp_openid = data.get('mp_openid', '')  # 小程序ID
        gzh_openid = data.get('gzh_openid', '')  # 公众号ID
        if not mp_openid:
            return json_response(message='参数不完整', code=400)

        from helpers import get_db
        conn = get_db()
        cur = conn.cursor()

        # 1. 先查 user_balances 有没有这条小程序ID
        cur.execute("SELECT id FROM user_balances WHERE mp_openid = %s LIMIT 1", (mp_openid,))
        exist = cur.fetchone()
        if exist:
            conn.close()
            return json_response(message='已同步', data={'matched': True})

        # 2. 没有 -> 用公众号ID去匹配
        matched_gzh = gzh_openid or ''
        if not matched_gzh:
            # 从 phone_openids 看有没有这个小程序ID对应的公众号ID
            cur.execute("SELECT openid FROM phone_openids WHERE mp_openid = %s LIMIT 1", (mp_openid,))
            r = cur.fetchone()
            if r:
                matched_gzh = r[0]

        if matched_gzh:
            # 用公众号ID去 user_balances 找
            cur.execute("SELECT id FROM user_balances WHERE openid = %s AND (mp_openid IS NULL OR mp_openid = chr(39)||chr(39) OR mp_openid = %s) LIMIT 1", (matched_gzh, matched_gzh))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE user_balances SET mp_openid = %s WHERE id = %s", (mp_openid, r[0]))
                # 同时更新 phone_openids，确保发通知时能查到小程序ID
                cur.execute("UPDATE phone_openids SET mp_openid = %s WHERE openid = %s AND (mp_openid IS NULL OR mp_openid = '' OR mp_openid = %s)", (mp_openid, matched_gzh, matched_gzh))
                conn.commit()
                conn.close()
                return json_response(message='同步成功', data={'matched': True, 'updated': True})
            else:
                # 用公众号ID去 phone_openids 找到 phone，再用 phone 去 user_balances 找（兼容旧数据）
                cur.execute("SELECT phone FROM phone_openids WHERE openid = %s LIMIT 1", (matched_gzh,))
                r = cur.fetchone()
                if r:
                    found_phone = r[0]
                    cur.execute("SELECT id FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != chr(39)||chr(39) LIMIT 1", (found_phone,))
                    r = cur.fetchone()
                    if r:
                        cur.execute("UPDATE user_balances SET mp_openid = %s WHERE id = %s", (mp_openid, r[0]))
                        conn.commit()
                        conn.close()
                        return json_response(message='同步成功', data={'matched': True, 'updated': True})

        conn.close()
        return json_response(message='未找到匹配记录', data={'matched': False})
    except Exception as e:
        logger.error(f'[sync_mp_openid] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/order/<int:order_id>/reopen', methods=['POST'])
def order_reopen_by_url(order_id):
    try:
        from helpers import get_db, json_response, logger, send_open_lock
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''SELECT o.*, cs.board_no, cs.lock_no, c.mainboard_device_id, c.cabinet_code
            FROM orders o LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id
            LEFT JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s''', (order_id,))
        order = cursor.fetchone()
        conn.close()
        if not order:
            return json_response(message='\u8ba2\u5355\u4e0d\u5b58\u5728', code=404)
        device_id = order['mainboard_device_id']
        send_open_lock(device_id, order['board_no'] or 1, order['lock_no'] or 1, order_id=order.get('order_no', str(order_id)))
        return json_response(message='\u5f00\u9501\u6307\u4ee4\u5df2\u53d1\u9001')
    except Exception as e:
        logger.error(f'[order_reopen_url] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/user/wallet/transactions', methods=['GET'])

@bp.route('/user/wallet/transactions', methods=['GET'])
def get_user_transactions():
    """用户交易记录"""
    try:
        phone = request.args.get('phone', '')
        openid = request.args.get('openid', '')
        tp = request.args.get('type', '')  # all, income, expense
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        offset = (page - 1) * limit
        if not phone:
            return json_response(message='????', code=400)
        conn = get_db()
        cur = conn.cursor()
        if tp == 'income':
            where_extra = 'AND d.amount > 0'
        elif tp == 'expense':
            where_extra = 'AND d.amount < 0'
        else:
            where_extra = ''
            cur.execute(f'''
                SELECT d.id, d.amount, d.source_time, d.status, d.remark, d.order_id,
                       o.order_no
                FROM user_balance_details d
                LEFT JOIN orders o ON d.order_id = o.id
                WHERE d.user_phone = %s {where_extra}
                ORDER BY d.source_time DESC
                LIMIT %s OFFSET %s
            ''', (phone, limit, offset))
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(f'SELECT COUNT(*) FROM user_balance_details d WHERE d.user_phone = %s {where_extra}', (phone,))
        total = cur.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total})
    except Exception as e:
        logger.error(f'[user/transactions] 错误: {e}')
        return json_response(message=str(e), code=500)

@bp.route('/user/wallet/withdrawals', methods=['GET'])
def get_user_withdrawals():
    """用户提现记录"""
    try:
        phone = request.args.get('phone', '')
        openid = request.args.get('openid', '')
        if phone and not openid:
            conn_temp = get_db()
            cur_temp = conn_temp.cursor()
            cur_temp.execute('SELECT COALESCE(mp_openid, openid) as openid FROM phone_openids WHERE phone = %s ORDER BY created_at DESC LIMIT 1', (phone,))
            row_temp = cur_temp.fetchone()
            conn_temp.close()
            if row_temp and row_temp['openid']:
                openid = row_temp['openid']
        if not phone:
            return json_response(message='????', code=400)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            SELECT w.id, w.amount, w.status, w.apply_time, w.approve_time, w.error_msg
            FROM withdrawal_records w
            WHERE w.user_phone = %s
            ORDER BY w.created_at DESC
            LIMIT 50
        ''', (phone,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return json_response(data=rows)
    except Exception as e:
        logger.error(f'[user/withdrawals] 错误: {e}')
        return json_response(message=str(e), code=500)
