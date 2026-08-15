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
from helpers import (json_response, require_merchant_auth, get_setting, logger, send_open_lock, send_open_all,
                     should_hide_order, filter_duplicate_users, is_device_online,
                     upsert_user_balance_row, find_user_balance_row)

bp = Blueprint('merchant', __name__)

@bp.after_request
def add_no_cache(response):
    if '/merchant/dashboard' in request.path:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

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
        # Try agent login first
        cursor.execute('SELECT * FROM agents WHERE contact_phone = %s AND status = 1', (phone,))
        agent = cursor.fetchone()
        if agent and check_password_hash(agent['password_hash'], password):
            token = secrets.token_hex(16)
            cursor.execute('UPDATE agents SET auth_token=%s WHERE id=%s', (token, agent['id']))
            # Agent concurrent session limit: max 2
            cursor.execute("DELETE FROM user_tokens WHERE user_type='agent' AND user_id=%s AND id NOT IN (SELECT id FROM (SELECT id FROM user_tokens WHERE user_type='agent' AND user_id=%s ORDER BY created_at DESC LIMIT 1) AS k)", (agent['id'], agent['id']))
            cursor.execute("INSERT INTO user_tokens (user_type, user_id, token) VALUES ('agent', %s, %s)", (agent['id'], token))
            conn.commit()
            session['agent_id'] = agent['id']
            session['is_agent'] = True
            agent_perms = json.loads(agent['permissions'] or '[]')
            session['permissions'] = agent_perms
            conn.close()
            return json_response({'id': agent['id'], 'name': agent['name'], 'permissions': json.dumps(agent_perms, ensure_ascii=False),
                                  'contact_phone': agent['contact_phone'], 'token': token, 'is_agent': True})
        # Try merchant login
        cursor.execute('SELECT * FROM merchants WHERE contact_phone = %s AND status = 1', (phone,))
        merchant = cursor.fetchone()
        if not merchant or not check_password_hash(merchant['password_hash'], password):
            # Try employee login
            cursor.execute('SELECT e.*, m.name as mname, a.name as aname FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id LEFT JOIN agents a ON e.agent_id=a.id WHERE e.phone=%s AND e.status = %s', (phone, '1'))
            employee = cursor.fetchone()
            if employee and check_password_hash(employee['password_hash'], password):
                token = secrets.token_hex(16)
                cursor.execute('UPDATE employees SET auth_token=%s WHERE id=%s', (token, employee['id']))
                # Employee concurrent session limit: max 10
                cursor.execute("DELETE FROM user_tokens WHERE user_type='employee' AND user_id=%s AND id NOT IN (SELECT id FROM (SELECT id FROM user_tokens WHERE user_type='employee' AND user_id=%s ORDER BY created_at DESC LIMIT 9) AS k)", (employee['id'], employee['id']))
                cursor.execute("INSERT INTO user_tokens (user_type, user_id, token) VALUES ('employee', %s, %s)", (employee['id'], token))
                conn.commit()
                employee_perms = json.loads(employee['permissions'] or '[]')
                if employee.get('agent_id'):
                    session['agent_id'] = employee['agent_id']
                    session['agent_name'] = employee['aname'] or employee['name']
                    session['is_agent'] = True
                else:
                    session['merchant_id'] = employee['merchant_id']
                    session['merchant_name'] = employee['mname'] or employee['name']
                session['employee_id'] = employee['id']
                session['is_employee'] = True
                session['permissions'] = employee_perms
                conn.close()
                return json_response({'id': employee.get('agent_id') or employee['merchant_id'], 'name': employee['name'],
                                      'contact_phone': employee['phone'], 'token': token, 'is_employee': True,
                                      'is_agent': bool(employee.get('agent_id')),
                                      'permissions': employee['permissions'] or '[]'})
            conn.close()
            return json_response(message='手机号或密码错误', code=400)
        token = secrets.token_hex(16)
        cursor.execute('UPDATE merchants SET auth_token=%s WHERE id=%s', (token, merchant['id']))
        # Merchant concurrent session limit: max 5
        cursor.execute("DELETE FROM user_tokens WHERE user_type='merchant' AND user_id=%s AND id NOT IN (SELECT id FROM (SELECT id FROM user_tokens WHERE user_type='merchant' AND user_id=%s ORDER BY created_at DESC LIMIT 4) AS k)", (merchant['id'], merchant['id']))
        cursor.execute("INSERT INTO user_tokens (user_type, user_id, token) VALUES ('merchant', %s, %s)", (merchant['id'], token))
        conn.commit()
        session['merchant_id'] = merchant['id']
        session['merchant_name'] = merchant['name']
        session['merchant_phone'] = merchant['contact_phone']
        merchant_perms = json.loads(merchant['permissions'] or '[]')
        if 'merchant_manage' in merchant_perms:
            merchant_perms.remove('merchant_manage')
        session['permissions'] = merchant_perms
        conn.close()
        return json_response({'id': merchant['id'], 'name': merchant['name'], 'permissions': json.dumps(merchant_perms, ensure_ascii=False),
                              'contact_name': merchant['contact_name'], 'contact_phone': merchant['contact_phone'],
                              'token': token})
    except Exception as e:
        logger.error(f'[merchant_login] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/logout', methods=['POST'])
