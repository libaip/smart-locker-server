

import random, string

def generate_random_password(length=6):
    """生成6位数字明文密码"""
    return ''.join(random.choices(string.digits, k=length))

from login_guard import check_rate, fail, ok, left
"""
管理后台API - Blueprint
包含：管理员认证、商户、网点、柜组、柜体、主板、柜格、订单、统计、设置等
"""
import logging
import random
import string
import json
import secrets
from datetime import datetime, timedelta
from flask import Blueprint, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db
from helpers import (json_response, require_auth, require_merchant_auth, require_agent_auth, require_employee_auth,
                     get_setting, is_mock_mode, send_open_lock, should_hide_order,
                     filter_duplicate_users, logger, get_wxpay, get_channel_wxpay,
                     select_payment_channel, connected_devices)
from models import ORDER_STATUS, BUSINESS_STATUS_MAP, BUSINESS_STATUS_ACTIVE

bp = Blueprint('admin', __name__)
import config


# ============================================
# 管理员认证
# ============================================

@bp.route('/admin/login', methods=['POST'])
def admin_login():
    try:
        ip = request.remote_addr
        allowed, wait = check_rate(ip)
        if not allowed:
            return json_response(message="登录失败次数过多，请{}秒后再试".format(wait), code=429)
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        if not all([username, password]):
            return json_response(message='用户名和密码不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM admin_users WHERE username = %s', (username,))
        admin = cursor.fetchone()
        if not admin or not check_password_hash(admin['password_hash'], password):
            conn.close()
            fail(ip)
            return json_response(message='用户名或密码错误', code=400)
        ok(ip)
        token = secrets.token_hex(16)
        session['admin_id'] = admin['id']
        session['admin_username'] = admin['username']
        session['admin_role'] = admin['role']
        cursor.execute('UPDATE admin_users SET auth_token=%s WHERE id=%s', (token, admin['id']))
        conn.commit()
        conn.close()
        return json_response({'id': admin['id'], 'username': admin['username'], 'role': admin['role'], 'token': token})
    except Exception as e:
        logger.error(f'[admin_login] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/logout', methods=['POST'])
@require_auth
def admin_logout():
    session.clear()
    return json_response(message='登出成功')


@bp.route('/admin/info', methods=['GET', 'POST'])
@require_auth
def admin_info():
    return json_response({'id': session['admin_id'], 'username': session['admin_username'], 'role': session['admin_role']})


