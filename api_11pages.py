# -*- coding: utf-8 -*-
"""
11个功能页面的API端点
追加到 routes/admin_v2.py 末尾
"""
import os
import sqlite3

# ============ 建表 ============

def _ensure_tables():
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'locker.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS companies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        credit_code TEXT,
        contact_person TEXT,
        contact_phone TEXT,
        address TEXT,
        status INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS blacklist(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        reason TEXT,
        cabinet_id INTEGER,
        operator TEXT,
        status INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alarms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        cabinet_id INTEGER,
        device_id TEXT,
        content TEXT,
        level INTEGER DEFAULT 1,
        status INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW(),
        resolved_at DATETIME,
        resolver TEXT
    )""")
    conn.commit()
    conn.close()

_ensure_tables()

# ============ 1. 结算管理 ============

@bp.route('/settlement/list', methods=['GET'])
@require_auth
def settlement_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        location_id = request.args.get('location_id', '')
        date_start = request.args.get('date_start', '')
        date_end = request.args.get('date_end', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT o.id, o.order_no, o.user_phone, o.cabinet_id, o.deposit_amount,
                o.status, o.store_time, o.retrieve_time, o.cabinet_name,
                c.location_id, l.name as location_name
                FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
                LEFT JOIN locations l ON c.location_id=l.id
                WHERE 1=1"""
        params = []
        if location_id:
            sql += " AND c.location_id=%s"
            params.append(location_id)
        if date_start:
            sql += " AND o.store_time>=%s"
            params.append(date_start)
        if date_end:
            sql += " AND o.store_time<=%s"
            params.append(date_end)
        sql += " ORDER BY o.id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id WHERE 1=1"
        count_params = []
        if location_id:
            count_sql += " AND c.location_id=%s"
            count_params.append(location_id)
        if date_start:
            count_sql += " AND o.store_time>=%s"
            count_params.append(date_start)
        if date_end:
            count_sql += " AND o.store_time<=%s"
            count_params.append(date_end)
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page, 'size': size})
    except Exception as e:
        logger.error(f'[settlement_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/settlement/stats', methods=['GET'])
@require_auth
def settlement_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as total_orders, COALESCE(SUM(deposit_amount),0) as total_deposit FROM orders")
        row = c.fetchone()
        c.execute("SELECT COUNT(*) as active_orders FROM orders WHERE status=1")
        active = c.fetchone()[0]
        c.execute("SELECT COUNT(*) as completed FROM orders WHERE status=2")
        completed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) as refunded FROM orders WHERE status=3")
        refunded = c.fetchone()[0]
        conn.close()
        return json_response(data={
            'total_orders': row['total_orders'],
            'total_deposit': row['total_deposit'],
            'active_orders': active,
            'completed': completed,
            'refunded': refunded
        })
    except Exception as e:
        logger.error(f'[settlement_stats] {e}')
        return json_response(message=str(e), code=500)

# ============ 2. 提现管理 ============

@bp.route('/withdrawals/list', methods=['GET'])
@require_auth
def withdrawals_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        status = request.args.get('status', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = "SELECT * FROM withdrawal_records WHERE 1=1"
        params = []
        if status != '':
            sql += " AND status=%s"
            params.append(int(status))
        sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM withdrawal_records WHERE 1=1"
        count_params = []
        if status != '':
            count_sql += " AND status=%s"
            count_params.append(int(status))
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page, 'size': size})
    except Exception as e:
        logger.error(f'[withdrawals_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/withdrawals/approve', methods=['POST'])
