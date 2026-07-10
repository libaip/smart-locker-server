"""
商户端API - Blueprint
包含：商户认证、信息、网点、柜体、柜格、订单、开锁日志等
"""
import logging
import secrets
import json
from datetime import datetime
from flask import Blueprint, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from database import get_db
from helpers import (json_response, require_merchant_auth, get_setting, logger, send_open_lock,
                     should_hide_order, filter_duplicate_users)

bp = Blueprint('merchant', __name__)

def _get_merchant_filter():
    """Get merchant_id and SQL filter based on session (merchant or agent)"""
    if session.get('is_agent'):
        agent_id = session['agent_id']
        # Agent sees data for all merchants under them
        return None, f'l.merchant_id IN (SELECT id FROM merchants WHERE agent_id = %s)', [agent_id]
    else:
        mid = session['merchant_id']
        return mid, 'l.merchant_id = %s', [mid]




@bp.route('/merchant/login', methods=['POST'])
def merchant_login():
    try:
        raw = request.get_data(); data = json.loads(raw) if raw else {}
        phone = data.get('phone')
        password = data.get('password')
        if not all([phone, password]):
            return json_response(message='手机号和密码不能为空', code=400)

        conn = get_db()
        cursor = conn.cursor()
        ALL_PERMISSIONS = '["dashboard","locations","devices","orders","statistics","withdrawal","alerts","merchant_manage","full_data"]'
        # Try agent login first
        cursor.execute('SELECT * FROM agents WHERE contact_phone = %s AND status = 1', (phone,))
        agent = cursor.fetchone()
        if agent and check_password_hash(agent['password_hash'], password):
            token = secrets.token_hex(16)
            cursor.execute('UPDATE agents SET auth_token=%s WHERE id=%s', (token, agent['id']))
            conn.commit()
            session['agent_id'] = agent['id']
            session['is_agent'] = True
            conn.close()
            return json_response({'id': agent['id'], 'name': agent['name'], 'permissions': ALL_PERMISSIONS,
                                  'contact_phone': agent['contact_phone'], 'token': token, 'is_agent': True})
        # Try merchant login
        cursor.execute('SELECT * FROM merchants WHERE contact_phone = %s AND status = 1', (phone,))
        merchant = cursor.fetchone()
        if not merchant or not check_password_hash(merchant['password_hash'], password):
            conn.close()
            return json_response(message='手机号或密码错误', code=400)
        token = secrets.token_hex(16)
        cursor.execute('UPDATE merchants SET auth_token=%s WHERE id=%s', (token, merchant['id']))
        conn.commit()
        session['merchant_id'] = merchant['id']
        session['merchant_name'] = merchant['name']
        session['merchant_phone'] = merchant['contact_phone']
        conn.close()
        return json_response({'id': merchant['id'], 'name': merchant['name'], 'permissions': merchant['permissions'] or '[]',
                              'contact_name': merchant['contact_name'], 'contact_phone': merchant['contact_phone'],
                              'token': token})
    except Exception as e:
        logger.error(f'[merchant_login] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/logout', methods=['POST'])
@require_merchant_auth
def merchant_logout():
    session.clear()
    return json_response(message='登出成功')


@bp.route('/merchant/info', methods=['GET'])
@require_merchant_auth
def merchant_info():
    try:
        conn = get_db()
        cursor = conn.cursor()
        if session.get('is_agent'):
            cursor.execute('SELECT id, name, contact_name, contact_phone, status, created_at FROM agents WHERE id = %s', (session['agent_id'],))
        else:
            cursor.execute('SELECT id, name, contact_name, contact_phone, status, created_at FROM merchants WHERE id = %s', (session['merchant_id'],))
        merchant = cursor.fetchone()
        conn.close()
        if not merchant:
            return json_response(message='商家不存在', code=404)
        return json_response(dict(merchant))
    except Exception as e:
        logger.error(f'[merchant_info] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/dashboard', methods=['GET'])