@require_merchant_auth
def merchant_logout():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        tok = auth_header[7:].strip()
        if tok:
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute('DELETE FROM user_tokens WHERE token=%s', (tok,))
                conn.commit()
                conn.close()
            except Exception as _e:
                logger.error(f'[logout_token_cleanup] {_e}')
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
    logic_filter = '' if show_hidden else " {hide_filter}"
    hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
    hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
    
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        today = datetime.now().strftime('%Y-%m-%d')
        conn = get_db()
        cursor = conn.cursor()
        filter_merchant_id = request.args.get('merchant_id', type=int)
        if filter_merchant_id and session.get('is_agent'):
            cursor.execute('SELECT id FROM merchants WHERE id=%s AND agent_id=%s', (filter_merchant_id, session.get('agent_id')))
            if cursor.fetchone():
                mfilter = 'l.merchant_id = %s'
                mparams = [filter_merchant_id]
                merchant_id = filter_merchant_id
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) = %s AND o.status NOT IN (1, 5)  {hide_filter}', (*mparams, today))
        today_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COUNT(*) as count FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND cs.status = 2', mparams)
        occupied_slots = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND p.amount < 100000 AND o.status NOT IN (0, 1, 5)  {hide_filter} AND DATE(o.created_at) = %s', (*mparams, today))
        today_income = cursor.fetchone()['total']
        cursor.execute(f'SELECT COUNT(*) as count FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND c.last_heartbeat >= NOW() - INTERVAL \'120 seconds\'', mparams)
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
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) = %s AND o.status NOT IN (1, 5)  {hide_filter}', (*mparams, yesterday))
        yesterday_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND p.amount < 100000 AND o.status NOT IN (0, 1, 5)  {hide_filter} AND DATE(o.created_at) = %s', (*mparams, yesterday))
        yesterday_income = cursor.fetchone()['total']
        month_start = datetime.now().strftime('%Y-%m-01')
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) >= %s AND o.status NOT IN (1, 5)  {hide_filter}', (*mparams, month_start))
        month_orders = cursor.fetchone()['count']
        # merge this month's historical data
        cursor.execute(f'SELECT COALESCE(SUM(visible_count),0) FROM historical_order_counts h JOIN locations l ON h.location_id=l.id WHERE l.merchant_id={mparams[0]} AND date>=%s', (month_start,))
        # merge this month's historical data
        cursor.execute(f'SELECT COALESCE(SUM(visible_count),0) FROM historical_order_counts h JOIN locations l ON h.location_id=l.id WHERE l.merchant_id={mparams[0]} AND date>=%s', (month_start,))
        h_val = cursor.fetchone()[0] or 0
        month_orders = (month_orders or 0) + h_val


        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND p.amount < 100000 AND o.status NOT IN (0, 1, 5)  {hide_filter} AND DATE(o.created_at) >= %s', (*mparams, month_start))
        month_income = cursor.fetchone()['total']

        # 寄存收益（收费模式完成订单）
        cursor.execute(f"SELECT COALESCE(SUM(GREATEST(o.deposit_amount - COALESCE(o.refund_amount,0), 0)), 0) as fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status = 4 AND o.deposit_amount < 100000 AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')  {hide_filter} AND DATE(o.created_at) = %s", (*mparams, today))
        today_storage_income = cursor.fetchone()['fee']
        cursor.execute(f"SELECT COALESCE(SUM(GREATEST(o.deposit_amount - COALESCE(o.refund_amount,0), 0)), 0) as fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status = 4 AND o.deposit_amount < 100000 AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')  {hide_filter} AND DATE(o.created_at) = %s", (*mparams, yesterday))
        yesterday_storage_income = cursor.fetchone()['fee']
        cursor.execute(f"SELECT COALESCE(SUM(GREATEST(o.deposit_amount - COALESCE(o.refund_amount,0), 0)), 0) as fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status = 4 AND o.deposit_amount < 100000 AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')  {hide_filter} AND DATE(o.created_at) >= %s", (*mparams, month_start))
        month_storage_income = cursor.fetchone()['fee']
        # 上月时间范围
        prev_month_end = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m-%d')
        prev_month_start = prev_month_end[:8] + '01'
        # 全景（全部时间）
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status NOT IN (1, 5)  {hide_filter}', mparams)
        total_all_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND p.amount < 100000 AND o.status NOT IN (0, 1, 5)  {hide_filter}', mparams)
        total_all_income = cursor.fetchone()['total']
        cursor.execute(f"SELECT COALESCE(SUM(GREATEST(o.deposit_amount - COALESCE(o.refund_amount,0), 0)), 0) as fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status = 4 AND o.deposit_amount < 100000 AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')  {hide_filter}", mparams)
        total_all_storage_income = cursor.fetchone()['fee']
        # 上月
        cursor.execute(f'SELECT COUNT(*) as count FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) BETWEEN %s AND %s AND o.status NOT IN (1, 5)  {hide_filter}', (*mparams, prev_month_start, prev_month_end))
        prev_month_orders = cursor.fetchone()['count']
        cursor.execute(f'SELECT COALESCE(SUM(COALESCE(p.amount, 0)), 0) as total FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND p.type = 1 AND p.status = 1 AND p.amount < 100000 AND o.status NOT IN (0, 1, 5)  {hide_filter} AND DATE(o.created_at) BETWEEN %s AND %s', (*mparams, prev_month_start, prev_month_end))
        prev_month_income = cursor.fetchone()['total']
        cursor.execute(f"SELECT COALESCE(SUM(GREATEST(o.deposit_amount - COALESCE(o.refund_amount,0), 0)), 0) as fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status = 4 AND o.deposit_amount < 100000 AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')  {hide_filter} AND DATE(o.created_at) BETWEEN %s AND %s", (*mparams, prev_month_start, prev_month_end))
        prev_month_storage_income = cursor.fetchone()['fee']
        # 押金统计
        cursor.execute(f'SELECT COALESCE(SUM(CASE WHEN p.status=1 THEN p.amount ELSE 0 END),0) as deposit_held, COALESCE(SUM(CASE WHEN p.status=2 THEN p.amount ELSE 0 END),0) as deposit_refunded FROM payments p JOIN orders o ON p.order_id=o.id JOIN cabinets c ON o.cabinet_id=c.id JOIN locations l ON c.location_id=l.id WHERE {mfilter} AND p.type=2 AND p.amount < 100000', mparams)
        deposit_row = cursor.fetchone()
        # 各时间段押金退还（提现金额）
        cursor.execute(f'SELECT COALESCE(SUM(p.amount),0) as total FROM payments p JOIN orders o ON p.order_id=o.id JOIN cabinets c ON o.cabinet_id=c.id JOIN locations l ON c.location_id=l.id WHERE {mfilter} AND p.type=2 AND p.amount < 100000 AND p.status=2 AND DATE(p.created_at)=%s', (*mparams, today))
        today_deposit_refunded = cursor.fetchone()['total']
        cursor.execute(f'SELECT COALESCE(SUM(p.amount),0) as total FROM payments p JOIN orders o ON p.order_id=o.id JOIN cabinets c ON o.cabinet_id=c.id JOIN locations l ON c.location_id=l.id WHERE {mfilter} AND p.type=2 AND p.amount < 100000 AND p.status=2 AND DATE(p.created_at)=%s', (*mparams, yesterday))
        yesterday_deposit_refunded = cursor.fetchone()['total']
        cursor.execute(f'SELECT COALESCE(SUM(p.amount),0) as total FROM payments p JOIN orders o ON p.order_id=o.id JOIN cabinets c ON o.cabinet_id=c.id JOIN locations l ON c.location_id=l.id WHERE {mfilter} AND p.type=2 AND p.amount < 100000 AND p.status=2 AND DATE(p.created_at) >= %s', (*mparams, month_start))
        month_deposit_refunded = cursor.fetchone()['total']
        cursor.execute(f'SELECT COALESCE(SUM(p.amount),0) as total FROM payments p JOIN orders o ON p.order_id=o.id JOIN cabinets c ON o.cabinet_id=c.id JOIN locations l ON c.location_id=l.id WHERE {mfilter} AND p.type=2 AND p.amount < 100000 AND p.status=2 AND DATE(p.created_at) BETWEEN %s AND %s', (*mparams, prev_month_start, prev_month_end))
        prev_month_deposit_refunded = cursor.fetchone()['total']
        # 退款统计（退款订单数/退款金额，范围与收入统计对齐）
        def _fee_refund(extra_sql='', extra_params=()):
            cursor.execute(f"SELECT COUNT(*) as refund_orders, COALESCE(SUM(o.refund_amount),0) as refund_amount FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND o.status = 4 AND o.refund_amount > 0 {hide_filter} {extra_sql}", (*mparams, *extra_params))
            r = cursor.fetchone()
            return r['refund_orders'] or 0, float(r['refund_amount'] or 0)
        today_refund_orders, today_refund_amount = _fee_refund('AND DATE(o.created_at) = %s', (today,))
        yesterday_refund_orders, yesterday_refund_amount = _fee_refund('AND DATE(o.created_at) = %s', (yesterday,))
        last_month_refund_orders, last_month_refund_amount = _fee_refund('AND DATE(o.created_at) >= %s', (month_start,))
        prev_month_refund_orders, prev_month_refund_amount = _fee_refund('AND DATE(o.created_at) BETWEEN %s AND %s', (prev_month_start, prev_month_end))
        total_refund_orders, total_refund_amount = _fee_refund()
        total_all_refund_orders, total_all_refund_amount = _fee_refund()
        # 判断商家是否有收费网点（charge_mode != 'deposit' 或 per_use_price > 0）
        if merchant_id:
            cursor.execute('SELECT COUNT(*) as cnt FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE l.merchant_id = %s AND (c.charge_mode != %s OR c.per_use_price > 0)', (merchant_id, 'deposit'))
        else:
            cursor.execute(f'SELECT COUNT(*) as cnt FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND (c.charge_mode != %s OR c.per_use_price > 0)', (*mparams, 'deposit'))
        has_charge = cursor.fetchone()['cnt'] > 0
        is_agent = bool(session.get('is_agent'))
        conn.close()
        return json_response({'today_orders': today_orders, 'occupied_slots': occupied_slots, 'today_income': round(float(today_income or 0), 2),
                              'online_devices': online_devices, 'total_devices': total_devices, 'location_count': location_count,
                              'yesterday_orders': yesterday_orders, 'yesterday_income': round(float(yesterday_income or 0), 2),
                              'month_orders': month_orders, 'month_income': round(float(month_income or 0), 2),
                              'today_storage_income': round(float(today_storage_income or 0), 2),
                              'yesterday_storage_income': round(float(yesterday_storage_income or 0), 2),
                              'month_storage_income': round(float(month_storage_income or 0), 2),
                              'today_refund_orders': today_refund_orders,
                              'today_refund_amount': round(float(today_refund_amount or 0), 2),
                              'yesterday_refund_orders': yesterday_refund_orders,
                              'yesterday_refund_amount': round(float(yesterday_refund_amount or 0), 2),
                              'last_month_refund_orders': last_month_refund_orders,
                              'last_month_refund_amount': round(float(last_month_refund_amount or 0), 2),
                              'prev_month_refund_orders': prev_month_refund_orders,
                              'prev_month_refund_amount': round(float(prev_month_refund_amount or 0), 2),
                              'total_refund_orders': total_refund_orders,
                              'total_refund_amount': round(float(total_refund_amount or 0), 2),
                              'total_all_refund_orders': total_all_refund_orders,
                              'total_all_refund_amount': round(float(total_all_refund_amount or 0), 2),
                              'today_deposit_refunded': round(float(today_refund_amount or 0), 2),
                              'yesterday_deposit_refunded': round(float(yesterday_refund_amount or 0), 2),
                              'month_deposit_refunded': round(float(last_month_refund_amount or 0), 2),
                              'prev_month_orders': prev_month_orders,
                              'prev_month_income': round(float(prev_month_income or 0), 2),
                              'prev_month_storage_income': round(float(prev_month_storage_income or 0), 2),
                              'prev_month_deposit_refunded': round(float(prev_month_refund_amount or 0), 2),
                              'total_all_orders': total_all_orders,
                              'total_all_income': round(float(total_all_income or 0), 2),
                              'total_all_storage_income': round(float(total_all_storage_income or 0), 2),
                              'total_all_deposit_refunded': round(float(total_all_refund_amount or 0), 2),
                              'deposit_held': round(float(deposit_row['deposit_held'] or 0), 2), 'deposit_refunded': round(float(deposit_row['deposit_refunded'] or 0), 2),
                              'has_charge_location': has_charge or is_agent, 'is_agent': is_agent,
                              'show_deposit_fields': 'show_deposit_fields' in permissions})
    except Exception as e:
        logger.error(f'[merchant_dashboard] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/locations', methods=['GET'])