@bp.route('/admin/password', methods=['PUT'])
@require_auth
def admin_change_password():
    try:
        data = request.get_json()
        old_password = data.get('old_password')
        new_password = data.get('new_password')
        if not all([old_password, new_password]):
            return json_response(message='旧密码和新密码不能为空', code=400)
        if len(new_password) < 6:
            return json_response(message='新密码长度不能少于6位', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT password_hash FROM admin_users WHERE id = %s', (session['admin_id'],))
        admin = cursor.fetchone()
        if not check_password_hash(admin['password_hash'], old_password):
            conn.close()
            fail(ip)
            return json_response(message='旧密码错误', code=400)
        cursor.execute('UPDATE admin_users SET password_hash = %s WHERE id = %s',
                       (generate_password_hash(new_password), session['admin_id']))
        conn.commit()
        conn.close()
        return json_response(message='密码修改成功')
    except Exception as e:
        logger.error(f'[admin_change_password] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 商户管理
# ============================================

@bp.route('/merchants', methods=['GET'])
@require_auth
def get_merchants():
    try:
        agent_id = request.args.get('agent_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        if agent_id:
            cursor.execute('SELECT * FROM merchants WHERE agent_id = %s ORDER BY created_at DESC', (agent_id,))
        else:
            cursor.execute('SELECT * FROM merchants ORDER BY created_at DESC')
        merchants = cursor.fetchall()
        conn.close()
        return json_response([dict(m) for m in merchants])
    except Exception as e:
        logger.error(f'[get_merchants] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchants', methods=['POST'])
@require_auth
def create_merchant():
    try:
        data = request.get_json()
        name = data.get('name')
        contact_name = data.get('contact_name')
        contact_phone = data.get('contact_phone')
        password = data.get('password') or generate_random_password()
        agent_id = data.get('agent_id')
        if not all([name, contact_phone]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM merchants WHERE contact_phone = %s', (contact_phone,))
        if cursor.fetchone():
            conn.close()
            return json_response(message='该手机号已注册', code=400)
        if agent_id:
            cursor.execute('SELECT id FROM agents WHERE id = %s AND status = 1', (agent_id,))
            if not cursor.fetchone():
                conn.close()
                return json_response(message='代理商不存在或已被禁用', code=400)
        password_hash = generate_password_hash(password)
        cursor.execute('INSERT INTO merchants (name, contact_name, contact_phone, password_hash, agent_id) VALUES (%s, %s, %s, %s, %s)',
                       (name, contact_name or name, contact_phone, password_hash, agent_id))
        merchant_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'id': merchant_id, 'password': password, 'message': '商家创建成功'})
    except Exception as e:
        logger.error(f'[create_merchant] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchants/<int:merchant_id>', methods=['PUT'])
@require_auth
def update_merchant(merchant_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        updates, params = [], []
        for field in ['name', 'contact_name', 'contact_phone', 'status', 'agent_id']:
            if field in data and data[field] is not None:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if 'agent_id' in data and data['agent_id'] is not None:
            cursor.execute('SELECT id FROM agents WHERE id = %s AND status = 1', (data['agent_id'],))
            if not cursor.fetchone():
                conn.close()
                return json_response(message='代理商不存在或已被禁用', code=400)
        if updates:
            params.append(merchant_id)
            cursor.execute(f'UPDATE merchants SET {", ".join(updates)} WHERE id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='商家信息更新成功')
    except Exception as e:
        logger.error(f'[update_merchant] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchants/<int:merchant_id>', methods=['DELETE'])
@require_auth
def delete_merchant(merchant_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM merchants WHERE id = %s', (merchant_id,))
        conn.commit()
        conn.close()
        return json_response(message='商家已删除')
    except Exception as e:
        logger.error(f'[delete_merchant] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 网点管理
# ============================================

@bp.route('/locations', methods=['GET'])
@require_auth
def get_locations():
    try:
        merchant_id = request.args.get('merchant_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        if merchant_id:
            cursor.execute('SELECT l.*, m.name as merchant_name FROM locations l JOIN merchants m ON l.merchant_id = m.id WHERE l.merchant_id = %s ORDER BY l.created_at DESC', (merchant_id,))
        else:
            cursor.execute('SELECT l.*, m.name as merchant_name FROM locations l JOIN merchants m ON l.merchant_id = m.id ORDER BY l.created_at DESC')
        locations = cursor.fetchall()
        conn.close()
        return json_response([dict(loc) for loc in locations])
    except Exception as e:
        logger.error(f'[get_locations] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/locations', methods=['POST'])
@require_auth
def create_location():
    try:
        data = request.get_json()
        merchant_id = data.get('merchant_id')
        name = data.get('name')
        address = data.get('address')
        if not all([merchant_id, name]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO locations (merchant_id, name, address, longitude, latitude) VALUES (%s, %s, %s, %s, %s)',
                  (data.get('merchant_id'), data.get('name',''), data.get('address',''), data.get('longitude'), data.get('latitude')))
        location_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'id': location_id, 'message': '网点创建成功'})
    except Exception as e:
        logger.error(f'[create_location] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/locations/<int:location_id>', methods=['PUT'])
@require_auth
def update_location(location_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        updates, params = [], []
        basic_fields = ['name', 'address', 'longitude', 'latitude', 'status']
        withdraw_fields = ['withdraw_enabled', 'auto_approve_day', 'auto_approve_time', 'auto_approve_rate',
                           'click_free_count', 'anti_test_minutes', 'anti_test_auto_refund', 'show_refunding_status',
                           'hide_ratio', 'whitelist_phones', 'duplicate_filter_enabled', 'duplicate_filter_days',
                           'duplicate_filter_limit']
        for field in basic_fields:
            if field in data:
                updates.append(f'{field} = %s')
                params.append(data[field])
        for field in withdraw_fields:
            if field in data and data[field] is not None:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if updates:
            params.append(location_id)
            cursor.execute(f'UPDATE locations SET {", ".join(updates)} WHERE id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='网点更新成功')
    except Exception as e:
        logger.error(f'[update_location] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/locations/<int:location_id>', methods=['DELETE'])
@require_auth
def delete_location(location_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM locations WHERE id = %s', (location_id,))
        conn.commit()
        conn.close()
        return json_response(message='网点已删除')
    except Exception as e:
        logger.error(f'[delete_location] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 柜组管理
# ============================================

@bp.route('/cabinet-groups', methods=['GET'])
@require_auth
def get_cabinet_groups():
    try:
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        if location_id:
            cursor.execute('SELECT cg.*, l.name as location_name, COUNT(DISTINCT c.id) as cabinet_count, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots FROM cabinet_groups cg JOIN locations l ON cg.location_id = l.id LEFT JOIN cabinets c ON c.group_id = cg.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE cg.location_id = %s GROUP BY cg.id ORDER BY cg.created_at DESC', (location_id,))
        else:
            cursor.execute('SELECT cg.*, l.name as location_name, COUNT(DISTINCT c.id) as cabinet_count, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots FROM cabinet_groups cg JOIN locations l ON cg.location_id = l.id LEFT JOIN cabinets c ON c.group_id = cg.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id GROUP BY cg.id ORDER BY cg.created_at DESC')
        groups = cursor.fetchall()
        conn.close()
        return json_response([dict(g) for g in groups])
    except Exception as e:
        logger.error(f'[get_cabinet_groups] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups', methods=['POST'])
@require_auth
def create_cabinet_group():
    try:
        data = request.get_json()
        location_id = data.get('location_id')
        group_code = data.get('group_code')
        name = data.get('name')
        screen_url = data.get('screen_url')
        if not all([location_id, group_code]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM cabinet_groups WHERE group_code = %s', (group_code,))
        if cursor.fetchone():
            conn.close()
            return json_response(message='柜组编号已存在', code=400)
        cursor.execute('INSERT INTO cabinet_groups (location_id, group_code, name, screen_url) VALUES (%s, %s, %s, %s)',
                       (location_id, group_code, name or group_code, screen_url))
        group_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'id': group_id, 'message': '柜组创建成功'})
    except Exception as e:
        logger.error(f'[create_cabinet_group] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups/<int:group_id>', methods=['PUT'])
@require_auth
def update_cabinet_group(group_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cabinet_groups WHERE id = %s', (group_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='柜组不存在', code=404)
        updates, params = [], []
        for field in ['name', 'screen_url', 'status']:
            if field in data:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if updates:
            params.append(group_id)
            cursor.execute(f'UPDATE cabinet_groups SET {", ".join(updates)} WHERE id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='柜组更新成功')
    except Exception as e:
        logger.error(f'[update_cabinet_group] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups/<int:group_id>', methods=['DELETE'])
@require_auth
def delete_cabinet_group(group_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM cabinets WHERE group_id = %s', (group_id,))
        if cursor.fetchone()[0] > 0:
            conn.close()
            return json_response(message='该柜组下存在柜体，请先删除柜体', code=400)
        cursor.execute('DELETE FROM cabinet_groups WHERE id = %s', (group_id,))
        conn.commit()
        conn.close()
        return json_response(message='柜组已删除')
    except Exception as e:
        logger.error(f'[delete_cabinet_group] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups/<int:group_id>/cabinets', methods=['GET'])
@require_auth
def get_group_cabinets(group_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT c.*, l.name as location_name, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots, CASE WHEN c.last_heartbeat >= datetime(\'now\', \'-30 seconds\') THEN 1 ELSE 0 END as is_online FROM cabinets c JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE c.group_id = %s GROUP BY c.id ORDER BY c.created_at DESC', (group_id,))
        cabinets = cursor.fetchall()
        conn.close()
        return json_response([dict(c) for c in cabinets])
    except Exception as e:
        logger.error(f'[get_group_cabinets] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups/<int:group_id>/available-slots', methods=['GET'])
def get_group_available_slots(group_id):
    try:
        slot_size = request.args.get('slot_size')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cabinet_groups WHERE id = %s', (group_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='柜组不存在', code=404)
        if slot_size:
            cursor.execute('SELECT cs.*, c.cabinet_code, c.name as cabinet_name, cg.group_code FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id JOIN cabinet_groups cg ON c.group_id = cg.id WHERE c.group_id = %s AND cs.status = 1 AND cs.slot_size = %s ORDER BY cs.slot_number', (group_id, slot_size))
        else:
            cursor.execute('SELECT cs.*, c.cabinet_code, c.name as cabinet_name, cg.group_code FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id JOIN cabinet_groups cg ON c.group_id = cg.id WHERE c.group_id = %s AND cs.status = 1 ORDER BY cs.slot_number', (group_id,))
        slots = cursor.fetchall()
        conn.close()
        return json_response([dict(s) for s in slots])
    except Exception as e:
        logger.error(f'[get_group_available_slots] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups/<int:group_id>/active-orders', methods=['GET'])
def get_group_active_orders(group_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM cabinet_groups WHERE id = %s', (group_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='柜组不存在', code=404)
        cursor.execute('SELECT o.id as order_id, o.order_no, o.user_phone, o.access_code, o.deposit_amount, o.slot_id, o.compartment_number, o.slot_size, o.cabinet_id, o.cabinet_code, o.cabinet_name, o.store_time, o.transaction_id, o.group_id FROM orders o WHERE o.group_id = %s AND o.status IN (2, 3, 5) ORDER BY o.id DESC', (group_id,))
        orders = cursor.fetchall()
        conn.close()
        return json_response([dict(o) for o in orders])
    except Exception as e:
        logger.error(f'[get_group_active_orders] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinet-groups/by-code/<group_code>', methods=['GET'])
def get_cabinet_group_by_code(group_code):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT cg.*, l.name as location_name, l.address as location_address, l.usage_rules FROM cabinet_groups cg JOIN locations l ON cg.location_id = l.id WHERE cg.group_code = %s', (group_code,))
        group = cursor.fetchone()
        if not group:
            conn.close()
            return json_response(message='柜组不存在', code=404)
        cursor.execute('SELECT c.id, c.cabinet_code, c.name as cabinet_name, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots FROM cabinets c LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE c.group_id = %s GROUP BY c.id', (group['id'],))
        cabinets = cursor.fetchall()
        conn.close()
        return json_response({**dict(group), 'cabinets': [dict(c) for c in cabinets]})
    except Exception as e:
        logger.error(f'[get_cabinet_group_by_code] {e}')
        return json_response(message=str(e), code=500)



@bp.route("/cabinets/by-group/<group_code>", methods=["GET"])
def get_cabinets_by_group_code(group_code):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cg.*, l.name as location_name, l.address as location_address, l.deposit_amount FROM cabinet_groups cg JOIN locations l ON cg.location_id = l.id WHERE cg.group_code = %s", (group_code,))
        group = cursor.fetchone()
        if not group:
            conn.close()
            return json_response({"code": 404, "message": "group not found"})
        conn.close()
        return json_response({"code": 0, "data": {
            "name": group["location_name"] or group.get("name", ""),
            "location": group["location_address"] or "",
            "deposit": float(group.get("deposit_amount") or 20)
        }})
    except Exception as e:
        logger.error("[get_cabinets_by_group_code] %s" % str(e))
        return json_response({"code": 500, "message": str(e)})

# ============================================
# 柜体管理
# ============================================

@bp.route('/cabinets', methods=['GET'])
@require_auth
def get_cabinets():
    try:
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        if location_id:
            cursor.execute('SELECT c.*, l.name as location_name FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.location_id = %s ORDER BY c.created_at DESC', (location_id,))
        else:
            cursor.execute('SELECT c.*, l.name as location_name FROM cabinets c JOIN locations l ON c.location_id = l.id ORDER BY c.created_at DESC')
        cabinets = cursor.fetchall()
        conn.close()
        return json_response([dict(c) for c in cabinets])
    except Exception as e:
        logger.error(f'[get_cabinets] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets', methods=['POST'])
@require_auth
def create_cabinet():
    try:
        data = request.get_json()
        location_id = data.get('location_id')
        group_id = data.get('group_id')
        cabinet_code = data.get('cabinet_code')
        name = data.get('name')
        if not name:
            return json_response(message='柜体名称不能为空', code=400)
        if not cabinet_code:
            import time
            cabinet_code = 'C' + str(int(time.time() * 100))[-6:]
        conn = get_db()
        cursor = conn.cursor()
        if group_id and location_id:
            cursor.execute('SELECT * FROM cabinet_groups WHERE id = %s AND location_id = %s', (group_id, location_id))
            if not cursor.fetchone():
                conn.close()
                return json_response(message='柜组不存在或不属于该网点', code=400)
        cursor.execute('SELECT id FROM cabinets WHERE cabinet_code = %s', (cabinet_code,))
        if cursor.fetchone():
            conn.close()
            return json_response(message='柜体编号已存在', code=400)
        cursor.execute('INSERT INTO cabinets (location_id, group_id, cabinet_code, name, total_slots, mainboard_device_id, mainboard_source, charge_mode, deposit_amount, business_status) VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s)',
                       (location_id, group_id, cabinet_code, name, data.get('mainboard_device_id', ''), data.get('mainboard_source', 'YBM'), data.get('charge_mode', 'deposit'), data.get('deposit_amount', 20), 'inactive'))
        cabinet_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'id': cabinet_id, 'message': '柜体创建成功，请添加主板和格子'})
    except Exception as e:
        logger.error(f'[create_cabinet] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>', methods=['GET'])
@require_auth
def get_cabinet(cabinet_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cabinets WHERE id=%s', (cabinet_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return json_response(message='柜体不存在', code=404)
        result = dict(row)
        # 获取柜门列表
        cursor.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s ORDER BY slot_number', (cabinet_id,))
        result['slots'] = [dict(r) for r in cursor.fetchall()]
        # 获取主板信息
        cursor.execute('SELECT * FROM mainboards WHERE cabinet_id=%s', (cabinet_id,))
        result['mainboards'] = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[get_cabinet] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>', methods=['PUT'])
@require_auth
def update_cabinet(cabinet_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        if 'group_id' in data:
            group_id = data['group_id']
            if group_id:
                cursor.execute('SELECT location_id FROM cabinets WHERE id = %s', (cabinet_id,))
                cabinet = cursor.fetchone()
                if cabinet:
                    cursor.execute('SELECT * FROM cabinet_groups WHERE id = %s AND location_id = %s', (group_id, cabinet['location_id']))
                    if not cursor.fetchone():
                        conn.close()
                        return json_response(message='柜组不存在或不属于该网点', code=400)
        updates, params = [], []
        for field in ['name', 'total_slots', 'status', 'group_id', 'deposit_amount', 'mainboard_device_id',
                       'mainboard_source', 'charge_mode', 'business_status', 'business_hours', 'customer_phone', 'per_use_price']:
            if field in data:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if updates:
            params.append(cabinet_id)
            cursor.execute(f'UPDATE cabinets SET {", ".join(updates)} WHERE id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='柜体更新成功')
    except Exception as e:
        logger.error(f'[update_cabinet] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>', methods=['DELETE'])
@require_auth
def delete_cabinet(cabinet_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM cabinet_slots WHERE cabinet_id = %s', (cabinet_id,))
        cursor.execute('DELETE FROM cabinets WHERE id = %s', (cabinet_id,))
        conn.commit()
        conn.close()
        return json_response(message='柜体已删除')
    except Exception as e:
        logger.error(f'[delete_cabinet] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/info/<int:cabinet_id>', methods=['GET'])
def get_cabinet_public_info(cabinet_id):
    """公开接口 - H5页面获取设备押金信息"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT c.id, c.deposit_amount, c.charge_mode, c.per_use_price, c.name, c.last_heartbeat, l.name as location_name, l.allow_h5_to_mp, l.mp_appid, l.h5_url FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.id=%s', (cabinet_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return json_response(message='设备不存在', code=404)
        return json_response(data=dict(row))
    except Exception as e:
        logger.error(f'[get_cabinet_public_info] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/by-mainboard/<mainboard_id>', methods=['GET'])

def get_cabinet_by_mainboard(mainboard_id):
    """通过主板编号获取柜体配置（APK使用）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT c.*, g.name as group_name, l.name as location_name, l.address as location_address, l.usage_rules, l.show_slot_count, '' as business_hours, '' as customer_phone, c.business_status, c.usage_rules, l.usage_rules as location_usage_rules FROM cabinets c LEFT JOIN cabinet_groups g ON c.group_id = g.id LEFT JOIN locations l ON c.location_id = l.id WHERE c.mainboard_device_id = %s", (mainboard_id,))
        cabinet = cursor.fetchone()
        # 如果柜体没有自己的寄存规则，则用网点的
        if not cabinet:
            conn.close()
            return json_response(message='未找到该主板对应的柜体', code=404)
        # 检查营业状态 + 刷新心跳
        biz_status = cabinet['business_status'] if cabinet['business_status'] else 'inactive'
        result = dict(cabinet)
        try:
            c_up = conn.cursor()
            if biz_status == 'inactive':
                c_up.execute("UPDATE cabinets SET business_status='active', last_heartbeat=NOW() WHERE id=%s", (cabinet['id'],))
                biz_status = 'active'
                logger.info(f"[自动激活] 设备 {mainboard_id} 已自动恢复激活")
            else:
                c_up.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE id=%s", (cabinet['id'],))
            conn.commit()
            result['last_heartbeat'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            logger.error(f"[心跳刷新] 失败: {e}")
        result['biz_status'] = biz_status
        # 寄存规则只用设备自己的，不用网点的
        # if not result.get('usage_rules'):
        #     result['usage_rules'] = result.get('location_usage_rules', '')
        cursor.execute("SELECT cs.*, m.board_index, m.slot_count FROM cabinet_slots cs LEFT JOIN mainboards m ON cs.mainboard_id = m.id WHERE cs.cabinet_id = %s ORDER BY cs.slot_number", (cabinet['id'],))
        slots = [dict(s) for s in cursor.fetchall()]
        result['slots'] = slots
        result['available_slots'] = sum(1 for s in slots if s.get('status') == 1)
        result['total_slots_count'] = len(slots)
        # 查询主板串口配置
        cursor.execute('SELECT id, board_index, slot_count, name, serial_port, baud_rate, protocol FROM mainboards WHERE cabinet_id = %s ORDER BY board_index', (cabinet['id'],))
        mainboards = [dict(m) for m in cursor.fetchall()]
        result['mainboards'] = mainboards
        # 兼容：用第一块主板的串口作为默认串口配置；没有主板时用协议默认值
        if mainboards:
            sp = mainboards[0].get('serial_port', 'ttyS1')
            result['serial_port'] = sp if sp.startswith('/dev/') else '/dev/' + sp
            result['baud_rate'] = mainboards[0].get('baud_rate', 9600)
            result['serial_type'] = 'BaseSerial'
        else:
            # 没有主板时用协议默认串口配置
            protocol = result.get('mainboard_source', 'YBM')
            from routes.device import get_board_config as _gbc
            _sp, _br, _pr = _gbc(protocol)
            result['serial_port'] = _sp
            result['baud_rate'] = _br
            result['serial_type'] = 'BaseSerial'
        # 替换寄存规则中的占位符
        rules = result.get('usage_rules', '')
        if rules:
            da = result.get('deposit_amount', 0)
            rules = rules.replace('{deposit_amount}', str(int(da) if da and da == int(da) else da))
            # 将字面量\n替换为真换行符，确保前端正确分行
            rules = rules.replace('\\n', '\n')
            result['usage_rules'] = rules
        # 补充location级别的配置
        if result.get('location_id'):
            cursor.execute('SELECT allow_h5_to_mp, h5_url FROM locations WHERE id=%s', (result['location_id'],))
            loc_row = cursor.fetchone()
            if loc_row:
                result['allow_h5_to_mp'] = loc_row['allow_h5_to_mp'] or 0
                result['mp_appid'] = config.WX_MP_APP_ID  # 用户端小程序AppID
                result['mp_path'] = 'pages/subscribe/subscribe'
                result['h5_url'] = loc_row['h5_url'] or ''
        # 计算设备在线状态
        if result.get('last_heartbeat'):
            try:
                hb = result['last_heartbeat']
                if isinstance(hb, str):
                    hb = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
                result['is_online'] = (datetime.now() - hb).total_seconds() < 300
            except:
                result['is_online'] = False
        else:
            result['is_online'] = False
        # 补充空闲柜门列表（供H5前端显示）
        available_slots = [s for s in slots if s.get('status') == 1]
        result['available_slots_list'] = [{'id': s['id'], 'slot_number': s['slot_number'], 'slot_size': s.get('slot_size','M'), 'slot_label': s.get('slot_label','') or ''} for s in available_slots[:10]]
        # 根据收费模式生成屏幕大字显示文本
        charge_mode = result.get('charge_mode', 'deposit')
        if charge_mode == 'free':
                        result['display_text'] = '\xe5\x85\x8d\xe8\xb4\xb9\xe5\xaf\x84\xe5\xad\x98'
        elif charge_mode == 'per_use':
            price = result.get('per_use_price', 0)
            result['display_text'] = f'?{int(price)}??' if price and price == int(price) else f'?{price}??'
        else:  # deposit
            result['display_text'] = '????'
        result['cabinet_name'] = result.get('name', '')
        conn.close()
        return json_response(result, headers={'Cache-Control': 'public, max-age=300'})
    except Exception as e:
        logger.error(f'[get_cabinet_by_mainboard] {e}')
        try:
            conn.close()
        except:
            pass
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/heartbeat', methods=['POST'])
def cabinet_heartbeat(cabinet_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE cabinets SET last_heartbeat = %s WHERE id = %s', (datetime.now(), cabinet_id))
        conn.commit()
        conn.close()
        return json_response(message='心跳更新成功')
    except Exception as e:
        logger.error(f'[cabinet_heartbeat] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 主板管理（新功能：串口/波特率）
# ============================================

@bp.route('/cabinets/<int:cabinet_id>/mainboards', methods=['GET'])
def get_mainboards(cabinet_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT mb.*, COUNT(cs.id) as slot_count_actual, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as free_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as used_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots, MIN(cs.display_number) as min_display, MAX(cs.display_number) as max_display FROM mainboards mb LEFT JOIN cabinet_slots cs ON mb.id = cs.mainboard_id WHERE mb.cabinet_id = %s GROUP BY mb.id ORDER BY mb.board_index', (cabinet_id,))
        mainboards = [dict(m) for m in cursor.fetchall()]
        conn.close()
        return json_response(mainboards)
    except Exception as e:
        logger.error(f'[get_mainboards] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/mainboards', methods=['POST'])
@require_auth
def create_mainboard(cabinet_id):
    """创建主板（支持serial_port和baud_rate）"""
    try:
        data = request.get_json()
        slot_count = data.get('slot_count', 16)
        board_name = data.get('name', '')
        serial_port = data.get('serial_port', '')
        baud_rate = data.get('baud_rate', 0)

        if slot_count < 1 or slot_count > 16:
            return json_response(message='格子数量需在1-16之间', code=400)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cabinets WHERE id = %s', (cabinet_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在', code=404)

        cabinet_code = cabinet['cabinet_code']

        # 串口和波特率必须手动选择，不自动填充
        if not serial_port:
            return json_response(message='请选择串口路径', code=400)
        if not baud_rate:
            return json_response(message='请选择波特率', code=400)

        cursor.execute('SELECT MAX(board_index) as max_idx FROM mainboards WHERE cabinet_id = %s', (cabinet_id,))
        board_index = (cursor.fetchone()['max_idx'] or 0) + 1
        cursor.execute('SELECT MAX(display_number) as max_dn FROM cabinet_slots WHERE cabinet_id = %s', (cabinet_id,))
        next_display = (cursor.fetchone()['max_dn'] or 0) + 1
        cursor.execute('SELECT MAX(slot_number) as max_sn FROM cabinet_slots WHERE cabinet_id = %s', (cabinet_id,))
        next_slot = (cursor.fetchone()['max_sn'] or 0) + 1

        if not board_name:
            board_name = f'主板{board_index}'

        cursor.execute('INSERT INTO mainboards (cabinet_id, board_index, slot_count, name, serial_port, baud_rate) VALUES (%s, %s, %s, %s, %s, %s)',
                       (cabinet_id, board_index, slot_count, board_name, serial_port, baud_rate))
        mainboard_id = cursor.lastrowid

        for i in range(slot_count):
            cursor.execute('INSERT INTO cabinet_slots (cabinet_id, mainboard_id, slot_number, display_number, slot_size, status, cabinet_code) VALUES (%s, %s, %s, %s, \'M\', 1, %s)',
                           (cabinet_id, mainboard_id, next_slot + i, next_display + i, cabinet_code))

        cursor.execute('UPDATE cabinets SET total_slots = (SELECT COUNT(*) FROM cabinet_slots WHERE cabinet_id = %s) WHERE id = %s', (cabinet_id, cabinet_id))
        conn.commit()
        conn.close()
        return json_response({'id': mainboard_id, 'board_index': board_index, 'serial_port': serial_port, 'baud_rate': baud_rate,
                              'message': f'主板{board_index}添加成功，含{slot_count}个格子，串口{serial_port}，波特率{baud_rate}'})
    except Exception as e:
        logger.error(f'[create_mainboard] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/mainboards/<int:mainboard_id>', methods=['PUT'])
@require_auth
def update_mainboard(cabinet_id, mainboard_id):
    """更新主板（支持serial_port和baud_rate）"""
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        updates, params = [], []
        for field in ['name', 'serial_port', 'baud_rate']:
            if field in data:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if updates:
            params.extend([cabinet_id, mainboard_id])
            cursor.execute(f'UPDATE mainboards SET {", ".join(updates)} WHERE cabinet_id = %s AND id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='主板更新成功')
    except Exception as e:
        logger.error(f'[update_mainboard] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/mainboards/<int:mainboard_id>', methods=['DELETE'])
@require_auth
def delete_mainboard(cabinet_id, mainboard_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as cnt FROM cabinet_slots WHERE mainboard_id = %s AND status = 2', (mainboard_id,))
        if cursor.fetchone()['cnt'] > 0:
            conn.close()
            return json_response(message='有格子正在使用中，无法删除', code=400)
        cursor.execute('SELECT * FROM mainboards WHERE id = %s AND cabinet_id = %s', (mainboard_id, cabinet_id))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='主板不存在', code=404)
        cursor.execute('UPDATE orders SET slot_id = NULL WHERE slot_id IN (SELECT id FROM cabinet_slots WHERE mainboard_id = %s)', (mainboard_id,))
        cursor.execute('DELETE FROM cabinet_slots WHERE mainboard_id = %s', (mainboard_id,))
        cursor.execute('DELETE FROM mainboards WHERE id = %s', (mainboard_id,))
        cursor.execute('SELECT id FROM cabinet_slots WHERE cabinet_id = %s ORDER BY slot_number', (cabinet_id,))
        for idx, slot in enumerate(cursor.fetchall()):
            cursor.execute('UPDATE cabinet_slots SET display_number = %s WHERE id = %s', (idx + 1, slot['id']))
        cursor.execute('SELECT id FROM mainboards WHERE cabinet_id = %s ORDER BY board_index', (cabinet_id,))
        for idx, board in enumerate(cursor.fetchall()):
            cursor.execute('UPDATE mainboards SET board_index = %s WHERE id = %s', (idx + 1, board['id']))
        cursor.execute('UPDATE cabinets SET total_slots = (SELECT COUNT(*) FROM cabinet_slots WHERE cabinet_id = %s) WHERE id = %s', (cabinet_id, cabinet_id))
        conn.commit()
        conn.close()
        return json_response(message='主板已删除，编号已重新排列')
    except Exception as e:
        logger.error(f'[delete_mainboard] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 柜格管理
# ============================================

@bp.route('/cabinets/<int:cabinet_id>/slots', methods=['GET'])
def cabinet_slots(cabinet_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT cs.*, o.user_phone, o.access_code, o.store_time FROM cabinet_slots cs LEFT JOIN orders o ON cs.id = o.slot_id AND o.status IN (2, 3, 5) WHERE cs.cabinet_id = %s ORDER BY cs.slot_number', (cabinet_id,))
        slots = cursor.fetchall()
        conn.close()
        return json_response([dict(s) for s in slots])
    except Exception as e:
        logger.error(f'[cabinet_slots] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/slots/<int:slot_id>/status', methods=['PUT'])
@require_auth
def update_slot_status(cabinet_id, slot_id):
    try:
        data = request.get_json()
        status = data.get('status')
        if status is None:
            return json_response(message='状态不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE cabinet_slots SET status = %s WHERE id = %s', (status, slot_id))
        conn.commit()
        conn.close()
        return json_response(message='柜格状态更新成功')
    except Exception as e:
        logger.error(f'[update_slot_status] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/slots/<int:slot_id>/open', methods=['POST'])
@require_auth
def open_single_slot(cabinet_id, slot_id):
    """单个开柜门"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT cs.slot_number, cs.board_no, cs.lock_no, c.mainboard_device_id FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id WHERE cs.id = %s AND cs.cabinet_id = %s', (slot_id, cabinet_id))
        row = c.fetchone()
        conn.close()
        if not row:
            return json_response(message='柜门不存在', code=404)
        if not row['mainboard_device_id']:
            return json_response(message='未找到主板编号', code=400)
        bn = row.get('board_no') or 1
        ln = row.get('lock_no') or row['slot_number']
        send_open_lock(str(row['mainboard_device_id']), int(bn), int(ln), None, '', slot_number=row['slot_number'])
        return json_response(message=f'{row["slot_number"]}号柜门开锁指令已发送')
    except Exception as e:
        logger.error(f'[open_single_slot] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/open-all', methods=['POST'])
@require_auth
def open_all_normal_slots(cabinet_id):
    """一键开门 - 只开正常柜门"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT cs.slot_number, cs.board_no, cs.lock_no, c.mainboard_device_id FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id WHERE cs.cabinet_id = %s AND cs.status = 1', (cabinet_id,))
        slots = c.fetchall()
        conn.close()
        if not slots:
            return json_response(message='没有可开的正常柜门', code=400)
        did = str(slots[0]['mainboard_device_id'])
        opened = []
        for s in slots:
            bn2 = s.get('board_no') or 1
            ln2 = s.get('lock_no') or s['slot_number']
            send_open_lock(did, int(bn2), int(ln2), None, '', slot_number=s['slot_number'])
            opened.append(s['slot_number'])
        return json_response(message=f'已发送{len(opened)}个柜门开锁指令', data={'opened': opened})
    except Exception as e:
        logger.error(f'[open_all] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/cabinets/<int:cabinet_id>/clear-all', methods=['POST'])
@require_auth
def clear_all_slots(cabinet_id):
    """一键清柜 - 释放所有占用和故障的柜格"""
    try:
        conn = get_db()
        c = conn.cursor()
        # 关闭所有相关订单
        c.execute('UPDATE orders SET status = 5, retrieve_time = %s WHERE cabinet_id = %s AND status = 2', (datetime.now(), cabinet_id))
        # 释放所有柜格
        c.execute('UPDATE cabinet_slots SET status = 1 WHERE cabinet_id = %s AND status != 1', (cabinet_id,))
        conn.commit()
        conn.close()
        return json_response(message='一键清柜完成，所有柜格已释放')
    except Exception as e:
        logger.error(f'[clear_all_slots] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 订单管理
# ============================================

@bp.route('/orders', methods=['GET'])
@require_auth
def get_orders():
    try:
        status = request.args.get('status', type=int)
        cabinet_id = request.args.get('cabinet_id', type=int)
        merchant_id = request.args.get('merchant_id', type=int)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit

        conn = get_db()
        cursor = conn.cursor()
        where_clauses = ['1=1']
        params = []
        if status:
            where_clauses.append('o.status = %s')
            params.append(status)
        if cabinet_id:
            where_clauses.append('o.cabinet_id = %s')
            params.append(cabinet_id)
        if merchant_id:
            where_clauses.append('l.merchant_id = %s')
            params.append(merchant_id)
        if start_date:
            where_clauses.append('DATE(o.created_at) >= %s')
            params.append(start_date)
        if end_date:
            where_clauses.append('DATE(o.created_at) <= %s')
            params.append(end_date)
        # 前端传入的额外筛选参数
        phone = request.args.get('phone')
        order_id = request.args.get('order_id', type=int)
        logic_mark = request.args.get('logic_mark')
        location_id = request.args.get('location_id', type=int)
        device_id = request.args.get('device_id', type=int)
        if phone:
            where_clauses.append('o.user_phone LIKE %s')
            params.append(f'%{phone}%')
        if order_id:
            where_clauses.append('o.id = %s')
            params.append(order_id)
        if logic_mark:
            where_clauses.append('o.logic_mark = %s')
            params.append(logic_mark)
        if location_id:
            where_clauses.append('l.id = %s')
            params.append(location_id)
        if device_id:
            where_clauses.append('o.cabinet_id = %s')
            params.append(device_id)
        where_sql = ' AND '.join(where_clauses)

        cursor.execute(f'SELECT o.*, c.cabinet_code, c.name as cabinet_name, l.merchant_id, m.name as merchant_name FROM orders o LEFT JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN locations l ON c.location_id = l.id LEFT JOIN merchants m ON l.merchant_id = m.id WHERE {where_sql} ORDER BY o.created_at DESC LIMIT 5000 OFFSET 0', params)
        all_orders = cursor.fetchall()

        # 管理员不应用隐藏
        filtered_orders = list(all_orders)
        total = len(filtered_orders)
        paginated = filtered_orders[offset:offset + limit]
        conn.close()
        return json_response({'list': [dict(o) for o in paginated], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[get_orders] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/orders/<int:order_id>', methods=['GET'])
@require_auth
def get_order_detail(order_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, c.cabinet_code, c.name as cabinet_name FROM orders o LEFT JOIN cabinets c ON o.cabinet_id = c.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        cursor.execute('SELECT * FROM payments WHERE order_id = %s ORDER BY created_at', (order_id,))
        payments = cursor.fetchall()
        conn.close()
        return json_response({'order': dict(order), 'payments': [dict(p) for p in payments]})
    except Exception as e:
        logger.error(f'[get_order_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/orders/<int:order_id>/cancel', methods=['POST'])
@require_auth
def cancel_order(order_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if order['status'] not in [1, 2]:
            conn.close()
            return json_response(message='订单状态不允许取消', code=400)
        cursor.execute('UPDATE orders SET status = 5 WHERE id = %s', (order_id,))
        if order['slot_id']:
            cursor.execute('SELECT COUNT(*) FROM orders WHERE slot_id = %s AND status IN (1,2) AND id != %s', (order['slot_id'], order_id))
        if cursor.fetchone()[0] == 0:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        conn.commit()
        conn.close()
        return json_response(message='订单已取消')
    except Exception as e:
        logger.error(f'[cancel_order] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/orders/<int:order_id>/open-lock', methods=['POST'])
@require_auth
def open_order_lock(order_id):
    """远程开柜"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, c.mainboard_device_id, c.mainboard_source, cs.board_no, cs.lock_no FROM orders o LEFT JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if order['status'] not in [2, 3, 5]:
            conn.close()
            return json_response(message='订单状态不允许开柜', code=400)
        device_id = order['mainboard_device_id']
        board_no = order['board_no'] or 1
        lock_no = order['lock_no'] or order['compartment_number'] or 1
        protocol = order['mainboard_source'] or _get_device_protocol(str(dict(order).get('mainboard_device_id',''))) or 'YBM'
        if not device_id:
            conn.close()
            return json_response(message='该设备未配置主板ID', code=400)
        success = send_open_lock(device_id, board_no, lock_no, protocol, order.get('order_no', str(order_id)))
        if success:
            cursor.execute('INSERT INTO payments (order_id, type, amount, transaction_id, status, created_at) VALUES (%s, 5, 0, %s, 1, %s)',
                           (order_id, f'MANUAL_OPEN_{order_id}', datetime.now()))
            conn.commit()
            conn.close()
            return json_response(message='开锁指令已发送')
        conn.close()
        return json_response(message='设备不在线', code=400)
    except Exception as e:
        logger.error(f'[open_order_lock] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/orders/<int:order_id>/refund', methods=['POST'])
@require_auth
def refund_order(order_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        if not order['transaction_id']:
            conn.close()
            return json_response(message='该订单无支付记录', code=400)
        if order['status'] not in [2, 3, 5]:
            conn.close()
            return json_response(message='订单状态不允许退款', code=400)
        cursor.execute('SELECT * FROM payments WHERE order_id = %s AND type = 2', (order_id,))
        if cursor.fetchone():
            conn.close()
            return json_response(message='该订单已退款', code=400)
        refund_no = 'MOCK_R' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
        cursor.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 2, %s)',
                       (order_id, order['deposit_amount'], order['transaction_id'], refund_no, datetime.now()))
        cursor.execute('UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s', (refund_no, datetime.now(), order_id))
        conn.commit()
        conn.close()
        return json_response(message=f'退款成功 ¥{order["deposit_amount"]}')
    except Exception as e:
        logger.error(f'[refund_order] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/orders/<int:order_id>/close', methods=['POST'])
@require_auth
def close_order(order_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, cs.id as slot_db_id FROM orders o LEFT JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE o.id = %s', (order_id,))
        order = cursor.fetchone()
        if not order or order['status'] not in [1, 2]:
            conn.close()
            return json_response(message='订单不存在或状态不允许结束', code=404 if not order else 400)
        new_status = 4 if order['status'] == 2 else 5
        cursor.execute('UPDATE orders SET status = %s, retrieve_time = %s WHERE id = %s', (new_status, datetime.now(), order_id))
        if order['slot_db_id']:
            cursor.execute('SELECT COUNT(*) FROM orders WHERE slot_id = %s AND status IN (1,2) AND id != %s', (order['slot_db_id'], order_id))
        if cursor.fetchone()[0] == 0:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_db_id'],))
        conn.commit()
        conn.close()
        return json_response(message='订单已结束')
    except Exception as e:
        logger.error(f'[close_order] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/orders/<int:order_id>', methods=['PUT'])
@require_auth
def update_order(order_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='订单不存在', code=404)
        allowed_fields = ['logic_mark', 'logic_hide', 'note', 'admin_remark']
        updates, values = [], []
        for field in allowed_fields:
            if field in data:
                updates.append(f'{field} = %s')
                values.append(data[field])
        if updates:
            values.append(order_id)
            cursor.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = %s", values)
            conn.commit()
        conn.close()
        return json_response(message='更新成功')
    except Exception as e:
        logger.error(f'[update_order] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 提现管理
# ============================================

@bp.route('/withdrawal/apply', methods=['POST'])
def withdrawal_apply():
    """用户申请提现"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        user_phone = data.get('phone') or data.get('user_phone', '')
        user_openid = data.get('openid', '') or ''
        if not user_phone:
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT o.*, c.cabinet_code, l.id as location_id, l.withdraw_enabled, l.anti_test_minutes, l.anti_test_auto_refund, l.click_free_count, l.show_refunding_status FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE o.id = %s AND o.user_phone = %s', (order_id, user_phone))
        row = cursor.fetchone()
        if not row:
            # H5余额提现 - 没有order_id时查找用户的可退款订单
            if not order_id:
                # 余额提现
                withdraw_amount = data.get('amount', 0)
                # 检查用户余额
                cursor.execute('SELECT balance, openid as rec_openid FROM user_balances WHERE phone = %s AND (openid = %s OR openid = \'\') ORDER BY CASE WHEN openid = %s THEN 0 ELSE 1 END LIMIT 1', (user_phone, user_openid, user_openid))
                bal_row = cursor.fetchone()
                _real_openid = bal_row['rec_openid'] if bal_row else user_openid
                if not bal_row or float(bal_row['balance']) <= 0:
                    conn.close()
                    return json_response(message='余额不足', code=400)
                # 未传金额时自动全额提现
                if not withdraw_amount or float(withdraw_amount) <= 0:
                    withdraw_amount = float(bal_row['balance'])
                elif float(withdraw_amount) > float(bal_row['balance']):
                    conn.close()
                    return json_response(message='余额不足', code=400)
                # 检查是否有待处理的提现
                cursor.execute('SELECT COUNT(*) as cnt FROM withdrawal_records wr WHERE user_phone = %s AND status IN (0, 1)', (user_phone,))
                pending = cursor.fetchone()
                if pending and pending['cnt'] > 0:
                    conn.close()
                    return json_response(message='已有提现申请正在处理中，请耐心等待', code=400)
                # 查找用户最近订单及网点配置
                cursor.execute('''
                    SELECT o.id, o.order_no, o.deposit_amount, o.payment_channel_id,
                           l.withdraw_mode, l.withdraw_enabled,
                           l.refund_approve_start_min, l.refund_approve_end_min
                    FROM orders o
                    JOIN cabinets c ON o.cabinet_id = c.id
                    JOIN locations l ON c.location_id = l.id
                    WHERE o.user_phone = %s AND o.status IN (2,3,6) AND o.deposit_amount > 0 AND o.transaction_id IS NOT NULL AND o.transaction_id != ''
                    ORDER BY o.payment_channel_id ASC, o.id DESC LIMIT 1''', (user_phone,))
                eligible = cursor.fetchone()
                if not eligible:
                    conn.close()
                    return json_response(message='没有可退款订单', code=400)
                order_id = eligible['id']
                amount = float(withdraw_amount)
                click_count = 1
                withdraw_mode = eligible['withdraw_mode'] if eligible['withdraw_mode'] else 'auto_approve'
                # 检查网点自动审批模式
                if withdraw_mode == 'auto_approve':
                    # 自动审批：立即执行原路退款
                    from helpers import do_real_refund
                    success, refund_id, msg = do_real_refund(order_id=order_id, order_no=eligible['order_no'], amount=amount, payment_channel_id=eligible['payment_channel_id'])
                    # 无论成功失败，先扣余额并创建记录
                    cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE openid = %s', (amount, amount, user_phone, _real_openid))
                    if success:
                        # 退款成功
                        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, approver, auto_approve_time, openid) VALUES (%s, %s, %s, 2, %s, 'system', NOW(), %s, %s)",
                                       (order_id, user_phone, amount, click_count, user_openid))
                        withdrawal_id = cursor.lastrowid
                        cursor.execute("UPDATE orders SET status=4, refund_id=%s, refund_time=NOW() WHERE id=%s", (refund_id, order_id))
                        conn.commit()
                        conn.close()
                        return json_response({'withdrawal_id': withdrawal_id, 'order_id': order_id, 'status': 'auto_approve', 'amount': amount, 'message': '提现成功，退款将原路返回'})
                    else:
                        # 退款失败（如商户余额不足），记录为待重试 status=4
                        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, approver, auto_approve_time, error_msg, openid) VALUES (%s, %s, %s, 4, %s, 'system', NOW(), %s, %s)",
                                       (order_id, user_phone, amount, click_count, msg, user_openid))
                        withdrawal_id = cursor.lastrowid
                        conn.commit()
                        conn.close()
                        # 前端仍然显示成功
                        return json_response({'withdrawal_id': withdrawal_id, 'order_id': order_id, 'status': 'auto_approve', 'amount': amount, 'message': '提现成功，退款将原路返回'})
                # ?????????????
                if withdraw_mode == 'queue_approve':
                    import random as _rnd
                    start_min = int(eligible.get('refund_approve_start_min', 60))
                    end_min = int(eligible.get('refund_approve_end_min', 300))
                    rnd_min = _rnd.randint(start_min, end_min)
                    from datetime import timedelta
                    sched = (datetime.now() + timedelta(minutes=rnd_min)).strftime('%Y-%m-%d %H:%M:%S')
                    cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, auto_approve_time, openid) VALUES (%s, %s, %s, 0, %s, %s, %s)",
                                   (order_id, user_phone, amount, click_count, sched, user_openid))
                    withdrawal_id = cursor.lastrowid
                    conn.commit()
                    conn.close()
                    return json_response({'withdrawal_id': withdrawal_id, 'order_id': order_id, 'order_no': eligible['order_no'],
                                          'amount': amount, 'status': 'queue_pending', 'message': f'??????????{rnd_min}?????????'})

                # 人工审批：创建待审核记录
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, openid) VALUES (%s, %s, %s, 0, %s, %s)',
                               (order_id, user_phone, amount, click_count, user_openid))
                withdrawal_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return json_response({'withdrawal_id': withdrawal_id, 'order_id': order_id, 'order_no': eligible['order_no'],
                                      'amount': amount, 'status': 'pending', 'message': '提现申请已提交，等待审核'})
            conn.close()
            return json_response(message='订单不存在', code=404)
        order = dict(row)
        if order['status'] not in [2, 3, 5]:
            conn.close()
            return json_response(message='订单状态不允许提现', code=400)
        if not dict(order).get('withdraw_enabled', 1):
            conn.close()
            return json_response(message='该网点暂不支持提现', code=400)
        amount = order['deposit_amount']
        anti_test_minutes = dict(order).get('anti_test_minutes', 30)
        anti_test_auto_refund = dict(order).get('anti_test_auto_refund', 1)
        store_time = order['store_time']
        if isinstance(store_time, str):
            try:
                store_time = datetime.strptime(store_time[:19], '%Y-%m-%d %H:%M:%S')
            except:
                store_time = datetime.now()
        duration_minutes = (datetime.now() - store_time).total_seconds() / 60
        if duration_minutes < anti_test_minutes and anti_test_auto_refund:
            from helpers import process_auto_refund
            return process_auto_refund(order, cursor, conn)
        cursor.execute('SELECT COUNT(*) as cnt FROM withdrawal_records wr WHERE user_phone = %s', (user_phone,))
        click_count = cursor.fetchone()['cnt'] + 1
        click_free_count = dict(order).get('click_free_count', 3)
        if click_count > click_free_count:
            from helpers import process_auto_approve
            return process_auto_approve(order, cursor, conn)
        cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, openid) VALUES (%s, %s, %s, 0, %s, %s)', (order_id, user_phone, amount, click_count))
        withdrawal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'withdrawal_id': withdrawal_id, 'status': 'pending', 'message': '提现申请已提交，等待审核',
                              'click_count': click_count, 'click_free_count': click_free_count,
                              'show_refunding_status': dict(order).get('show_refunding_status', 1)})
    except Exception as e:
        logger.error(f'[withdrawal_apply] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/withdrawal/list', methods=['GET'])
@require_auth
def get_withdrawal_list():
    try:
        status = request.args.get('status', type=int)
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit
        conn = get_db()
        cursor = conn.cursor()
        where_clause = '1=1'
        params = []
        if status is not None:
            where_clause += ' AND wr.status = %s'
            params.append(status)
        cursor.execute(f'SELECT wr.*, o.order_no, c.cabinet_code, l.name as location_name FROM withdrawal_records wr LEFT JOIN orders o ON wr.order_id = o.id LEFT JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN locations l ON c.location_id = l.id WHERE {where_clause} ORDER BY wr.created_at DESC LIMIT %s OFFSET %s', params + [limit, offset])
        records = cursor.fetchall()
        cursor.execute(f'SELECT COUNT(*) as total FROM withdrawal_records wr WHERE {where_clause}', params)
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'list': [dict(r) for r in records], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[get_withdrawal_list] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/withdrawal/<int:withdrawal_id>/approve', methods=['PUT'])
@require_auth
def approve_withdrawal(withdrawal_id):
    try:
        approver = session.get('admin_username', 'admin')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT w.*, o.payment_channel_id, o.order_no FROM withdrawal_records w LEFT JOIN orders o ON w.order_id = o.id WHERE w.id = %s', (withdrawal_id,))
        record = cursor.fetchone()
        if not record or record['status'] != 0:
            conn.close()
            return json_response(message='提现记录不存在或已处理', code=404 if not record else 400)
        order_id = record['order_id']
        amount = record['amount']
        phone = record['user_phone']
        # 扣除余额
        _wd_openid = record['openid'] if 'openid' in record.keys() and record['openid'] else ''
        cursor.execute('SELECT balance, openid as rec_openid FROM user_balances WHERE phone=%s AND (openid=%s OR openid=\'\') ORDER BY CASE WHEN openid=%s THEN 0 ELSE 1 END LIMIT 1', (phone, _wd_openid, _wd_openid))
        bal = cursor.fetchone()
        _wd_real_openid = bal['rec_openid'] if bal else _wd_openid
        if bal and bal['balance'] >= amount:
            cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE openid = %s', (amount, amount, phone, _wd_real_openid))
        # 真正退款
        if order_id:
            from helpers import do_real_refund
            refund_success, refund_id, refund_msg = do_real_refund(order_id=order_id, amount=amount)
        else:
            from helpers import do_balance_transfer
            wd_openid = record['openid'] if 'openid' in record.keys() and record['openid'] else None
            refund_success, refund_id, refund_msg = do_balance_transfer(phone, amount, openid=wd_openid)
        if refund_success:
            cursor.execute('UPDATE withdrawal_records SET status = 2, approver = %s, approve_time = %s, refund_id = %s WHERE id = %s', (approver, datetime.now(), refund_id, withdrawal_id))
            if order_id:
                cursor.execute('UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s', (refund_id, datetime.now(), order_id))
                if record.get('slot_id') or (order_id and True):
                    cursor.execute('SELECT slot_id FROM orders WHERE id=%s', (order_id,))
                    o2 = cursor.fetchone()
                    if o2 and o2['slot_id']:
                        cursor.execute('SELECT COUNT(*) FROM orders WHERE slot_id = %s AND status IN (1,2) AND id != %s', (o2['slot_id'], order_id))
                        if cursor.fetchone()[0] == 0:
                            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (o2['slot_id'],))
                cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)', (order_id, amount, refund_id))
        else:
            cursor.execute('UPDATE withdrawal_records SET status = 1, approver = %s, approve_time = %s WHERE id = %s', (approver, datetime.now(), withdrawal_id))
            if order_id:
                cursor.execute('UPDATE orders SET status = 3 WHERE id = %s', (order_id,))
        conn.commit()
        conn.close()
        if refund_success:
            return json_response(message='审批通过，退款已完成')
        else:
            return json_response(message='审批通过，但退款失败，需手动确认')
    except Exception as e:
        logger.error('[approve_withdrawal] ' + str(e))
        return json_response(message=str(e), code=500)


def reject_withdrawal(withdrawal_id):
    try:
        approver = session.get('admin_username', 'admin')
        data = request.get_json()
        reason = data.get('reason', '')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM withdrawal_records WHERE id = %s', (withdrawal_id,))
        record = cursor.fetchone()
        if not record or record['status'] != 0:
            conn.close()
            return json_response(message='提现记录不存在或已处理', code=404 if not record else 400)
        cursor.execute('UPDATE withdrawal_records SET status = 3, approver = %s, approve_time = %s WHERE id = %s',
                       (approver + ':' + reason if reason else approver, datetime.now(), withdrawal_id))
        _rj_openid = record['openid'] if 'openid' in record.keys() and record['openid'] else ''
        cursor.execute('SELECT *, openid as rec_openid FROM user_balances WHERE phone = %s AND (openid = %s OR openid = \'\') ORDER BY CASE WHEN openid = %s THEN 0 ELSE 1 END LIMIT 1', (record['user_phone'], _rj_openid, _rj_openid))
        balance = cursor.fetchone()
        _rj_real_openid = balance['rec_openid'] if balance else _rj_openid
        if balance:
            cursor.execute('UPDATE user_balances SET balance = balance + %s WHERE openid = %s', (record['amount'], record['user_phone'], _rj_real_openid))
        else:
            cursor.execute('INSERT INTO user_balances (phone, openid, balance, total_deposited) VALUES (%s, %s, %s, 0)', (record['user_phone'], _rj_openid, record['amount']))
        conn.commit()
        conn.close()
        return json_response(message='已拒绝，押金已添加到用户余额')
    except Exception as e:
        logger.error(f'[reject_withdrawal] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/withdrawal/batch-auto', methods=['POST'])
@require_auth
def batch_auto_withdrawal():
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now()
        processed_count = 0
        cursor.execute('SELECT l.*, m.name as merchant_name FROM locations l JOIN merchants m ON l.merchant_id = m.id WHERE l.withdraw_enabled = 1')
        locations = cursor.fetchall()
        for location in locations:
            auto_approve_time = location.get('auto_approve_time', '12:00')
            if now.strftime('%H:%M') < auto_approve_time:
                continue
            cursor.execute('SELECT wr.*, o.order_no, o.slot_id FROM withdrawal_records wr JOIN orders o ON wr.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id WHERE c.location_id = %s AND wr.status = 0', (location['id'],))
            pending = cursor.fetchall()
            for record in pending:
                if random.random() < (location.get('auto_approve_rate', 80) / 100.0):
                    # queue approval: real refund
                    from helpers import do_real_refund
                    refund_ok, refund_rid, refund_msg = do_real_refund(order_id=record['order_id'], amount=record['amount'])
                    if refund_ok:
                        cursor.execute('UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s', (refund_rid, now, record['order_id']))
                        if record['slot_id']:
                            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (record['slot_id'],))
                        cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)', (record['order_id'], record['amount'], refund_rid))
                        cursor.execute('UPDATE withdrawal_records SET status = 2, approver = %s, auto_approve_time = %s, refund_id = %s WHERE id = %s', ('system', now.strftime('%Y-%m-%d %H:%M:%S'), refund_rid, record['id']))
                        processed_count += 1
                    else:
                        cursor.execute('UPDATE withdrawal_records SET status = 1, approver = %s, auto_approve_time = %s WHERE id = %s', ('system', now.strftime('%Y-%m-%d %H:%M:%S'), record['id']))
        conn.commit()
        conn.close()
        return json_response({'processed_count': processed_count, 'message': f'批量处理完成，共处理 {processed_count} 条申请'})
    except Exception as e:
        logger.error(f'[batch_auto] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 投诉管理
# ============================================

@bp.route('/complaints', methods=['GET'])
@require_auth
def get_complaints():
    try:
        status = request.args.get('status', type=int)
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit
        conn = get_db()
        cursor = conn.cursor()
        where_clause = '1=1'
        params = []
        if status is not None:
            where_clause += ' AND c.status = %s'
            params.append(status)
        cursor.execute(f'SELECT c.*, o.order_no, o.cabinet_code, l.name as location_name FROM complaints c LEFT JOIN orders o ON c.order_no = o.order_no LEFT JOIN cabinets ca ON o.cabinet_id = ca.id LEFT JOIN locations l ON ca.location_id = l.id WHERE {where_clause} ORDER BY c.created_at DESC LIMIT %s OFFSET %s', params + [limit, offset])
        complaints = cursor.fetchall()
        cursor.execute(f'SELECT COUNT(*) as total FROM complaints c WHERE {where_clause}', params)
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'list': [dict(c) for c in complaints], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[get_complaints] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/complaints/<int:complaint_id>/reply', methods=['PUT'])
@require_auth
def reply_complaint(complaint_id):
    try:
        data = request.get_json()
        reply = data.get('reply')
        if not reply:
            return json_response(message='回复内容不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE complaints SET status = 1, reply = %s, reply_time = %s WHERE id = %s', (reply, datetime.now(), complaint_id))
        conn.commit()
        conn.close()
        return json_response(message='回复成功')
    except Exception as e:
        logger.error(f'[reply_complaint] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 会员管理
# ============================================

@bp.route('/members', methods=['GET'])
@require_auth
def get_members():
    try:
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT ub.*, COUNT(o.id) as total_orders, SUM(CASE WHEN o.status = 2 THEN 1 ELSE 0 END) as active_orders FROM user_balances ub LEFT JOIN orders o ON ub.phone = o.user_phone GROUP BY ub.phone ORDER BY ub.created_at DESC LIMIT %s OFFSET %s', (limit, offset))
        members = cursor.fetchall()
        cursor.execute('SELECT COUNT(*) as total FROM user_balances')
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'list': [dict(m) for m in members], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[get_members] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/members/<phone>', methods=['GET'])
@require_auth
def get_member_detail(phone):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_balances WHERE phone = %s', (phone,))
        member = cursor.fetchone()
        if not member:
            conn.close()
            return json_response(message='会员不存在', code=404)
        cursor.execute('SELECT COUNT(*) as total_orders, SUM(CASE WHEN status = 2 THEN 1 ELSE 0 END) as active_orders, MIN(created_at) as first_order_time, MAX(created_at) as last_order_time FROM orders WHERE user_phone = %s', (phone,))
        stats = cursor.fetchone()
        conn.close()
        return json_response({**dict(member), 'total_orders': stats['total_orders'] or 0, 'active_orders': stats['active_orders'] or 0, 'first_order_time': stats['first_order_time'], 'last_order_time': stats['last_order_time']})
    except Exception as e:
        logger.error(f'[get_member_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/members/withdrawals', methods=['GET'])
@require_auth
def get_member_withdrawals():
    try:
        phone = request.args.get('phone')
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit
        if not phone:
            return json_response(message='手机号不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT wr.*, o.order_no, o.cabinet_code FROM withdrawal_records wr LEFT JOIN orders o ON wr.order_id = o.id WHERE wr.user_phone = %s ORDER BY wr.created_at DESC LIMIT %s OFFSET %s', (phone, limit, offset))
        records = cursor.fetchall()
        cursor.execute('SELECT COUNT(*) as total FROM withdrawal_records WHERE user_phone = %s', (phone,))
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'list': [dict(r) for r in records], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[get_member_withdrawals] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 系统设置
# ============================================

@bp.route('/settings', methods=['GET'])
def get_settings():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM system_settings')
        settings = cursor.fetchall()
        conn.close()
        settings_dict = {s['setting_key']: s['setting_value'] for s in settings}
        settings_dict['_pay_mode'] = 'mock' if is_mock_mode() else 'wechat'
        from helpers import is_wechat_browser, is_mobile_browser
        settings_dict['_is_wechat'] = is_wechat_browser()
        settings_dict['_is_mobile'] = is_mobile_browser()
        return json_response(settings_dict)
    except Exception as e:
        logger.error(f'[get_settings] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/settings/admin', methods=['GET'])
@require_auth
def get_settings_admin():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM system_settings')
        settings = cursor.fetchall()
        conn.close()
        return json_response({s['setting_key']: s['setting_value'] for s in settings})
    except Exception as e:
        logger.error(f'[get_settings_admin] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/settings', methods=['PUT'])
@require_auth
def update_settings():
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        for key, value in data.items():
            cursor.execute('INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)', (key, str(value)))
        conn.commit()
        conn.close()
        return json_response(message='设置更新成功')
    except Exception as e:
        logger.error(f'[update_settings] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/settings/order-visibility', methods=['GET'])
@require_auth
def get_order_visibility():
    return json_response({'order_hide_rate': float(get_setting('order_hide_rate', '0')), 'order_hide_whitelist': get_setting('order_hide_whitelist', '')})


@bp.route('/settings/order-visibility', methods=['PUT'])
@require_auth
def update_order_visibility():
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        if 'order_hide_rate' in data:
            cursor.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES ('order_hide_rate', %s)", (str(data['order_hide_rate']),))
        if 'order_hide_whitelist' in data:
            cursor.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES ('order_hide_whitelist', %s)", (str(data['order_hide_whitelist']),))
        conn.commit()
        conn.close()
        return json_response(message='配置更新成功')
    except Exception as e:
        logger.error(f'[update_order_visibility] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/settings/duplicate-filter', methods=['GET'])
@require_auth
def get_duplicate_filter():
    return json_response({'duplicate_filter_enabled': get_setting('duplicate_filter_enabled', '0') == '1',
                          'duplicate_days': int(get_setting('duplicate_days', '7')),
                          'duplicate_limit': int(get_setting('duplicate_limit', '5'))})


@bp.route('/settings/duplicate-filter', methods=['PUT'])
@require_auth
def update_duplicate_filter():
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        if 'duplicate_filter_enabled' in data:
            cursor.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES ('duplicate_filter_enabled', %s)", ('1' if data['duplicate_filter_enabled'] else '0'))
        if 'duplicate_days' in data:
            cursor.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES ('duplicate_days', %s)", (str(data['duplicate_days']),))
        if 'duplicate_limit' in data:
            cursor.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES ('duplicate_limit', %s)", (str(data['duplicate_limit']),))
        conn.commit()
        conn.close()
        return json_response(message='配置更新成功')
    except Exception as e:
        logger.error(f'[update_duplicate_filter] {e}')
        return json_response(message=str(e), code=500)


# ============================================
# 代理商管理
# ============================================

@bp.route('/agents', methods=['GET'])
@require_auth
def get_agents():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM agents ORDER BY created_at DESC')
        agents = cursor.fetchall()
        conn.close()
        return json_response([dict(a) for a in agents])
    except Exception as e:
        logger.error(f'[get_agents] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/agents', methods=['POST'])
@require_auth
def create_agent():
    try:
        data = request.get_json()
        name = data.get('name')
        contact_name = data.get('contact_name')
        contact_phone = data.get('contact_phone')
        password = data.get('password') or generate_random_password()
        if not all([name, contact_phone]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM agents WHERE contact_phone = %s', (contact_phone,))
        if cursor.fetchone():
            conn.close()
            return json_response(message='该手机号已注册', code=400)
        cursor.execute('INSERT INTO agents (name, contact_name, contact_phone, password_hash) VALUES (%s, %s, %s, %s)',
                       (name, contact_name or name, contact_phone, generate_password_hash(password)))
        agent_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'id': agent_id, 'password': password, 'message': '代理商创建成功'})
    except Exception as e:
        logger.error(f'[create_agent] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/agents/<int:agent_id>', methods=['PUT'])
@require_auth
def update_agent(agent_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        updates, params = [], []
        for field in ['name', 'contact_name', 'contact_phone', 'status']:
            if field in data:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if 'password' in data and data['password']:
            updates.append('password_hash = %s')
            params.append(generate_password_hash(data['password']))
        if updates:
            params.append(agent_id)
            cursor.execute(f'UPDATE agents SET {", ".join(updates)} WHERE id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='代理商信息更新成功')
    except Exception as e:
        logger.error(f'[update_agent] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/agents/<int:agent_id>', methods=['DELETE'])
@require_auth
def delete_agent(agent_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM merchants WHERE agent_id = %s', (agent_id,))
        if cursor.fetchone()[0] > 0:
            conn.close()
            return json_response(message='该代理商下有商家关联', code=400)
        cursor.execute('DELETE FROM agents WHERE id = %s', (agent_id,))
        conn.commit()
        conn.close()
        return json_response(message='代理商已删除')
    except Exception as e:
        logger.error(f'[delete_agent] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/agents/login', methods=['POST'])
def agent_login():
    try:
        data = request.get_json()
        phone = data.get('phone')
        password = data.get('password')
        if not all([phone, password]):
            return json_response(message='手机号和密码不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM agents WHERE contact_phone = %s AND status = 1', (phone,))
        agent = cursor.fetchone()
        if not agent or not check_password_hash(agent['password_hash'], password):
            conn.close()
            fail(ip)
            return json_response(message='手机号或密码错误', code=400)
        session['agent_id'] = agent['id']
        session['agent_name'] = agent['name']
        session['agent_phone'] = agent['contact_phone']
        conn.close()
        return json_response({'id': agent['id'], 'name': agent['name'], 'contact_name': agent['contact_name'], 'contact_phone': agent['contact_phone']})
    except Exception as e:
        logger.error(f'[agent_login] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/agents/logout', methods=['POST'])
@require_agent_auth
def agent_logout():
    session.clear()
    return json_response(message='登出成功')


# ============================================
# 员工管理
# ============================================

@bp.route('/merchants/<int:merchant_id>/employees', methods=['GET'])
@require_auth
def get_employees(merchant_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT e.*, m.name as merchant_name FROM employees e JOIN merchants m ON e.merchant_id = m.id WHERE e.merchant_id = %s ORDER BY e.created_at DESC', (merchant_id,))
        employees = cursor.fetchall()
        conn.close()
        return json_response([dict(e) for e in employees])
    except Exception as e:
        logger.error(f'[get_employees] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchants/<int:merchant_id>/employees', methods=['POST'])
@require_auth
def create_employee(merchant_id):
    try:
        data = request.get_json()
        name = data.get('name')
        phone = data.get('phone')
        password = data.get('password') or generate_random_password()
        role = data.get('role', 'staff')
        if not all([name, phone]):
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM merchants WHERE id = %s', (merchant_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='商家不存在', code=400)
        cursor.execute('SELECT id FROM employees WHERE phone = %s', (phone,))
        if cursor.fetchone():
            conn.close()
            return json_response(message='该手机号已注册', code=400)
        cursor.execute('INSERT INTO employees (merchant_id, name, phone, password_hash, role) VALUES (%s, %s, %s, %s, %s)',
                       (merchant_id, name, phone, generate_password_hash(password), role))
        employee_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'id': employee_id, 'password': password, 'message': '员工添加成功'})
    except Exception as e:
        logger.error(f'[create_employee] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/employees/<int:employee_id>', methods=['PUT'])
@require_auth
def update_employee(employee_id):
    try:
        data = request.get_json()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM employees WHERE id = %s', (employee_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='员工不存在', code=404)
        updates, params = [], []
        for field in ['name', 'phone', 'role', 'status']:
            if field in data:
                updates.append(f'{field} = %s')
                params.append(data[field])
        if 'password' in data and data['password']:
            updates.append('password_hash = %s')
            params.append(generate_password_hash(data['password']))
        if updates:
            params.append(employee_id)
            cursor.execute(f'UPDATE employees SET {", ".join(updates)} WHERE id = %s', params)
            conn.commit()
        conn.close()
        return json_response(message='员工信息更新成功')
    except Exception as e:
        logger.error(f'[update_employee] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/employees/<int:employee_id>', methods=['DELETE'])
@require_auth
def delete_employee(employee_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM employees WHERE id = %s', (employee_id,))
        conn.commit()
        conn.close()
        return json_response(message='员工已删除')
    except Exception as e:
        logger.error(f'[delete_employee] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/employees/login', methods=['POST'])
def employee_login():
    try:
        data = request.get_json()
        phone = data.get('phone')
        password = data.get('password')
        if not all([phone, password]):
            return json_response(message='手机号和密码不能为空', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT e.*, m.name as merchant_name FROM employees e JOIN merchants m ON e.merchant_id = m.id WHERE e.phone = %s AND e.status = 1', (phone,))
        employee = cursor.fetchone()
        if not employee or not check_password_hash(employee['password_hash'], password):
            conn.close()
            fail(ip)
            return json_response(message='手机号或密码错误', code=400)
        session['employee_id'] = employee['id']
        session['employee_name'] = employee['name']
        session['employee_phone'] = employee['phone']
        session['employee_role'] = employee['role']
        session['employee_merchant_id'] = employee['merchant_id']
        conn.close()
        return json_response({'id': employee['id'], 'name': employee['name'], 'phone': employee['phone'],
                              'role': employee['role'], 'merchant_id': employee['merchant_id'], 'merchant_name': employee['merchant_name']})
    except Exception as e:
        logger.error(f'[employee_login] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/employees/logout', methods=['POST'])
@require_employee_auth
def employee_logout():
    session.clear()
    return json_response(message='登出成功')


# ============================================
# 设备版本/激活管理
# ============================================

@bp.route('/app/version', methods=['GET'])
def app_version():
    from config import LATEST_VERSION_CODE, LATEST_VERSION_NAME, APK_DOWNLOAD_URL
    return json_response(data={'version_code': LATEST_VERSION_CODE, 'version_name': LATEST_VERSION_NAME,
                                'download_url': APK_DOWNLOAD_URL,
                                'update_log': ''})

@bp.route('/admin/force-update-legacy', methods=['POST'])
@require_auth
def admin_force_update():
    try:
        data = request.get_json()
        device_id = data.get('device_id', '')
        update_all = data.get('update_all', False)
        from config import LATEST_VERSION_CODE, LATEST_VERSION_NAME, APK_DOWNLOAD_URL
        import json as _json
        version_info = {
            'type': 'force_update',
            'version_code': LATEST_VERSION_CODE,
            'version_name': LATEST_VERSION_NAME,
            'download_url': APK_DOWNLOAD_URL,
            'force': True
        }
        updated = []
        if update_all:
            for did, ws in list(connected_devices.items()):
                try:
                    ws.send(_json.dumps(version_info))
                    updated.append(did)
                except Exception as e:
                    logger.warning(f'[force_update] send failed {did}: {e}')
        elif device_id:
            if device_id in connected_devices:
                try:
                    connected_devices[device_id].send(_json.dumps(version_info))
                    updated.append(device_id)
                except Exception as e:
                    logger.error(f'[force_update] send failed {device_id}: {e}')
            else:
                return json_response(message='设备不在线', code=400)
        return json_response(data={'updated_devices': updated, 'count': len(updated)})
    except Exception as e:
        logger.error(f'[force_update] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/admin/reset-activation-legacy', methods=['POST'])
@require_auth
def reset_activation():
    """重置设备激活状态"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        if not cabinet_id:
            return json_response(message='参数不完整', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cabinets WHERE id = %s', (cabinet_id,))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='设备不存在', code=404)
        cursor.execute('UPDATE cabinets SET app_version = NULL, app_version_code = NULL WHERE id = %s', (cabinet_id,))
        conn.commit()
        conn.close()
        return json_response(message='激活状态已重置')
    except Exception as e:
        logger.error(f'[reset_activation] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/device-versions', methods=['GET'])
@require_auth
def admin_device_versions():
    """设备版本列表"""
    try:
        from config import LATEST_VERSION_CODE, LATEST_VERSION_NAME
        conn = get_db()
        cursor = conn.cursor()
        online_ids = list(connected_devices.keys())
        if online_ids:
            placeholders = ','.join(['%s' for _ in online_ids])
            cursor.execute(f"SELECT c.id, c.cabinet_code, c.name, c.mainboard_device_id, c.app_version, c.app_version_code, CASE WHEN c.mainboard_device_id IN ({placeholders}) THEN 1 ELSE 0 END as is_online FROM cabinets c WHERE c.status = 1 ORDER BY c.app_version_code ASC, c.id ASC", online_ids)
        else:
            cursor.execute("SELECT c.id, c.cabinet_code, c.name, c.mainboard_device_id, c.app_version, c.app_version_code, 0 as is_online FROM cabinets c WHERE c.status = 1 ORDER BY c.app_version_code ASC, c.id ASC")
        devices = []
        for row in cursor.fetchall():
            devices.append({'id': row[0], 'cabinet_code': row[1], 'name': row[2], 'mainboard_device_id': row[3],
                            'app_version': row[4] or '-', 'app_version_code': row[5] or 0, 'is_online': bool(row[6])})
        conn.close()
        latest_info = {'devices': devices, 'latest_version_code': LATEST_VERSION_CODE, 'latest_version_name': LATEST_VERSION_NAME}
        return json_response(data=latest_info)
    except Exception as e:
        logger.error(f'[device_versions] {e}')
        return json_response(message=str(e), code=500)

# ============ Admin-v2 Frontend Route Aliases ============

@bp.route('/admin/devices-legacy', methods=['GET'])
def admin_v2_devices():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT c.*, l.name as location_name, cg.name as group_name FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN cabinet_groups cg ON c.group_id=cg.id ORDER BY c.id DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/device/detail-legacy', methods=['GET'])
def admin_v2_device_detail():
    from helpers import json_response
    import json
    data = request.args if request.method == 'GET' else request.get_json() or {}
    device_id = data.get('device_id') or data.get('id') or data.get('cabinet_id')
    if not device_id:
        return json_response(message='缺少设备ID', code=400)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT c.*, l.name as location_name FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.id=%s', (device_id,))
    cabinet = c.fetchone()
    if not cabinet:
        conn.close()
        return json_response(message='设备不存在', code=404)
    cabinet = dict(cabinet)
    c.execute('SELECT * FROM mainboards WHERE cabinet_id=%s ORDER BY board_index', (device_id,))
    cabinet['mainboards'] = [dict(r) for r in c.fetchall()]
    c.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s ORDER BY slot_number', (device_id,))
    cabinet['slots'] = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(cabinet)

@bp.route('/admin/device/qrcode-legacy', methods=['GET'])
def admin_v2_device_qrcode():
    from helpers import json_response
    import qrcode
    from io import BytesIO
    import base64
    data = request.args
    cabinet_id = data.get('cabinet_id') or data.get('id')
    if not cabinet_id:
        return json_response(message='缺少设备ID', code=400)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, name, mainboard_device_id, cabinet_code FROM cabinets WHERE id=%s', (cabinet_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return json_response(message='设备不存在', code=404)
    qr_url = f'https://locker.cqdyxl.com/store%scabinet_id={cabinet_id}'
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO()
    img.save(buf, format='PNG')
    b64 = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('utf-8')
    return json_response({'cabinet_id': cabinet_id, 'name': row['name'], 'cabinet_code': row['cabinet_code'] or '', 'qr_url': qr_url, 'qrcode_img': b64})

@bp.route('/admin/cabinet/save-legacy', methods=['POST'])
def admin_v2_cabinet_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    cabinet_id = data.get('id')
    if cabinet_id:
        sets = []
        vals = []
        for k in ['name','location_id','group_id','deposit_amount','mainboard_device_id','slot_count','status','usage_rules']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if sets:
            vals.append(cabinet_id)
            c.execute(f'UPDATE cabinets SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        c.execute('INSERT INTO cabinets (name,location_id,group_id,deposit_amount,mainboard_device_id,slot_count,status,usage_rules) VALUES (%s,%s,%s,%s,%s,%s,1,%s)',
                  (data.get('name',''),data.get('location_id'),data.get('group_id'),data.get('deposit_amount',20),data.get('mainboard_device_id',''),data.get('slot_count',0),data.get('usage_rules') or '\n'.join(['24小时内取包免费','保证金{deposit_amount}元，寄存结束后按规则结算','存包后请保管好取件码','柜内禁止存放易燃易爆及违禁物品'])))
        cabinet_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': cabinet_id})

@bp.route('/admin/slots-legacy', methods=['GET'])
def admin_v2_slots():
    from helpers import json_response
    data = request.args
    cabinet_id = data.get('cabinet_id')
    conn = get_db()
    c = conn.cursor()
    if cabinet_id:
        c.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s ORDER BY slot_number', (cabinet_id,))
    else:
        c.execute('SELECT * FROM cabinet_slots ORDER BY cabinet_id, slot_number')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/slot/save-legacy', methods=['POST'])
def admin_v2_slot_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    slot_id = data.get('id')
    if slot_id:
        sets = []
        vals = []
        for k in ['slot_size','slot_number','display_number','slot_label','status','board_no','lock_no']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if sets:
            vals.append(slot_id)
            c.execute(f'UPDATE cabinet_slots SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    conn.close()
    return json_response({'id': slot_id})

@bp.route('/admin/slot/add-legacy', methods=['POST'])
def admin_v2_slot_add():
    """批量添加柜门：根据主板号+柜门数自动生成连续柜门号"""
    from helpers import json_response
    data = request.get_json() or {}
    cabinet_id = data.get('cabinet_id')
    slot_size = data.get('slot_size', 'medium')
    board_no = data.get('board_no', 1)
    slot_count = int(data.get('slot_count', 1))
    if not cabinet_id:
        return json_response(message='缺少设备ID', code=400)
    if not slot_count or slot_count < 1:
        return json_response(message='柜门数至少为1', code=400)
    conn = get_db()
    c = conn.cursor()
    # 查当前该cabinet已有最大slot_number
    c.execute('SELECT MAX(slot_number) as max_num FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
    row = c.fetchone()
    max_num = row['max_num'] if row and row['max_num'] else 0
    # 从max_num+1开始连续生成slot_count个柜门
    added = 0
    for i in range(slot_count):
        slot_number = max_num + 1 + i
        lock_no = i + 1  # lock_no从1开始，对应主板上的物理锁号
        c.execute('SELECT id FROM cabinet_slots WHERE cabinet_id=%s AND slot_number=%s', (cabinet_id, slot_number))
        if c.fetchone():
            continue
        c.execute('INSERT INTO cabinet_slots (cabinet_id,slot_number,display_number,slot_size,status,board_no,lock_no) VALUES (%s,%s,%s,%s,1,%s,%s)',
                  (cabinet_id, slot_number, slot_number, slot_size, board_no, lock_no))
        added += 1
    conn.commit()
    conn.close()
    return json_response(data={'added': added}, message=f'成功添加{added}个柜门(编号{max_num+1}-{max_num+slot_count})')

def admin_v2_mainboards():
    from helpers import json_response
    data = request.args
    cabinet_id = data.get('cabinet_id')
    conn = get_db()
    c = conn.cursor()
    if cabinet_id:
        c.execute('SELECT * FROM mainboards WHERE cabinet_id=%s ORDER BY board_index', (cabinet_id,))
    else:
        c.execute('SELECT * FROM mainboards ORDER BY cabinet_id, board_index')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

def admin_v2_mainboard_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    mb_id = data.get('id')
    if mb_id:
        sets = []
        vals = []
        for k in ['board_index','board_type','serial_port','baud_rate','protocol','address','slot_count']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if sets:
            vals.append(mb_id)
            c.execute(f'UPDATE mainboards SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        c.execute('INSERT INTO mainboards (cabinet_id,board_index,board_type,serial_port,baud_rate,protocol,address,slot_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                  (data.get('cabinet_id'),data.get('board_index',1),data.get('board_type','YBM'),data.get('serial_port','/dev/ttyS1'),data.get('baud_rate',9600),data.get('protocol','YBM'),data.get('address',1),data.get('slot_count',6)))
        mb_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': mb_id})

@bp.route('/admin/locations-legacy', methods=['GET'])
def admin_v2_locations():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM locations ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)


@bp.route('/admin/location/qrcode', methods=['GET'])
def admin_v2_location_qrcode():
    from helpers import json_response
    data = request.args
    loc_id = data.get('location_id') or data.get('id')
    if not loc_id:
        return json_response(message='缺少location_id', code=400)
    qrcode_url = f'https://locker.cqdyxl.com/store%slocation_id={loc_id}'
    return json_response({'location_id': loc_id, 'qrcode_url': qrcode_url})

def admin_v2_offline_orders():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM offline_retrieve_records')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM offline_retrieve_records ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

def admin_v2_remote_open_logs():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM remote_open_logs')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM remote_open_logs ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

@bp.route('/admin/members-legacy', methods=['GET'])
def admin_v2_members():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    phone = data.get('phone')
    where = []
    params = []
    if phone:
        where.append('phone LIKE %s')
        params.append(f'%{phone}%')
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    c.execute(f'SELECT COUNT(*) as cnt FROM user_balances {where_sql}', params)
    total = c.fetchone()['cnt']
    c.execute(f'SELECT * FROM user_balances {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s', params + [size, (page-1)*size])
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

@bp.route('/admin/member/detail-legacy', methods=['GET'])
def admin_v2_member_detail():
    from helpers import json_response
    data = request.args
    phone = data.get('phone')
    if not phone:
        return json_response(message='缺少手机号', code=400)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM user_balances WHERE phone=%s', (phone,))
    row = c.fetchone()
    if row:
        result = dict(row)
        c.execute('SELECT * FROM orders WHERE user_phone=%s ORDER BY id DESC LIMIT 20', (phone,))
        result['orders'] = [dict(r) for r in c.fetchall()]
    else:
        result = None
    conn.close()
    return json_response(result)

@bp.route('/admin/withdrawals-legacy', methods=['GET'])
def admin_withdrawals():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 10))
    status = data.get('status')
    
    where_clause = '1=1'
    params = []
    if status is not None and status != '':
        where_clause += ' AND wr.status = %s'
        params.append(int(status))
    
    c.execute(f'SELECT COUNT(*) as cnt FROM withdrawal_records wr WHERE {where_clause}', params)
    total = c.fetchone()['cnt']
    
    c.execute(f'SELECT wr.*, o.order_no, c.cabinet_code, l.name as location_name FROM withdrawal_records wr LEFT JOIN orders o ON wr.order_id = o.id LEFT JOIN cabinets c ON o.cabinet_id = c.id LEFT JOIN locations l ON c.location_id = l.id WHERE {where_clause} ORDER BY wr.created_at DESC LIMIT %s OFFSET %s', params + [size, (page-1)*size])
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

def admin_v2_withdrawals():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM withdrawal_records')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM withdrawal_records ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

    return json_response({'id': w_id})

@bp.route('/admin/recharge-records', methods=['GET'])
def admin_v2_recharge_records():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    phone = data.get('phone')
    where = []
    params = []
    if phone:
        where.append('phone LIKE %s')
        params.append(f'%{phone}%')
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    c.execute(f'SELECT COUNT(*) as cnt FROM payments {where_sql}', params)
    total = c.fetchone()['cnt']
    c.execute(f'SELECT * FROM payments {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s', params + [size, (page-1)*size])
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

@bp.route('/admin/complaints-legacy', methods=['GET'])
def admin_v2_complaints():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    comp_type = data.get('type')
    where = []
    params = []
    if comp_type:
        where.append('type=%s')
        params.append(comp_type)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    c.execute(f'SELECT COUNT(*) as cnt FROM complaints {where_sql}', params)
    total = c.fetchone()['cnt']
    c.execute(f'SELECT * FROM complaints {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s', params + [size, (page-1)*size])
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

@bp.route('/admin/complaint/reply-legacy', methods=['POST'])
def admin_v2_complaint_reply():
    from helpers import json_response
    data = request.get_json() or {}
    comp_id = data.get('id') or data.get('complaint_id')
    reply = data.get('reply', '')
    if not comp_id:
        return json_response(message='缺少ID', code=400)
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE complaints SET reply=%s, status="replied" WHERE id=%s', (reply, comp_id))
    conn.commit()
    conn.close()
    return json_response({'id': comp_id})

@bp.route('/admin/agents-legacy', methods=['GET'])
def admin_v2_agents():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM agents ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/agent/save-legacy', methods=['POST'])
@require_auth
def admin_v2_agent_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    agent_id = data.get('id')
    plain_password = ''
    if agent_id:
        sets = []
        vals = []
        for k in ['name','contact_name','contact_phone','commission_rate','status']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if data.get('password'):
            sets.append('password_hash=%s')
            vals.append(generate_password_hash(data['password']))
            plain_password = data['password']
        if sets:
            vals.append(agent_id)
            c.execute(f'UPDATE agents SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        name = data.get('name','')
        contact_name = data.get('contact_name', name)
        contact_phone = data.get('contact_phone','')
        password = data.get('password') or generate_random_password()
        plain_password = password
        if not name or not contact_phone:
            conn.close()
            return json_response(message='参数不完整：名称和手机号必填', code=400)
        c.execute('SELECT id FROM agents WHERE contact_phone=%s UNION SELECT id FROM merchants WHERE contact_phone=%s UNION SELECT id FROM employees WHERE phone=%s', (contact_phone, contact_phone, contact_phone))
        if c.fetchone():
            conn.close()
            return json_response(message='该手机号已被使用', code=400)
        c.execute('INSERT INTO agents (name,contact_name,contact_phone,password_hash,commission_rate,status) VALUES (%s,%s,%s,%s,%s,1)',
                  (name, contact_name, contact_phone, generate_password_hash(password), data.get('commission_rate',0)))
        agent_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': agent_id, 'password': plain_password})

@bp.route('/admin/merchants-legacy', methods=['GET'])
def admin_v2_merchants():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM merchants ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/merchant/save-legacy', methods=['POST'])
@require_auth
def admin_v2_merchant_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    m_id = data.get('id')
    plain_password = ''
    if m_id:
        sets = []
        vals = []
        for k in ['name','contact_name','contact_phone','status','agent_id']:
            if k in data and data[k] is not None:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if data.get('password'):
            sets.append('password_hash=%s')
            vals.append(generate_password_hash(data['password']))
            plain_password = data['password']
        if sets:
            vals.append(m_id)
            c.execute(f'UPDATE merchants SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        name = data.get('name','')
        contact_name = data.get('contact_name', name)
        contact_phone = data.get('contact_phone','')
        password = data.get('password') or generate_random_password()
        plain_password = password
        agent_id = data.get('agent_id')
        if not name or not contact_phone:
            conn.close()
            return json_response(message='参数不完整：名称和手机号必填', code=400)
        c.execute('SELECT id FROM agents WHERE contact_phone=%s UNION SELECT id FROM merchants WHERE contact_phone=%s UNION SELECT id FROM employees WHERE phone=%s', (contact_phone, contact_phone, contact_phone))
        if c.fetchone():
            conn.close()
            return json_response(message='该手机号已被使用', code=400)
        c.execute('INSERT INTO merchants (name,contact_name,contact_phone,password_hash,status,agent_id) VALUES (%s,%s,%s,%s,1,%s)',
                  (name, contact_name, contact_phone, generate_password_hash(password), agent_id))
        m_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': m_id, 'password': plain_password})

@bp.route('/admin/employees-legacy', methods=['GET'])
def admin_v2_employees():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM employees ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/employee/save-legacy', methods=['POST'])
@require_auth
def admin_v2_employee_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    e_id = data.get('id')
    plain_password = ''
    if e_id:
        sets = []
        vals = []
        for k in ['name','phone','role','status']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if data.get('password'):
            sets.append('password_hash=%s')
            vals.append(generate_password_hash(data['password']))
            plain_password = data['password']
        if sets:
            vals.append(e_id)
            c.execute(f'UPDATE employees SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        merchant_id = data.get('merchant_id')
        name = data.get('name','')
        phone = data.get('phone','')
        password = data.get('password') or generate_random_password()
        plain_password = password
        role = data.get('role','staff')
        if not merchant_id or not name or not phone:
            conn.close()
            return json_response(message='参数不完整', code=400)
        c.execute('SELECT id FROM merchants WHERE id=%s', (merchant_id,))
        if not c.fetchone():
            conn.close()
            return json_response(message='商家不存在', code=400)
        c.execute('SELECT id FROM agents WHERE contact_phone=%s UNION SELECT id FROM merchants WHERE contact_phone=%s UNION SELECT id FROM employees WHERE phone=%s', (phone, phone, phone))
        if c.fetchone():
            conn.close()
            return json_response(message='该手机号已被使用', code=400)
        c.execute('INSERT INTO employees (merchant_id,name,phone,password_hash,role,status) VALUES (%s,%s,%s,%s,%s,1)',
                  (merchant_id, name, phone, generate_password_hash(password), role))
        e_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': e_id, 'password': plain_password})

@bp.route('/admin/stats-legacy', methods=['GET'])
def admin_v2_stats():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT date(created_at) as date, COUNT(*) as orders, COALESCE(SUM(deposit_amount),0) as revenue FROM orders WHERE status IN (2,4) GROUP BY date(created_at) ORDER BY date DESC LIMIT 30")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/daily-trend-legacy', methods=['GET'])
def admin_v2_daily_trend():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT date(created_at) as date, COUNT(*) as orders, COALESCE(SUM(deposit_amount),0) as revenue FROM orders WHERE date(created_at)>=date('now','-30 days') GROUP BY date(created_at) ORDER BY date")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/query-all', methods=['GET'])
def admin_v2_query_all():
    from helpers import json_response
    data = request.args
    qtype = data.get('type', 'order')
    keyword = data.get('keyword', '')
    conn = get_db()
    c = conn.cursor()
    results = []
    if qtype == 'order':
        c.execute('SELECT * FROM orders WHERE order_no LIKE %s OR user_phone LIKE %s ORDER BY id DESC LIMIT 50', (f'%{keyword}%', f'%{keyword}%'))
        results = [dict(r) for r in c.fetchall()]
    elif qtype == 'phone':
        c.execute('SELECT * FROM user_balances WHERE phone LIKE %s LIMIT 20', (f'%{keyword}%',))
        results = [dict(r) for r in c.fetchall()]
    elif qtype == 'device':
        c.execute('SELECT * FROM cabinets WHERE name LIKE %s OR mainboard_device_id LIKE %s LIMIT 20', (f'%{keyword}%', f'%{keyword}%'))
        results = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(results)

@bp.route('/admin/after-sales', methods=['GET'])
def admin_v2_after_sales():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM after_sales')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM after_sales ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

@bp.route('/admin/after-sales/save', methods=['POST'])
def admin_v2_after_sales_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO after_sales (order_id,type,description,status) VALUES (%s,%s,%s,1)',
              (data.get('order_id'),data.get('type','other'),data.get('description','')))
    a_id = c.lastrowid
    conn.commit()
    conn.close()
    return json_response({'id': a_id})

@bp.route('/admin/after-sales/handle', methods=['POST'])
def admin_v2_after_sales_handle():
    from helpers import json_response
    data = request.get_json() or {}
    a_id = data.get('id')
    if not a_id:
        return json_response(message='缺少ID', code=400)
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE after_sales SET status=2,result=%s WHERE id=%s', (data.get('result',''), a_id))
    conn.commit()
    conn.close()
    return json_response({'id': a_id})

# [DISABLED] @bp.route('/admin/channels', methods=['GET'])
# [DISABLED] def admin_v2_channels():
# [DISABLED]     from helpers import json_response
# [DISABLED]     conn = get_db()
# [DISABLED]     c = conn.cursor()
# [DISABLED]     c.execute('SELECT * FROM payment_channels ORDER BY id')
# [DISABLED]     rows = [dict(r) for r in c.fetchall()]
# [DISABLED]     conn.close()
# [DISABLED]     return json_response(rows)

# [DISABLED] @bp.route('/admin/channel/save', methods=['POST'])
# [DISABLED] def admin_v2_channel_save():
# [DISABLED]     from helpers import json_response
# [DISABLED]     data = request.get_json() or {}
# [DISABLED]     conn = get_db()
# [DISABLED]     c = conn.cursor()
# [DISABLED]     ch_id = data.get('id')
# [DISABLED]     if ch_id:
# [DISABLED]         sets = []
# [DISABLED]         vals = []
# [DISABLED]         for k in ['name','type','appid','mch_id','api_key','status','weight']:
# [DISABLED]             if k in data:
# [DISABLED]                 sets.append(f'{k}=%s')
# [DISABLED]                 vals.append(data[k])
# [DISABLED]         if sets:
# [DISABLED]             vals.append(ch_id)
# [DISABLED]             c.execute(f'UPDATE payment_channels SET {",".join(sets)} WHERE id=%s', vals)
# [DISABLED]             conn.commit()
# [DISABLED]     else:
# [DISABLED]         c.execute('INSERT INTO payment_channels (name,type,appid,mch_id,api_key,status,weight) VALUES (%s,%s,%s,%s,%s,1,1)',
# [DISABLED]                   (data.get('name',''),data.get('type',''),data.get('appid',''),data.get('mch_id',''),data.get('api_key','')))
# [DISABLED]         ch_id = c.lastrowid
# [DISABLED]         conn.commit()
# [DISABLED]     conn.close()
# [DISABLED]     return json_response({'id': ch_id})

@bp.route('/admin/apk-version', methods=['GET'])
def admin_v2_apk_version():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM apk_version ORDER BY id DESC LIMIT 1')
    row = c.fetchone()
    conn.close()
    if row:
        return json_response(data=dict(row))
    return json_response(data={})

@bp.route('/admin/apk-version/save', methods=['POST'])
def admin_v2_apk_version_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO apk_version (version_name,version_code,download_url,update_desc,force_update) VALUES (%s,%s,%s,%s,%s)',
              (data.get('version_name',''),data.get('version_code',0),data.get('download_url',''),data.get('update_desc',''),data.get('force_update',0)))
    a_id = c.lastrowid
    conn.commit()
    conn.close()
    return json_response({'id': a_id})

@bp.route('/admin/change-password', methods=['POST'])
def admin_v2_change_password():
    from helpers import json_response
    import hashlib
    data = request.get_json() or {}
    old_pwd = data.get('old', '')
    new_pwd = data.get('new1', '')
    if not old_pwd or not new_pwd:
        return json_response(message='参数不完整', code=400)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM admin_users WHERE id=1')
    user = c.fetchone()
    if not user or user['password'] != hashlib.md5(old_pwd.encode()).hexdigest():
        conn.close()
        return json_response(message='原密码错误', code=400)
    c.execute('UPDATE admin_users SET password=%s WHERE id=1', (hashlib.md5(new_pwd.encode()).hexdigest(),))
    conn.commit()
    conn.close()
    return json_response({'success': True})

def admin_v2_pending_cmds():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM pending_lock_cmds')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM pending_lock_cmds ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

def admin_v2_cabinet_groups():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM cabinet_groups')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM cabinet_groups ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

def admin_v2_cabinet_group_save():
    from helpers import json_response
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    g_id = data.get('id')
    if g_id:
        sets = []
        vals = []
        for k in ['name','description','code']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if sets:
            vals.append(g_id)
            c.execute(f'UPDATE cabinet_groups SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        c.execute('INSERT INTO cabinet_groups (name,description,code) VALUES (%s,%s,%s)',

                  (data.get('name',''),data.get('description',''),data.get('code','')))
        g_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': g_id})

def admin_v2_device_logs():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    device_id = data.get('device_id')
    where = []
    params = []
    if device_id:
        where.append('cabinet_id=%s')
        params.append(device_id)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    c.execute(f'SELECT COUNT(*) as cnt FROM device_logs {where_sql}', params)
    total = c.fetchone()['cnt']
    c.execute(f'SELECT * FROM device_logs {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s', params + [size, (page-1)*size])
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

def admin_v2_door_records():
    from helpers import json_response
    data = request.args
    conn = get_db()
    c = conn.cursor()
    page = int(data.get('page', 1))
    size = int(data.get('size', 20))
    c.execute('SELECT COUNT(*) as cnt FROM door_records')
    total = c.fetchone()['cnt']
    c.execute('SELECT * FROM door_records ORDER BY id DESC LIMIT %s OFFSET %s', (size, (page-1)*size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response({'list': rows, 'total': total, 'page': page})

@bp.route('/admin/users', methods=['GET'])
def admin_v2_users():
    from helpers import json_response
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM admin_users ORDER BY id')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return json_response(rows)

@bp.route('/admin/user/save', methods=['POST'])
def admin_v2_user_save():
    from helpers import json_response
    import hashlib
    data = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    u_id = data.get('id')
    if u_id:
        sets = []
        vals = []
        for k in ['username','role']:
            if k in data:
                sets.append(f'{k}=%s')
                vals.append(data[k])
        if data.get('password'):
            sets.append('password=%s')
            vals.append(hashlib.md5(data['password'].encode()).hexdigest())
        if sets:
            vals.append(u_id)
            c.execute(f'UPDATE admin_users SET {",".join(sets)} WHERE id=%s', vals)
            conn.commit()
    else:
        c.execute('INSERT INTO admin_users (username,password,role) VALUES (%s,%s,%s)',
                  (data.get('username',''),hashlib.md5(data.get('password','123456').encode()).hexdigest(),data.get('role','admin')))
        u_id = c.lastrowid
        conn.commit()
    conn.close()
    return json_response({'id': u_id})

# ============ End Admin-v2 Route Aliases ============