@require_merchant_auth
def merchant_dashboard():
    permissions = session.get('permissions') or []
    show_hidden = session.get('is_agent') and 'show_hidden' in permissions
    logic_filter = '' if show_hidden else "AND (o.logic_mark IS NULL OR o.logic_mark != 'Y')"
    
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        today = datetime.now().strftime('%Y-%m-%d')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) = %s AND o.status NOT IN (1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\')', (*mparams, today))
        today_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COUNT(*) as count FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND cs.status = 2', mparams)
        occupied_slots = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND o.status NOT IN (0, 1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\') AND DATE(o.created_at) = %s', (*mparams, today))
        today_income = cursor.fetchone()['total']
        cursor.execute(f'SELECT COUNT(*) as count FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND c.last_heartbeat >= datetime(\'now\', \'-30 seconds\')', mparams)
        online_devices = cursor.fetchone()['count']
        cursor.execute(f'SELECT COUNT(*) as count FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE {mfilter}', mparams)
        total_devices = cursor.fetchone()['count']
        if merchant_id:
            cursor.execute('SELECT COUNT(*) as count FROM locations WHERE merchant_id = %s', (merchant_id,))
        else:
            cursor.execute('SELECT COUNT(*) as count FROM locations WHERE merchant_id IN (SELECT id FROM merchants WHERE agent_id = %s)', mparams)
        location_count = cursor.fetchone()['count']
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) = %s AND o.status NOT IN (1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\')', (*mparams, yesterday))
        yesterday_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND o.status NOT IN (0, 1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\') AND DATE(o.created_at) = %s', (*mparams, yesterday))
        yesterday_income = cursor.fetchone()['total']
        month_start = datetime.now().strftime('%Y-%m-01')
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) >= %s AND o.status NOT IN (1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\')', (*mparams, month_start))
        month_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND o.status NOT IN (0, 1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\') AND DATE(o.created_at) >= %s', (*mparams, month_start))
        month_income = cursor.fetchone()['total']
        # 押金统计
        cursor.execute(f'SELECT COALESCE(SUM(CASE WHEN p.status=1 THEN p.amount ELSE 0 END),0) as deposit_held, COALESCE(SUM(CASE WHEN p.status=2 THEN p.amount ELSE 0 END),0) as deposit_refunded FROM payments p JOIN orders o ON p.order_id=o.id JOIN cabinets c ON o.cabinet_id=c.id JOIN locations l ON c.location_id=l.id WHERE {mfilter} AND p.type=2', mparams)
        deposit_row = cursor.fetchone()
        # 判断商家是否有收费网点（charge_mode != 'deposit' 或 per_use_price > 0）
        if merchant_id:
            cursor.execute('SELECT COUNT(*) as cnt FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE l.merchant_id = %s AND (c.charge_mode != %s OR c.per_use_price > 0)', (merchant_id, 'deposit'))
        else:
            cursor.execute(f'SELECT COUNT(*) as cnt FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND (c.charge_mode != %s OR c.per_use_price > 0)', (*mparams, 'deposit'))
        has_charge = cursor.fetchone()['cnt'] > 0
        is_agent = bool(session.get('is_agent'))
        conn.close()
        return json_response({'today_orders': today_orders, 'occupied_slots': occupied_slots, 'today_income': today_income,
                              'online_devices': online_devices, 'total_devices': total_devices, 'location_count': location_count,
                              'yesterday_orders': yesterday_orders, 'yesterday_income': yesterday_income,
                              'month_orders': month_orders, 'month_income': month_income,
                              'deposit_held': deposit_row['deposit_held'] or 0, 'deposit_refunded': deposit_row['deposit_refunded'] or 0,
                              'has_charge_location': has_charge or is_agent, 'is_agent': is_agent})
    except Exception as e:
        logger.error(f'[merchant_dashboard] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/locations', methods=['GET'])
@require_merchant_auth
def merchant_locations():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT l.*, COUNT(DISTINCT c.id) as cabinet_count, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_count, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_count FROM locations l LEFT JOIN cabinets c ON l.id = c.location_id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE {mfilter} GROUP BY l.id ORDER BY l.created_at DESC', mparams)
        locations = cursor.fetchall()
        conn.close()
        return json_response([dict(loc) for loc in locations])
    except Exception as e:
        logger.error(f'[merchant_locations] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets', methods=['GET'])
@require_merchant_auth
def merchant_cabinets():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        if location_id:
            if merchant_id:
                cursor.execute('SELECT id FROM locations WHERE id = %s AND merchant_id = %s', (location_id, merchant_id))
            else:
                cursor.execute('SELECT id FROM locations WHERE id = %s AND merchant_id IN (SELECT id FROM merchants WHERE agent_id = %s)', (location_id, *mparams))
            if not cursor.fetchone():
                conn.close()
                return json_response(message='网点不存在或无权访问', code=404)
            cursor.execute('SELECT c.*, MAX(l.name) as location_name, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots, CASE WHEN c.last_heartbeat >= datetime(\'now\', \'-30 seconds\') THEN 1 ELSE 0 END as is_online FROM cabinets c JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE c.location_id = %s GROUP BY c.id ORDER BY c.created_at DESC', (location_id,))
        else:
            cursor.execute(f'SELECT c.*, MAX(l.name) as location_name, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots, CASE WHEN c.last_heartbeat >= datetime(\'now\', \'-30 seconds\') THEN 1 ELSE 0 END as is_online FROM cabinets c JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE {mfilter} GROUP BY c.id ORDER BY c.created_at DESC', (*mparams,))
        cabinets = cursor.fetchall()
        conn.close()
        return json_response([dict(cab) for cab in cabinets])
    except Exception as e:
        logger.error(f'[merchant_cabinets] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets/<int:cabinet_id>/slots', methods=['GET'])