@require_merchant_auth
def merchant_locations():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT l.*, COUNT(DISTINCT c.id) as cabinet_count, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_count, SUM(CASE WHEN cs.status = 1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id = cs.id AND o2.status = 2) THEN 1 ELSE 0 END) as available_count FROM locations l LEFT JOIN cabinets c ON l.id = c.location_id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE {mfilter} GROUP BY l.id ORDER BY l.created_at DESC', mparams)
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
            cursor.execute('SELECT c.*, l.name as location_name, SUM(CASE WHEN cs.status = 1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id = cs.id AND o2.status = 2) THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots, 0 as is_online FROM cabinets c JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE c.location_id = %s GROUP BY c.id, l.name ORDER BY c.created_at DESC', (location_id,))
        else:
            cursor.execute(f'SELECT c.*, l.name as location_name, SUM(CASE WHEN cs.status = 1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id = cs.id AND o2.status = 2) THEN 1 ELSE 0 END) as available_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as occupied_slots, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault_slots, 0 as is_online FROM cabinets c JOIN locations l ON c.location_id = l.id LEFT JOIN cabinet_slots cs ON c.id = cs.cabinet_id WHERE {mfilter} GROUP BY c.id, l.name ORDER BY c.created_at DESC', (*mparams,))
        cabinets = cursor.fetchall()
        conn.close()
        from helpers import is_heartbeat_online
        result = []
        for cab in cabinets:
            d = dict(cab)
            d['is_online'] = 1 if is_heartbeat_online(d.get('last_heartbeat')) else 0
            result.append(d)
        return json_response(result)
    except Exception as e:
        logger.error(f'[merchant_cabinets] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets/<int:cabinet_id>/slots', methods=['GET'])