@require_auth
def withdrawals_approve():
    try:
        data = request.get_json()
        wid = data.get('id')
        action = data.get('action')  # approve/reject
        if not wid or not action:
            return json_response(message='参数缺失', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM withdrawal_records WHERE id=%s", (wid,))
        record = c.fetchone()
        if not record:
            conn.close()
            return json_response(message='记录不存在', code=404)
        if record['status'] != 0:
            conn.close()
            return json_response(message='该记录已处理', code=400)
        new_status = 1 if action == 'approve' else 2
        c.execute("UPDATE withdrawal_records SET status=%s, approve_time=NOW(), approver=%s WHERE id=%s",
                  (new_status, session.get('admin_user', 'admin'), wid))
        conn.commit()
        conn.close()
        return json_response(message='处理成功')
    except Exception as e:
        logger.error(f'[withdrawals_approve] {e}')
        return json_response(message=str(e), code=500)

# ============ 3. 平台流水 ============

@bp.route('/platform-flow/list', methods=['GET'])
@require_auth
def platform_flow_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        flow_type = request.args.get('type', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT p.id, p.order_id, p.type, p.amount, p.transaction_id, p.status, p.created_at,
                p.refund_transaction_id, o.order_no, o.user_phone
                FROM payments p LEFT JOIN orders o ON p.order_id=o.id WHERE 1=1"""
        params = []
        if flow_type:
            sql += " AND p.type=%s"
            params.append(int(flow_type))
        sql += " ORDER BY p.id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM payments p WHERE 1=1"
        count_params = []
        if flow_type:
            count_sql += " AND p.type=%s"
            count_params.append(int(flow_type))
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(CASE WHEN type=1 THEN amount END),0) as total_deposit, COALESCE(SUM(CASE WHEN type=2 THEN amount END),0) as total_refund FROM payments WHERE status=1")
        summary = c.fetchone()
        conn.close()
        return json_response(data={
            'list': rows, 'total': total, 'page': page, 'size': size,
            'total_deposit': summary['total_deposit'],
            'total_refund': summary['total_refund']
        })
    except Exception as e:
        logger.error(f'[platform_flow_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 4. 资金流水 ============

@bp.route('/fund-flow/list', methods=['GET'])
@require_auth
def fund_flow_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT ub.phone, ub.balance, ub.total_deposited, ub.total_withdrawn, ub.first_use_time,
                (SELECT COUNT(*) FROM orders WHERE user_phone=ub.phone) as order_count
                FROM user_balances ub ORDER BY ub.id DESC""")
        rows = [dict(r) for r in c.fetchall()]
        c.execute("SELECT COUNT(*) as total_users, COALESCE(SUM(balance),0) as total_balance FROM user_balances")
        summary = c.fetchone()
        conn.close()
        return json_response(data={'list': rows, 'total_users': summary['total_users'], 'total_balance': summary['total_balance']})
    except Exception as e:
        logger.error(f'[fund_flow_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 5. 综合查询 ============

@bp.route('/query-all/list', methods=['GET'])
@require_auth
def query_all_list():
    try:
        keyword = request.args.get('keyword', '')
        query_type = request.args.get('type', 'order')
        conn = get_db()
        c = conn.cursor()
        results = []
        if not keyword:
            return json_response(data={'list': [], 'total': 0})
        if query_type == 'order':
            c.execute("""SELECT o.*, c.cabinet_code, c.name as cabinet_name, l.name as location_name
                    FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
                    LEFT JOIN locations l ON c.location_id=l.id
                    WHERE o.order_no LIKE %s OR o.user_phone LIKE %s ORDER BY o.id DESC LIMIT 50""",
                    (f'%{keyword}%', f'%{keyword}%'))
            results = [dict(r) for r in c.fetchall()]
        elif query_type == 'phone':
            c.execute("""SELECT ub.*, (SELECT COUNT(*) FROM orders WHERE user_phone=ub.phone) as order_count
                    FROM user_balances ub WHERE ub.phone LIKE %s LIMIT 50""", (f'%{keyword}%',))
            results = [dict(r) for r in c.fetchall()]
        elif query_type == 'cabinet':
            c.execute("""SELECT c.*, l.name as location_name
                    FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id
                    WHERE c.cabinet_code LIKE %s OR c.name LIKE %s OR c.mainboard_device_id LIKE %s LIMIT 50""",
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'))
            results = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': results, 'total': len(results)})
    except Exception as e:
        logger.error(f'[query_all_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 6. 公司管理 ============

@bp.route('/companies/list', methods=['GET'])
@require_auth
def companies_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM companies ORDER BY id DESC")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[companies_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/companies/save', methods=['POST'])
@require_auth
def companies_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        cid = data.get('id')
        if cid:
            c.execute("UPDATE companies SET name=%s, credit_code=%s, contact_person=%s, contact_phone=%s, address=%s, status=%s WHERE id=%s",
                      (data.get('name',''), data.get('credit_code',''), data.get('contact_person',''),
                       data.get('contact_phone',''), data.get('address',''), data.get('status',1), cid))
        else:
            c.execute("INSERT INTO companies(name, credit_code, contact_person, contact_phone, address, status) VALUES(%s,%s,%s,%s,%s,%s)",
                      (data.get('name',''), data.get('credit_code',''), data.get('contact_person',''),
                       data.get('contact_phone',''), data.get('address',''), data.get('status',1)))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[companies_save] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/companies/delete', methods=['POST'])
@require_auth
def companies_delete():
    try:
        data = request.get_json()
        cid = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM companies WHERE id=%s", (cid,))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[companies_delete] {e}')
        return json_response(message=str(e), code=500)

# ============ 7. 黑名单管理 ============

@bp.route('/blacklist/list', methods=['GET'])
@require_auth
def blacklist_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT b.*, c.cabinet_code, c.name as cabinet_name
                FROM blacklist b LEFT JOIN cabinets c ON b.cabinet_id=c.id
                ORDER BY b.id DESC""")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[blacklist_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/blacklist/save', methods=['POST'])
@require_auth
def blacklist_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        bid = data.get('id')
        if bid:
            c.execute("UPDATE blacklist SET phone=%s, reason=%s, cabinet_id=%s, status=%s WHERE id=%s",
                      (data.get('phone',''), data.get('reason',''), data.get('cabinet_id'), data.get('status',1), bid))
        else:
            c.execute("INSERT INTO blacklist(phone, reason, cabinet_id, operator, status) VALUES(%s,%s,%s,%s,%s)",
                      (data.get('phone',''), data.get('reason',''), data.get('cabinet_id'),
                       session.get('admin_user','admin'), data.get('status',1)))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[blacklist_save] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/blacklist/delete', methods=['POST'])
@require_auth
def blacklist_delete():
    try:
        data = request.get_json()
        bid = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM blacklist WHERE id=%s", (bid,))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[blacklist_delete] {e}')
        return json_response(message=str(e), code=500)

# ============ 8. 报警记录 ============

@bp.route('/alarms/list', methods=['GET'])
@require_auth
def alarms_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        status = request.args.get('status', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT a.*, c.cabinet_code, c.name as cabinet_name
                FROM alarms a LEFT JOIN cabinets c ON a.cabinet_id=c.id WHERE 1=1"""
        params = []
        if status != '':
            sql += " AND a.status=%s"
            params.append(int(status))
        sql += " ORDER BY a.id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM alarms a WHERE 1=1"
        count_params = []
        if status != '':
            count_sql += " AND a.status=%s"
            count_params.append(int(status))
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page, 'size': size})
    except Exception as e:
        logger.error(f'[alarms_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/alarms/resolve', methods=['POST'])
@require_auth
def alarms_resolve():
    try:
        data = request.get_json()
        aid = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE alarms SET status=1, resolved_at=NOW(), resolver=%s WHERE id=%s",
                  (session.get('admin_user','admin'), aid))
        conn.commit()
        conn.close()
        return json_response(message='处理成功')
    except Exception as e:
        logger.error(f'[alarms_resolve] {e}')
        return json_response(message=str(e), code=500)

# ============ 9. 位置报警 ============

@bp.route('/location-alarms/list', methods=['GET'])
@require_auth
def location_alarms_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT c.id, c.cabinet_code, c.name, c.location_id, l.name as location_name,
                c.last_heartbeat, c.status, c.mainboard_device_id,
                (SELECT COUNT(*) FROM alarms WHERE cabinet_id=c.id AND status=0) as alarm_count,
                (SELECT COUNT(*) FROM orders WHERE cabinet_id=c.id AND status=1) as active_orders
                FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id
                WHERE c.location_id IS NOT NULL ORDER BY c.id""")
        rows = [dict(r) for r in c.fetchall()]
        import datetime as dt_mod
        for row in rows:
            if row.get('last_heartbeat'):
                try:
                    hb = row['last_heartbeat']
                    if isinstance(hb, str):
                        hb = dt_mod.datetime.strptime(hb, '%Y-%m-%d %H:%M:%S')
                    diff = (dt_mod.datetime.utcnow() - hb).total_seconds()
                    row['offline'] = diff > 300
                    row['heartbeat_age_min'] = int(diff / 60)
                except:
                    row['offline'] = True
                    row['heartbeat_age_min'] = 999
            else:
                row['offline'] = True
                row['heartbeat_age_min'] = 0
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[location_alarms_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 10. 角色管理 ============

@bp.route('/roles/list', methods=['GET'])
@require_auth
def roles_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, username, role, created_at FROM admin_users ORDER BY id")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[roles_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/roles/save', methods=['POST'])
@require_auth
def roles_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        uid = data.get('id')
        username = data.get('username','')
        role = data.get('role','viewer')
        password = data.get('password','')
        if uid:
            if password:
                c.execute("UPDATE admin_users SET username=%s, role=%s, password_hash=%s WHERE id=%s",
                          (username, role, generate_password_hash(password), uid))
            else:
                c.execute("UPDATE admin_users SET username=%s, role=%s WHERE id=%s", (username, role, uid))
        else:
            if not password:
                return json_response(message='新用户必须设置密码', code=400)
            c.execute("INSERT INTO admin_users(username, password_hash, role) VALUES(%s,%s,%s)",
                      (username, generate_password_hash(password), role))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[roles_save] {e}')
        return json_response(message=str(e), code=500)

# ============ 11. 数据重置 ============

@bp.route('/data-reset/stats', methods=['GET'])
@require_auth
def data_reset_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        stats = {}
        for table in ['orders','payments','withdrawal_records','complaints','device_logs','storage_records','door_records','pending_lock_cmds']:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = c.fetchone()[0]
        conn.close()
        return json_response(data=stats)
    except Exception as e:
        logger.error(f'[data_reset_stats] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/data-reset/exec', methods=['POST'])
@require_auth
def data_reset_exec():
    try:
        data = request.get_json()
        tables = data.get('tables', [])
        if not tables:
            return json_response(message='请选择要清理的表', code=400)
        conn = get_db()
        c = conn.cursor()
        allowed = ['orders','payments','withdrawal_records','complaints','device_logs','storage_records','door_records','pending_lock_cmds']
        for t in tables:
            if t in allowed:
                c.execute(f"DELETE FROM {t}")
        conn.commit()
        conn.close()
        return json_response(message='清理完成')
    except Exception as e:
        logger.error(f'[data_reset_exec] {e}')
        return json_response(message=str(e), code=500)