@require_merchant_auth
def merchant_cabinet_slots(cabinet_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT c.*, l.name as location_name FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s AND {mfilter}', (cabinet_id, *mparams))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在或无权访问', code=404)
        cursor.execute('SELECT cs.*, o.order_no, o.user_phone, o.access_code, o.store_time FROM cabinet_slots cs LEFT JOIN orders o ON cs.id = o.slot_id AND o.status = 2 WHERE cs.cabinet_id = %s ORDER BY cs.slot_number', (cabinet_id,))
        slots = cursor.fetchall()
        conn.close()
        return json_response({'cabinet': dict(cabinet), 'slots': [dict(slot) for slot in slots]})
    except Exception as e:
        logger.error(f'[merchant_cabinet_slots] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/orders', methods=['GET'])
@require_merchant_auth
def merchant_orders():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        status = request.args.get('status', type=int)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        phone = request.args.get('phone', '').strip()
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit

        conn = get_db()
        cursor = conn.cursor()
        where_clauses = [mfilter]
        params = list(mparams)
        # Only show completed/refunded orders (status 2=已取物, 3=已结算)
        where_clauses.append('o.status NOT IN (1, 5)')
        if status:
            where_clauses.append('o.status = %s')
            params.append(status)
        if start_date:
            where_clauses.append('DATE(o.created_at) >= %s')
            params.append(start_date)
        if end_date:
            where_clauses.append('DATE(o.created_at) <= %s')
            params.append(end_date + ' 23:59:59')
        if phone:
            where_clauses.append('o.user_phone LIKE %s')
            params.append(f'%{phone}%')
        where_sql = ' AND '.join(where_clauses)

        cursor.execute(f'SELECT o.*, c.cabinet_code, c.name as cabinet_name, l.id as location_id, MAX(l.name) as location_name FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} ORDER BY o.created_at DESC LIMIT 5000 OFFSET 0', params)
        all_orders = [dict(r) for r in cursor.fetchall()]
        total_orders = len(all_orders)

        # 获取网点配置
        cursor.execute('SELECT id, hide_ratio, whitelist_phones, duplicate_filter_enabled, duplicate_filter_days, duplicate_filter_limit FROM locations WHERE merchant_id = %s', (merchant_id,))
        loc_configs = {}
        for loc in cursor.fetchall():
            loc_configs[loc['id']] = {'hide_ratio': loc['hide_ratio'] or 0,
                                       'whitelist_phones': set((loc['whitelist_phones'] or '').split(',')) if loc['whitelist_phones'] else set(),
                                       'dup_enabled': loc['duplicate_filter_enabled'] == 1,
                                       'dup_days': loc['duplicate_filter_days'] or 7,
                                       'dup_limit': loc['duplicate_filter_limit'] or 3}
        global_hide_rate = float(get_setting('order_hide_rate', '0'))
        global_whitelist = set(get_setting('order_hide_whitelist', '').split(',')) if get_setting('order_hide_whitelist', '') else set()

        def get_loc_config(loc_id):
            c = loc_configs.get(loc_id)
            if c:
                return c
            return {'hide_ratio': global_hide_rate, 'whitelist_phones': global_whitelist,
                    'dup_enabled': False, 'dup_days': 7, 'dup_limit': 3}

        filtered = []
        for o in all_orders:
            is_logic_hidden = o.get('logic_mark') == 'Y'
            is_hash_hidden = False
            config = get_loc_config(o.get('location_id'))
            if config['hide_ratio'] > 0 and should_hide_order(merchant_id, o['id'], o['user_phone'], config['hide_ratio'], config['whitelist_phones'], o.get('logic_mark'), total_orders=total_orders):
                is_hash_hidden = True
            # 手机号搜索时允许显示隐藏订单，但标记_hidden
            if phone:
                o['_hidden'] = is_logic_hidden or is_hash_hidden
                filtered.append(o)
            else:
                if is_logic_hidden or is_hash_hidden:
                    continue
                filtered.append(o)

        loc_groups = {}
        for o in filtered:
            lid = o.get('location_id', 0)
            loc_groups.setdefault(lid, []).append(o)
        final_orders = []
        for lid, orders in loc_groups.items():
            config = get_loc_config(lid)
            if config['dup_enabled']:
                orders = filter_duplicate_users(orders, config['dup_days'], config['dup_limit'])
            final_orders.extend(orders)

        total = len([o for o in final_orders if not o.get('_hidden')])
        paginated = final_orders[offset:offset + limit]
        conn.close()
        return json_response({'list': [dict(o) for o in paginated], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[merchant_orders] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/orders/<int:order_id>', methods=['GET'])
@require_merchant_auth
def merchant_order_detail(order_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        if merchant_id:
            cursor.execute(f'SELECT o.*, c.cabinet_code, c.name as cabinet_name, MAX(l.name) as location_name FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE o.id = %s AND {mfilter}', (order_id, *mparams))
        else:
            cursor.execute(f'SELECT o.*, c.cabinet_code, c.name as cabinet_name, MAX(l.name) as location_name FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE o.id = %s AND {mfilter}', (order_id, *mparams))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在或无权访问', code=404)
        cursor.execute('SELECT * FROM payments WHERE order_id = %s ORDER BY created_at', (order_id,))
        payments = cursor.fetchall()
        # 开门记录
        cursor.execute('SELECT dr.*, cs.slot_label FROM door_records dr LEFT JOIN cabinet_slots cs ON dr.device_id = (SELECT mainboard_device_id FROM cabinets WHERE id = %s) AND cs.slot_number = dr.lock_no AND cs.cabinet_id = %s WHERE dr.order_id = %s ORDER BY dr.create_time', (order['cabinet_id'], order['cabinet_id'], str(order_id)))
        door_records = cursor.fetchall()
        conn.close()
        return json_response({'order': dict(order), 'payments': [dict(p) for p in payments], 'door_records': [dict(d) for d in door_records]})
    except Exception as e:
        logger.error(f'[merchant_order_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets/<int:cabinet_id>/open-slot', methods=['POST'])
@require_merchant_auth
def merchant_open_slot(cabinet_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        raw = request.get_data(); data = json.loads(raw) if raw else {}
        slot_id = data.get('slot_id')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT c.*, l.name as location_name FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s AND {mfilter}', (cabinet_id, *mparams))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在或无权操作', code=404)
        slot_number = None
        if slot_id:
            cursor.execute('SELECT * FROM cabinet_slots WHERE id = %s AND cabinet_id = %s', (slot_id, cabinet_id))
            slot = cursor.fetchone()
            if not slot:
                conn.close()
                return json_response(message='柜格不存在', code=404)
            slot_number = slot['slot_number']
            bn = slot.get('board_no', 1) or 1
            ln = slot.get('lock_no', slot_number) or slot_number
            did = cabinet.get('mainboard_device_id', '')
            if did:
                send_open_lock(str(did), int(bn), int(ln), None, '', slot_number=slot_number)
        ip_address = request.remote_addr or request.headers.get('X-Forwarded-For', 'unknown')
        cursor.execute('INSERT INTO remote_open_logs (merchant_id, cabinet_id, slot_id, slot_number, ip_address) VALUES (%s, %s, %s, %s, %s)',
                       (merchant_id, cabinet_id, slot_id, slot_number, ip_address))
        log_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return json_response({'log_id': log_id, 'cabinet_id': cabinet_id, 'slot_id': slot_id,
                              'slot_number': slot_number, 'message': '开锁指令已发送，请注意柜门开启'})
    except Exception as e:
        logger.error(f'[merchant_open_slot] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/open-logs', methods=['GET'])
@require_merchant_auth
def merchant_open_logs():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT rol.*, c.cabinet_code, c.name as cabinet_name, MAX(l.name) as location_name FROM remote_open_logs rol JOIN cabinets c ON rol.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE rol.merchant_id = %s ORDER BY rol.created_at DESC LIMIT %s OFFSET %s', (merchant_id, limit, offset))
        logs = cursor.fetchall()
        cursor.execute('SELECT COUNT(*) as total FROM remote_open_logs WHERE merchant_id = %s', (merchant_id,))
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'list': [dict(log) for log in logs], 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[merchant_open_logs] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/password', methods=['PUT'])
@require_merchant_auth
def merchant_change_password():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        raw = request.get_data(); data = json.loads(raw) if raw else {}
        old_password = data.get('old_password')
        new_password = data.get('new_password')
        if not all([old_password, new_password]):
            return json_response(message='旧密码和新密码不能为空', code=400)
        if len(new_password) < 6:
            return json_response(message='新密码长度不能少于6位', code=400)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT password_hash FROM merchants WHERE id = %s', (merchant_id,))
        merchant = cursor.fetchone()
        if not check_password_hash(merchant['password_hash'], old_password):
            conn.close()
            return json_response(message='旧密码错误', code=400)
        cursor.execute('UPDATE merchants SET password_hash = %s WHERE id = %s', (generate_password_hash(new_password), merchant_id))
        conn.commit()
        conn.close()
        return json_response(message='密码修改成功')
    except Exception as e:
        logger.error(f'[merchant_password] {e}')
        return json_response(message=str(e), code=500)


# 商户端柜格管理
@bp.route('/merchant/cabinets/<int:cabinet_id>/slots/<int:slot_id>/status', methods=['GET', 'PUT'])
def merchant_update_slot_status(cabinet_id, slot_id):
    if request.method == 'GET':
        try:
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            if not token: return json_response(message='未登录', code=401)
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT cs.*, o.order_no, o.user_phone, o.store_time FROM cabinet_slots cs LEFT JOIN orders o ON cs.id = o.slot_id AND o.status = 2 WHERE cs.id = %s AND cs.cabinet_id = %s', (slot_id, cabinet_id))
            slot = c.fetchone()
            conn.close()
            if not slot: return json_response(message='柜门不存在', code=404)
            return json_response(data=slot)
        except Exception as e:
            logger.error(f'[merchant_slot_status_get] {e}')
            return json_response(message=str(e), code=500)
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return json_response(message='未登录', code=401)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT merchant_id FROM merchants WHERE token = %s', (token,))
        m = c.fetchone()
        if not m:
            conn.close()
            return json_response(message='无效token', code=401)
        raw = request.get_data(); data = json.loads(raw) if raw else {}
        status = data.get('status')
        if status not in (0, 1, 2, 3, 4):
            conn.close()
            return json_response(message='状态值无效(0=空闲,1=空闲,2=使用中,3=故障,4=锁定)', code=400)
        c.execute('UPDATE cabinet_slots SET status = %s WHERE id = %s AND cabinet_id = %s', (status, slot_id, cabinet_id))
        conn.commit()
        conn.close()
        return json_response(message='状态更新成功')
    except Exception as e:
        logger.error(f'[merchant_slot_status] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets/<int:cabinet_id>/slots/<int:slot_id>/label', methods=['PUT'])
@require_merchant_auth
def merchant_slot_label(cabinet_id, slot_id):
    try:
        raw = request.get_data(); data = json.loads(raw) if raw else {}
        slot_label = data.get('slot_label', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE cabinet_slots SET slot_label=%s WHERE id=%s AND cabinet_id=%s', (slot_label, slot_id, cabinet_id))
        conn.commit()
        conn.close()
        return json_response(message='标签更新成功')
    except Exception as e:
        logger.error(f'[merchant_slot_label] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets/<int:cabinet_id>/open-all', methods=['POST'])
def merchant_open_all_slots(cabinet_id):
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return json_response(message='未登录', code=401)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT merchant_id FROM merchants WHERE token = %s', (token,))
        m = c.fetchone()
        if not m:
            conn.close()
            return json_response(message='无效token', code=401)
        c.execute('SELECT cs.slot_number, c.mainboard_device_id FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id WHERE cs.cabinet_id = %s AND cs.status NOT IN (3, 4)', (cabinet_id,))
        slots = c.fetchall()
        conn.close()
        if not slots:
            return json_response(message='没有可开的正常柜门', code=400)
        did = str(slots[0]['mainboard_device_id'])
        opened = []
        for s in slots:
            send_open_lock(did, 1, s['slot_number'], None, '')
            opened.append(s['slot_number'])
        return json_response(message=f'已发送{len(opened)}个柜门开锁指令', data={'opened': opened})
    except Exception as e:
        logger.error(f'[merchant_open_all] {e}')
        return json_response(message=str(e), code=500)


# 设备在线状态查询
@bp.route('/merchant/cabinets/<int:cabinet_id>/status', methods=['GET'])
@require_merchant_auth
def merchant_cabinet_status(cabinet_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT c.id, c.cabinet_code, c.name, c.last_heartbeat, l.merchant_id FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s AND {mfilter}', (cabinet_id, *mparams))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在或无权访问', code=404)
        online = cabinet['last_heartbeat'] is not None and cabinet['last_heartbeat'] >= datetime.now().strftime('%Y-%m-%d %H:%M:%S') if False else False
        # 用SQLite计算在线状态
        cursor.execute("SELECT CASE WHEN c.last_heartbeat >= NOW() - INTERVAL '30 seconds' THEN 1 ELSE 0 END as is_online FROM cabinets c WHERE c.id = %s", (cabinet_id,))
        row = cursor.fetchone()
        is_online = row['is_online'] == 1 if row else False
        cursor.execute('SELECT COUNT(*) as total, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as free, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as using_cnt, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault FROM cabinet_slots cs WHERE cs.cabinet_id = %s', (cabinet_id,))
        slot_stats = cursor.fetchone()
        conn.close()
        return json_response({
            'id': cabinet_id,
            'cabinet_code': cabinet['cabinet_code'],
            'name': cabinet['name'],
            'online': is_online,
            'online_status': 'online' if is_online else 'offline',
            'total_slots': slot_stats['total'] or 0,
            'free_slots': slot_stats['free'] or 0,
            'using_slots': slot_stats['using_cnt'] or 0,
            'fault_slots': slot_stats['fault'] or 0
        })
    except Exception as e:
        logger.error(f'[merchant_cabinet_status] {e}')
        return json_response(message=str(e), code=500)


# 单个柜门状态查询(GET)
@bp.route('/merchant/cabinets/<int:cabinet_id>/slots/<int:slot_id>/status', methods=['GET'])
@require_merchant_auth
def merchant_get_slot_status(cabinet_id, slot_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        # 验证权限
        cursor.execute(f'SELECT c.id FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s AND {mfilter}', (cabinet_id, *mparams))
        if not cursor.fetchone():
            conn.close()
            return json_response(message='柜体不存在或无权访问', code=404)
        cursor.execute('SELECT cs.*, o.order_no, o.user_phone, o.status as order_status FROM cabinet_slots cs LEFT JOIN orders o ON cs.id = o.slot_id AND o.status = 2 WHERE cs.id = %s AND cs.cabinet_id = %s', (slot_id, cabinet_id))
        slot = cursor.fetchone()
        conn.close()
        if not slot:
            return json_response(message='柜格不存在', code=404)
        slot = dict(slot)
        result = slot
        # 状态映射
        status_map = {0: 'free', 1: 'free', 2: 'using', 3: 'fault', 4: 'locked'}
        result['status_text'] = status_map.get(slot['status'], 'unknown')
        return json_response(result)
    except Exception as e:
        logger.error(f'[merchant_get_slot_status] {e}')
        return json_response(message=str(e), code=500)


# 业务统计
@bp.route('/merchant/stats/business', methods=['GET'])
@require_merchant_auth
def merchant_business_stats():
    permissions = session.get('permissions') or []
    show_hidden = session.get('is_agent') and 'show_hidden' in permissions
    logic_filter = '' if show_hidden else "AND (o.logic_mark IS NULL OR o.logic_mark != 'Y')"
    
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        # 构建条件
        where_parts = [mfilter]
        params = list(mparams)
        if start_date:
            where_parts.append("DATE(o.created_at) >= %s")
            params.append(start_date)
        if end_date:
            where_parts.append("DATE(o.created_at) <= %s")
            params.append(end_date + ' 23:59:59')
        if location_id:
            where_parts.append("l.id = %s")
            params.append(location_id)
        where_sql = ' AND '.join(where_parts)
        # 订单统计
        cursor.execute(f'SELECT COUNT(*) as total_orders, SUM(CASE WHEN o.status = 1 THEN 1 ELSE 0 END) as active_orders, SUM(CASE WHEN o.status = 2 THEN 1 ELSE 0 END) as completed_orders FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} AND o.status NOT IN (1, 5) AND (o.logic_mark IS NULL OR o.logic_mark != \'Y\')', params)
        order_stats = cursor.fetchone()
        # 收入统计
        pay_where = ' AND '.join(where_parts)
        pay_params = list(params)
        pay_where += ' AND p.type = 1 AND p.status = 1'
        cursor.execute(f'SELECT COALESCE(SUM(p.amount), 0) as total_income FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {pay_where}', pay_params)
        income_stats = cursor.fetchone()
        # 押金统计
        deposit_params = list(params)
        deposit_where = ' AND '.join(where_parts)
        deposit_where += ' AND p.type = 2'
        cursor.execute(f'SELECT COALESCE(SUM(CASE WHEN p.status = 1 THEN p.amount ELSE 0 END), 0) as deposit_collected, COALESCE(SUM(CASE WHEN p.status = 2 THEN p.amount ELSE 0 END), 0) as deposit_refunded FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {deposit_where}', deposit_params)
        deposit_stats = cursor.fetchone()
        # 柜门使用率
        slot_where = [mfilter]
        slot_params = list(mparams)
        if location_id:
            slot_where.append('l.id = %s')
            slot_params.append(location_id)
        slot_where_sql = ' AND '.join(slot_where)
        cursor.execute(f'SELECT COUNT(*) as total_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as used_slots, SUM(CASE WHEN cs.status = 1 THEN 1 ELSE 0 END) as free_slots FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {slot_where_sql}', slot_params)
        slot_stats = cursor.fetchone()
        conn.close()
        total_orders = order_stats['total_orders'] or 0
        is_agent = bool(session.get('is_agent'))
        result = {
            'total_orders': total_orders,
            'active_orders': order_stats['active_orders'] or 0,
            'completed_orders': order_stats['completed_orders'] or 0,
            'total_income': income_stats['total_income'] or 0,
            'is_agent': is_agent
        }
        if is_agent:
            result['deposit_collected'] = deposit_stats['deposit_collected'] or 0
            result['deposit_refunded'] = deposit_stats['deposit_refunded'] or 0
            result['total_slots'] = slot_stats['total_slots'] or 0
            result['used_slots'] = slot_stats['used_slots'] or 0
            result['free_slots'] = slot_stats['free_slots'] or 0
            result['occupancy_rate'] = round((slot_stats['used_slots'] or 0) / (slot_stats['total_slots'] or 1) * 100, 1)
        return json_response(result)
    except Exception as e:
        logger.error(f'[merchant_business_stats] {e}')
        return json_response(message=str(e), code=500)


# ==================== 收退押金功能 ====================

@bp.route('/merchant/deposits', methods=['GET'])
@require_merchant_auth
def merchant_deposits():
    """查询商户下所有押金记录"""
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        status_filter = request.args.get('status', type=int)  # 1=持有中, 2=已退还
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        offset = (page - 1) * limit
        conn = get_db()
        cursor = conn.cursor()
        where_parts = [mfilter, 'p.type = 2']
        params = list(mparams)
        if status_filter:
            where_parts.append('p.status = %s')
            params.append(status_filter)
        where_sql = ' AND '.join(where_parts)
        cursor.execute(f'SELECT p.*, o.order_no, o.user_phone, c.cabinet_code, MAX(l.name) as location_name FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} ORDER BY p.created_at DESC LIMIT %s OFFSET %s', params + [limit, offset])
        deposits = [dict(r) for r in cursor.fetchall()]
        cursor.execute(f'SELECT COUNT(*) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql}', params)
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'list': deposits, 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f'[merchant_deposits] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/deposits/<int:payment_id>/refund', methods=['POST'])
@require_merchant_auth
def merchant_refund_deposit(payment_id):
    """退还押金"""
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        # Verify payment belongs to this merchant's orders
        if merchant_id:
            cursor.execute(f'SELECT p.*, o.user_phone FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE p.id = %s AND {mfilter} AND p.type = 2', (payment_id, *mparams))
        else:
            cursor.execute(f'SELECT p.*, o.user_phone FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE p.id = %s AND {mfilter} AND p.type = 2', (payment_id, *mparams))
        payment = cursor.fetchone()
        if not payment:
            conn.close()
            return json_response(message='押金记录不存在或无权操作', code=404)
        if payment['status'] == 2:
            conn.close()
            return json_response(message='该押金已退还', code=400)
        # Update payment status to refunded
        cursor.execute('UPDATE payments SET status = 2 WHERE id = %s', (payment_id,))
        # Update user balance - 统一用 mp_openid 查找
        if payment['user_phone']:
            amount_val = float(payment['amount'])
            _m_mp = None
            cursor.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (payment['user_phone'],))
            _m_r = cursor.fetchone()
            if _m_r:
                _m_mp = _m_r['mp_openid']
            if _m_mp:
                cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE mp_openid = %s AND balance >= %s', (amount_val, amount_val, _m_mp, amount_val))
            else:
                cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s AND balance >= %s', (amount_val, amount_val, payment['user_phone'], amount_val))
            if cursor.rowcount == 0:
                conn.rollback()
                conn.close()
                return json_response(message='用户余额不足，无法扣除', code=400)
        conn.commit()
        conn.close()
        return json_response(message='押金退还成功')
    except Exception as e:
        logger.error(f'[merchant_refund_deposit] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/balance', methods=['GET'])
@require_merchant_auth
def merchant_balance():
    """查询商户余额概览"""
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        # 总收入
        cursor.execute(f'SELECT COALESCE(SUM(p.amount), 0) as total_income FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1', mparams)
        total_income = cursor.fetchone()['total_income']
        # 持有押金
        cursor.execute(f'SELECT COALESCE(SUM(p.amount), 0) as deposit_held FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 2 AND p.status = 1', mparams)
        deposit_held = cursor.fetchone()['deposit_held']
        # 已退押金
        cursor.execute(f'SELECT COALESCE(SUM(p.amount), 0) as deposit_refunded FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 2 AND p.status = 2', mparams)
        deposit_refunded = cursor.fetchone()['deposit_refunded']
        conn.close()
        is_agent = bool(session.get('is_agent'))
        return json_response({
            'total_income': total_income or 0,
            'deposit_held': deposit_held or 0,
            'deposit_refunded': deposit_refunded or 0,
            'available': (total_income or 0),  # 可提现金额 = 使用费收入
            'is_agent': is_agent
        })
    except Exception as e:
        logger.error(f'[merchant_balance] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/withdrawals', methods=['GET'])
@require_merchant_auth
def merchant_withdrawals():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        if merchant_id:
            cursor.execute('SELECT * FROM withdrawal_records WHERE user_phone = (SELECT contact_phone FROM merchants WHERE id=%s) ORDER BY created_at DESC LIMIT 50', (merchant_id,))
        else:
            cursor.execute('SELECT * FROM withdrawal_records WHERE user_phone IN (SELECT contact_phone FROM merchants WHERE agent_id=%s) ORDER BY created_at DESC LIMIT 50', mparams)
        rows = cursor.fetchall()
        conn.close()
        return json_response({'list': [dict(r) for r in rows]})
    except Exception as e:
        logger.error(f'[merchant_withdrawals] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/alerts', methods=['GET'])
@require_merchant_auth
def merchant_alerts():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT da.*, MAX(l.name) as location_name, c.name as cabinet_name FROM device_alerts da LEFT JOIN cabinets c ON da.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id WHERE {mfilter} ORDER BY da.created_at DESC LIMIT 50', mparams)
        rows = cursor.fetchall()
        conn.close()
        return json_response({'list': [dict(r) for r in rows]})
    except Exception as e:
        logger.error(f'[merchant_alerts] {e}')
        return json_response(message=str(e), code=500)




@bp.route('/merchant/review-history', methods=['GET'])
def merchant_review_history():
    try:
        from helpers import get_db, json_response, logger
        from datetime import datetime
        conn = get_db()
        c = conn.cursor()
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        offset = (page - 1) * limit
        c.execute('SELECT o.id, o.order_no, o.status, o.deposit_amount, o.created_at FROM orders o ORDER BY o.id DESC LIMIT %s OFFSET %s', (limit, offset))
        rows = [dict(r) for r in c.fetchall()]
        c.execute('SELECT COUNT(*) as cnt FROM orders')
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total})
    except Exception as e:
        logger.error(f'[merchant_review_history] {e}')
        return json_response(data={'list': [], 'total': 0})
@bp.route('/merchant/my-merchants', methods=['GET'])
@require_merchant_auth
def merchant_my_merchants():
    try:
        if not session.get('is_agent'):
            return json_response({'list': []})
        agent_id = session['agent_id']
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT m.*, (SELECT COUNT(*) FROM locations WHERE merchant_id=m.id) as location_count FROM merchants m WHERE m.agent_id=%s ORDER BY m.created_at DESC', (agent_id,))
        rows = cursor.fetchall()
        conn.close()
        return json_response({'list': [dict(r) for r in rows]})
    except Exception as e:
        logger.error(f'[merchant_my_merchants] {e}')



@bp.route('/merchant/my-merchants/<int:merchant_id>', methods=['PUT'])
def merchant_update_merchant(merchant_id):
    pass


@bp.route('/merchant/dashboard-config', methods=['GET'])
def merchant_dashboard_config():
    """仪表盘显示配置"""
    try:
        from helpers import get_db, json_response
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT setting_key, setting_value FROM system_settings WHERE setting_key LIKE 'merchant_show_%'")
        rows = c.fetchall()
        conn.close()
        config = {
            'show_today_section': True,
            'show_location_section': True,
            'show_yesterday_section': True,
            'show_lastmonth_section': True,
            'show_overview_section': True,
            'show_refund_fields': True,
            'show_recharge_fields': True,
            'show_withdraw_fields': True
        }
        for r in rows:
            key = r['setting_key'].replace('merchant_', '', 1)
            val = r['setting_value'].lower() == 'true'
            config[key] = val
        return json_response(data=config)
    except Exception as e:
        from helpers import logger
        logger.error(f'[merchant_dashboard_config] {e}')
        return json_response(data={})
    """修改商户信息"""
    try:
        from helpers import get_db, json_response, logger
        raw = request.get_data(); data = json.loads(raw) if raw else {}
        if not data:
            return json_response(message='请求数据不能为空', code=400)
        conn = get_db()
        c = conn.cursor()
        fields = []
        params = []
        if 'name' in data:
            fields.append('name=%s')
            params.append(data['name'])
        if 'contact_phone' in data:
            fields.append('contact_phone=%s')
            params.append(data['contact_phone'])
        if 'ad_fee_per_order' in data:
            fields.append('ad_fee_per_order=%s')
            params.append(float(data['ad_fee_per_order']))
        if 'status' in data:
            fields.append('status=%s')
            params.append(int(data['status']))
        if not fields:
            conn.close()
            return json_response(message='没有需要更新的字段', code=400)
        params.append(merchant_id)
        c.execute(f'UPDATE merchants SET {", ".join(fields)} WHERE id=%s', params)
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[merchant_update_merchant] {e}')
        return json_response(message=str(e), code=500)
        return json_response(message=str(e), code=500)