@require_merchant_auth
def merchant_cabinet_slots(cabinet_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
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
        cabinet_data = dict(cabinet)
        cabinet_data['is_online'] = 1 if is_device_online(str(cabinet_data.get('mainboard_device_id', '')), cabinet_data.get('last_heartbeat')) else 0
        return json_response({'cabinet': cabinet_data, 'slots': [dict(slot) for slot in slots]})
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
        # 只显示成功订单：使用中(2)和已结束(4)
        where_clauses.append('o.status IN (2, 4)')
        if status:
            where_clauses.append('o.status = %s')
            params.append(status)
        if start_date:
            if end_date:
                where_clauses.append('DATE(o.created_at) >= %s')
                params.append(start_date)
            else:
                # 本月：包含所有进行中订单 + 本月其他订单
                where_clauses.append('(o.status = 2 OR DATE(o.created_at) >= %s)')
                params.append(start_date)
        if end_date:
            where_clauses.append('DATE(o.created_at) <= %s')
            params.append(end_date + ' 23:59:59')
        if phone:
            where_clauses.append('o.user_phone LIKE %s')
            params.append(f'%{phone}%')
        where_sql = ' AND '.join(where_clauses)

        cursor.execute(f'SELECT o.*, c.cabinet_code, c.name as cabinet_name, l.id as location_id, l.name as location_name FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} ORDER BY o.created_at DESC', params)
        all_orders = [dict(r) for r in cursor.fetchall()]

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
            is_auto_hidden = o.get('logic_mark') != 'N' and bool(o.get('auto_hidden'))
            is_hidden = is_logic_hidden or is_auto_hidden
            # 手机号搜索时允许显示隐藏订单，但标记_hidden
            if phone:
                o['_hidden'] = is_hidden
                filtered.append(o)
            else:
                if is_hidden:
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
        cursor.execute(f'SELECT o.*, c.cabinet_code, c.name as cabinet_name, l.name as location_name FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE o.id = %s AND {mfilter}', (order_id, *mparams))
        order = cursor.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在或无权访问', code=404)
        cursor.execute('SELECT * FROM payments WHERE order_id = %s ORDER BY created_at', (order_id,))
        payments = cursor.fetchall()
        # 开门记录
        cursor.execute('SELECT dr.*, cs.slot_label FROM door_records dr LEFT JOIN cabinet_slots cs ON dr.device_id = (SELECT mainboard_device_id FROM cabinets WHERE id = %s) AND cs.slot_number = CAST(dr.lock_no AS integer) AND cs.cabinet_id = %s WHERE dr.order_id = %s ORDER BY dr.create_time', (order['cabinet_id'], order['cabinet_id'], str(order_id)))
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
            if did and not is_device_online(str(did), cabinet.get('last_heartbeat')):
                conn.close()
                return json_response(message='设备离线，无法发送开门指令', code=400)
            if did:
                send_open_lock(str(did), int(bn), int(ln), None, '', slot_number=slot_number, require_online=True)
        ip_address = request.remote_addr or request.headers.get('X-Forwarded-For', 'unknown')
        cursor.execute('INSERT INTO remote_open_logs (merchant_id, cabinet_id, slot_id, slot_number, ip_address, operator) VALUES (%s, %s, %s, %s, %s, %s)',
                       (merchant_id, cabinet_id, slot_id, slot_number, ip_address, session.get('merchant_name','') or ''))
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
        cursor.execute('SELECT rol.*, c.cabinet_code, c.name as cabinet_name, l.name as location_name FROM remote_open_logs rol JOIN cabinets c ON rol.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE rol.merchant_id = %s ORDER BY rol.created_at DESC LIMIT %s OFFSET %s', (merchant_id, limit, offset))
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
        c.execute("SELECT id FROM merchants WHERE auth_token = %s", (token,))
        m = c.fetchone()
        if not m:
            c.execute("SELECT merchant_id FROM employees WHERE auth_token = %s", (token,))
            m = c.fetchone()
        if not m:
            c.execute("SELECT id FROM agents WHERE auth_token = %s", (token,))
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
@require_merchant_auth
def merchant_open_all_slots(cabinet_id):
    """一键开门 - 只开正常柜门（通过WS批量指令）"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT mainboard_device_id, last_heartbeat FROM cabinets WHERE id = %s', (cabinet_id,))
        cabinet = c.fetchone()
        if not cabinet or not cabinet['mainboard_device_id']:
            conn.close()
            return json_response(message='未找到设备', code=400)
        did = str(cabinet['mainboard_device_id'])
        if not is_device_online(did, cabinet.get('last_heartbeat')):
            conn.close()
            return json_response(message='设备离线，无法发送开门指令', code=400)
        c.execute('SELECT cs.slot_number FROM cabinet_slots cs WHERE cs.cabinet_id = %s AND cs.status IN (1, 2)', (cabinet_id,))
        slots = c.fetchall()
        conn.close()
        if not slots:
            return json_response(message='没有可开的正常柜门', code=400)
        opened = [s['slot_number'] for s in slots]
        send_open_all(did)
        return json_response(message=f'已发送{len(opened)}个柜门开锁指令（批量）', data={'opened': opened})
    except Exception as e:
        logger.error(f'[merchant_open_all] {e}')
        return json_response(message=str(e), code=500)


# 设备在线状态查询
@bp.route('/merchant/cabinets/<int:cabinet_id>/status', methods=['GET'])
@require_merchant_auth
def merchant_cabinet_status(cabinet_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT c.id, c.cabinet_code, c.name, c.last_heartbeat, l.merchant_id FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s AND {mfilter}', (cabinet_id, *mparams))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='柜体不存在或无权访问', code=404)
        from helpers import is_heartbeat_online
        is_online = is_heartbeat_online(cabinet.get('last_heartbeat'))
        cursor.execute('SELECT COUNT(*) as total, SUM(CASE WHEN cs.status = 1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id = cs.id AND o2.status = 2) THEN 1 ELSE 0 END) as free, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as using_cnt, SUM(CASE WHEN cs.status = 3 THEN 1 ELSE 0 END) as fault FROM cabinet_slots cs WHERE cs.cabinet_id = %s', (cabinet_id,))
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
@bp.route('/merchant/query-door-status', methods=['POST'])
@require_merchant_auth
def merchant_query_door_status():
    """查询柜门物理状态（前端兼容接口）"""
    try:
        data = request.get_json() or {}
        cabinet_id = data.get('cabinet_id')
        board_no = data.get('board_no', 1)
        lock_no = data.get('lock_no')
        if not cabinet_id or not lock_no:
            return json_response(message='参数缺失', code=400)
        conn = get_db()
        c = conn.cursor()
        import urllib.request as _req
        import json as _json
        import uuid
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT mainboard_device_id FROM cabinets WHERE id = %s", (cabinet_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return json_response(message='柜体不存在', code=404)
        did = str(row[0])
        request_id = str(uuid.uuid4())[:8]
        query_cmd = {
            'type': 'query_door_status',
            'request_id': request_id,
            'device_id': did,
            'board_no': board_no,
            'lock_no': lock_no,
            'protocol': 'YBM'
        }
        try:
            _body = _json.dumps({'device_id': did, 'command': query_cmd}).encode()
            _req.urlopen('http://127.0.0.1:5004/send', data=_body, timeout=3)
        except Exception as e:
            from helpers import logger
            logger.error('[query_door_status] %s', str(e))
            return json_response(message='设备可能离线，无法查询物理状态', code=502)
        return json_response(message='查询指令已发送至设备，请稍后查看结果', data={
            'device_id': did, 'board_no': board_no, 'lock_no': lock_no,
            'request_id': request_id, 'query_sent': True
        })
    except Exception as e:
        from helpers import logger
        logger.error('[query_door_status] %s', str(e))
        return json_response(message=str(e), code=500)


@bp.route('/merchant/cabinets/<int:cabinet_id>/slots/<int:slot_id>/status', methods=['GET'])
@require_merchant_auth
def merchant_get_slot_status(cabinet_id, slot_id):
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
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
@bp.route('/merchant/business-stats', methods=['GET'])
@bp.route('/merchant/stats/business', methods=['GET'])
@require_merchant_auth
def merchant_business_stats():
    permissions = session.get('permissions') or []
    show_hidden = session.get('is_agent') and 'show_hidden' in permissions
    logic_filter = '' if show_hidden else " {hide_filter}"
    hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
    hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
    
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        filter_merchant_id = request.args.get('merchant_id', type=int)
        if filter_merchant_id and session.get('is_agent'):
            cursor.execute('SELECT id FROM merchants WHERE id=%s AND agent_id=%s', (filter_merchant_id, session.get('agent_id')))
            if cursor.fetchone():
                mfilter = 'l.merchant_id = %s'
                mparams = [filter_merchant_id]
                merchant_id = filter_merchant_id
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
        cursor.execute(f'SELECT COUNT(*) as total_orders, SUM(CASE WHEN o.status = 1 THEN 1 ELSE 0 END) as active_orders, SUM(CASE WHEN o.status = 2 THEN 1 ELSE 0 END) as completed_orders FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} AND o.status NOT IN (1, 5)  {hide_filter}', params)
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
        cursor.execute(f'SELECT COUNT(*) as total_slots, SUM(CASE WHEN cs.status = 2 THEN 1 ELSE 0 END) as used_slots, SUM(CASE WHEN cs.status = 1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id = cs.id AND o2.status = 2) THEN 1 ELSE 0 END) as free_slots FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {slot_where_sql}', slot_params)
        slot_stats = cursor.fetchone()
        # ===== 新增统计字段 =====
        # 退款统计
        cursor.execute(f"SELECT COUNT(*) as refund_orders, COALESCE(SUM(o.refund_amount),0) as refund_amount FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} AND o.status = 4  {hide_filter}", params)
        rfs = cursor.fetchone()
        # 有无收费网点
        cursor.execute(f"SELECT COUNT(*) as cnt FROM locations l WHERE {mfilter} AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')", mparams)
        has_charge = cursor.fetchone()[0] > 0
        # 商家广告费
        cursor.execute(f"SELECT COALESCE(SUM(m.ad_fee_per_order),0) as ad_fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id JOIN merchants m ON l.merchant_id = m.id WHERE {where_sql} AND o.status NOT IN (1, 5)  {hide_filter}", params)
        ad_fee_row = cursor.fetchone()
        # 每日趋势图（一次 GROUP BY 取完，避免逐日循环查库）
        from datetime import datetime as _dt, timedelta as _td
        chart = []
        if start_date and end_date:
            cursor.execute(f"SELECT DATE(o.created_at)::text as d, COUNT(*) as c FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} AND o.status NOT IN (1, 5)  {hide_filter} GROUP BY DATE(o.created_at)", params)
            count_map = {r['d']: r['c'] for r in cursor.fetchall()}
            d1 = _dt.strptime(start_date, '%Y-%m-%d')
            d2 = _dt.strptime(end_date.split()[0], '%Y-%m-%d')
            day_count = (d2 - d1).days + 1
            for i in range(day_count):
                d = (d1 + _td(days=i)).strftime('%Y-%m-%d')
                chart.append({"date": d, "orders": count_map.get(d, 0) or 0})
        else:
            cursor.execute(f"SELECT DATE(o.created_at)::text as d, COUNT(*) as c FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {mfilter} AND DATE(o.created_at) >= %s AND o.status NOT IN (1, 5)  {hide_filter} GROUP BY DATE(o.created_at)", mparams + [(_dt.now() - _td(days=29)).strftime('%Y-%m-%d')])
            count_map = {r['d']: r['c'] for r in cursor.fetchall()}
            for i in range(29, -1, -1):
                d = (_dt.now() - _td(days=i)).strftime('%Y-%m-%d')
                chart.append({"date": d, "orders": count_map.get(d, 0) or 0})
        # 收益金额（收费模式下的手续费，不含保证金）
        cursor.execute(f"SELECT COALESCE(SUM(GREATEST(o.deposit_amount - COALESCE(o.refund_amount,0), 0)), 0) as fee FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} AND o.status = 4 AND (l.charge_mode IS NOT NULL AND l.charge_mode != '' AND l.charge_mode != 'free')  {hide_filter}", params)
        fee_row = cursor.fetchone()
        conn.close()
        total_orders = order_stats['total_orders'] or 0
        # merge historical data
        try:
            hist_parts = [mfilter]
            hist_params = list(mparams)
            if start_date:
                hist_parts.append('date >= %s')
                hist_params.append(start_date)
            if end_date:
                hist_parts.append('date <= %s')
                hist_params.append(end_date)
            if location_id:
                hist_parts.append('h.location_id = %s')
                hist_params.append(location_id)
            hist_where = ' AND '.join(hist_parts)
            cursor.execute(f'SELECT COALESCE(SUM(visible_count),0) as h FROM historical_order_counts h JOIN locations l ON h.location_id = l.id WHERE {hist_where}', hist_params)
            total_orders = (total_orders or 0) + (cursor.fetchone()['h'] or 0)
        except Exception:
            pass

        is_agent = bool(session.get('is_agent'))
        result = {
            'total_orders': total_orders,
            'active_orders': order_stats['active_orders'] or 0,
            'completed_orders': order_stats['completed_orders'] or 0,
            'total_income': round(float(fee_row['fee'] or 0), 2),
            'total_refund_orders': rfs['refund_orders'] or 0,
            'total_refund_amount': round(float(rfs['refund_amount'] or 0), 2),
            'merchant_ad_fee': round(float(ad_fee_row['ad_fee'] or 0), 2),
            'has_charge_location': has_charge,
            'chart': chart,
            'is_agent': is_agent,
            'total_recharge': round(float(income_stats['total_income'] or 0), 2),
            'total_withdraw': round(float(deposit_stats['deposit_refunded'] or 0), 2),
            'show_deposit_fields': 'show_deposit_fields' in permissions
        }
        if is_agent:
            result['deposit_collected'] = round(float(deposit_stats['deposit_collected'] or 0), 2)
            result['deposit_refunded'] = round(float(deposit_stats['deposit_refunded'] or 0), 2)
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
        cursor.execute(f'SELECT p.*, o.order_no, o.user_phone, c.cabinet_code, l.name as location_name FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE {where_sql} ORDER BY p.created_at DESC LIMIT %s OFFSET %s', params + [limit, offset])
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
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        conn = get_db()
        cursor = conn.cursor()
        # Verify payment belongs to this merchant's orders
        if merchant_id:
            cursor.execute(f'SELECT p.*, o.user_phone, o.openid, o.unionid, o.mp_openid, o.user_id FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE p.id = %s AND {mfilter} AND p.type = 2', (payment_id, *mparams))
        else:
            cursor.execute(f'SELECT p.*, o.user_phone, o.openid, o.unionid, o.mp_openid, o.user_id FROM payments p JOIN orders o ON p.order_id = o.id JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE p.id = %s AND {mfilter} AND p.type = 2', (payment_id, *mparams))
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
            _m_ub = find_user_balance_row(cursor, phone=payment['user_phone'],
                                          openid=payment.get('openid', ''),
                                          mp_openid=payment.get('mp_openid', ''),
                                          unionid=payment.get('unionid', ''),
                                          user_id=payment.get('user_id') or 0)
            if _m_ub and float(_m_ub.get('balance') or 0) < amount_val:
                conn.rollback()
                conn.close()
                return json_response(message='用户余额不足，无法扣除', code=400)
            upsert_user_balance_row(cursor, phone=payment['user_phone'],
                                    openid=payment.get('openid', ''),
                                    unionid=payment.get('unionid', ''),
                                    mp_openid=payment.get('mp_openid', ''),
                                    balance=-amount_val, total_withdrawn=amount_val,
                                    user_id=payment.get('user_id') or 0)
        conn.commit()
        conn.close()
        return json_response(message='押金退还成功')
    except Exception as e:
        logger.error(f'[merchant_refund_deposit] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/balance', methods=['GET'])
@require_merchant_auth
def merchant_balance():
    """查询商户余额概览（含上月广告收入）"""
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
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
        # 已提现总额
        if merchant_id:
            cursor.execute('SELECT COALESCE(SUM(amount), 0) as withdrawn FROM withdrawal_records WHERE user_phone = (SELECT contact_phone FROM merchants WHERE id=%s)', (merchant_id,))
        else:
            cursor.execute('SELECT COALESCE(SUM(wr.amount), 0) as withdrawn FROM withdrawal_records wr WHERE wr.user_phone IN (SELECT m.contact_phone FROM merchants m WHERE m.agent_id=%s)', mparams)
        withdrawn = cursor.fetchone()['withdrawn']
        # 上月广告收入（不累计）
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now()
        if now.month == 1:
            last_month_start = _dt(now.year - 1, 12, 1)
        else:
            last_month_start = _dt(now.year, now.month - 1, 1)
        last_month_end = _dt(now.year, now.month, 1) - _td(days=1)
        last_month_start_str = last_month_start.strftime('%Y-%m-%d')
        last_month_end_str = last_month_end.strftime('%Y-%m-%d')
        cursor.execute(f"SELECT COALESCE(SUM(m.commission_per_order), 0) as cashback FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id JOIN merchants m ON l.merchant_id = m.id WHERE {mfilter} AND o.status NOT IN (1, 5)  {hide_filter} AND DATE(o.created_at) >= %s AND DATE(o.created_at) <= %s", (*mparams, last_month_start_str, last_month_end_str))
        cashback = cursor.fetchone()['cashback']
        conn.close()
        is_agent = bool(session.get('is_agent'))
        is_employee = bool(session.get('is_employee'))
        return json_response({
            'total_income': round(float(total_income or 0), 2),
            'deposit_held': round(float(deposit_held or 0), 2),
            'deposit_refunded': round(float(deposit_refunded or 0), 2),
            'available': round(float(total_income or 0), 2),  # 可提现金额 = 使用费收入
            'is_agent': is_agent,
            'is_employee': is_employee,
            'total_recharge': 0,
            'total_withdraw': round(float(withdrawn or 0), 2),
            # 商户小程序"我的"页面卡片数据
            'withdrawn': 0.00,      # 已提现（暂不展示实际数据）
            'reserve': 0,     # 储备金（暂不显示实际数据）
            'cashback': round(float(cashback or 0), 2) if not is_employee else 0,  # 上月广告收入（雇员不显示）
            'text_labels': {
                'cashback': '广告收入'
            }
        })
    except Exception as e:
        logger.error(f'[merchant_balance] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/merchant/withdrawals', methods=['GET'])
@require_merchant_auth
def merchant_withdrawals():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
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
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT da.*, l.name as location_name, c.name as cabinet_name FROM device_alerts da LEFT JOIN cabinets c ON da.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id WHERE {mfilter} ORDER BY da.created_at DESC LIMIT 50', mparams)
        rows = cursor.fetchall()
        conn.close()
        return json_response({'list': [dict(r) for r in rows]})
    except Exception as e:
        logger.error(f'[merchant_alerts] {e}')
        return json_response(message=str(e), code=500)





@bp.route('/merchant/device-status', methods=['GET'])
@require_merchant_auth
def merchant_device_status():
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        conn = get_db()
        cursor = conn.cursor()
        sql = "SELECT c.id, c.name, c.cabinet_code, c.mainboard_device_id, c.last_heartbeat, l.name as location_name FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE " + mfilter + " AND (c.last_heartbeat IS NULL OR c.last_heartbeat < NOW() - INTERVAL '120 seconds') ORDER BY l.name, c.name"
        cursor.execute(sql, mparams)
        rows = cursor.fetchall()
        conn.close()
        result = [dict(r) for r in rows]
        return json_response({'list': result})
    except Exception as e:
        from helpers import logger
        logger.error('[merchant_device_status] %s', str(e))
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
        cursor.execute('SELECT m.* FROM merchants m WHERE m.agent_id=%s ORDER BY m.created_at DESC', (agent_id,))
        rows = cursor.fetchall()
        hide_filter = " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        merchants = [dict(r) for r in rows]
        for m in merchants:
            cursor.execute(f'''SELECT l.id, l.name, l.contact_phone,
                (SELECT COUNT(*) FROM orders o JOIN cabinets c ON o.cabinet_id=c.id
                  WHERE c.location_id=l.id AND o.status IN (2,4) {hide_filter}) as order_count,
                (SELECT COUNT(*) FROM orders o JOIN cabinets c ON o.cabinet_id=c.id
                  WHERE c.location_id=l.id AND o.status IN (2,4)) as total_order_count
                FROM locations l WHERE l.merchant_id=%s ORDER BY l.created_at DESC''', (m['id'],))
            m['locations'] = [dict(r) for r in cursor.fetchall()]
            m['location_count'] = len(m['locations'])
        conn.close()
        return json_response({'list': merchants})
    except Exception as e:
        logger.error(f'[merchant_my_merchants] {e}')
        return json_response({'list': []})



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


@bp.route('/merchant/cabinets/<int:cabinet_id>/slots/<int:slot_id>/physical-status', methods=['GET'])
@require_merchant_auth
def merchant_query_physical_lock_status(cabinet_id, slot_id):
    """查询柜门的物理锁状态（通过WS代理发送状态查询指令到设备）"""
    try:
        merchant_id, mfilter, mparams = _get_merchant_filter()
        permissions = session.get('permissions') or []
        show_hidden = session.get('is_agent') and 'show_hidden' in permissions
        hide_filter = '' if show_hidden else " AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)"
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f'SELECT c.id, c.mainboard_device_id, cs.slot_number, cs.board_no, cs.lock_no FROM cabinets c JOIN locations l ON c.location_id = l.id JOIN cabinet_slots cs ON cs.cabinet_id = c.id WHERE c.id = %s AND cs.id = %s AND {mfilter}', (cabinet_id, slot_id, *mparams))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return json_response(message='柜格不存在或无权访问', code=404)
        
        did = str(row['mainboard_device_id'])
        board_no = row.get('board_no') or 1
        lock_no = row.get('lock_no') or row['slot_number']
        
        # 通过WS代理发送状态查询指令到设备
        from helpers import send_open_lock
        import urllib.request as _req
        import json as _json
        import uuid
        
        request_id = str(uuid.uuid4())[:8]
        query_cmd = {
            'type': 'query_door_status',
            'request_id': request_id,
            'device_id': did,
            'board_no': board_no,
            'lock_no': lock_no,
            'protocol': 'YBM'
        }
        
        try:
            _body = _json.dumps({'device_id': did, 'command': query_cmd}).encode()
            _req.urlopen('http://127.0.0.1:5004/send', data=_body, timeout=3)
        except Exception as e:
            logger.error(f'[physical_status] WS proxy send failed: {e}')
            return json_response(message='设备可能离线，无法查询物理状态', code=502)
        
        return json_response(message='状态查询指令已发送至设备，请稍后查看结果', data={
            'device_id': did,
            'board_no': board_no,
            'lock_no': lock_no,
            'request_id': request_id,
            'query_sent': True
        })
    except Exception as e:
        logger.error(f'[physical_status] {e}')
        return json_response(message=str(e), code=500)
