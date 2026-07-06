# -*- coding: utf-8 -*-
"""fix_p0_api.py - P0-3系统设置管理API + P0-4柜组管理API
追加到admin_v2.py末尾
"""

API_CODE = '''
# ==================== P0-3: 系统设置管理 ====================

@bp.route('/settings', methods=['GET'])
def get_settings():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        rows = c.execute("SELECT key, value, description FROM system_settings").fetchall()
        settings = {row['key']: {'value': row['value'], 'desc': row['description']} for row in rows}
        conn.close()
        return jsonify({'code': 200, 'data': settings})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/settings/save', methods=['POST'])
def save_settings():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for key, value in data.items():
            existing = c.execute("SELECT id FROM system_settings WHERE key=%s", (key,)).fetchone()
            if existing:
                c.execute("UPDATE system_settings SET value=%s WHERE key=%s", (str(value), key))
            else:
                c.execute("INSERT INTO system_settings (key, value, description) VALUES (%s, %s, '')", (key, str(value)))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/settings/order-visibility', methods=['GET'])
def get_order_visibility():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        hide_rate = c.execute("SELECT value FROM system_settings WHERE key='order_hide_rate'").fetchone()
        whitelist = c.execute("SELECT value FROM system_settings WHERE key='order_hide_whitelist'").fetchone()
        conn.close()
        return jsonify({'code': 200, 'data': {
            'order_hide_rate': int(hide_rate['value']) if hide_rate else 0,
            'order_hide_whitelist': whitelist['value'] if whitelist else ''
        }})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/settings/order-visibility/save', methods=['POST'])
def save_order_visibility():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for key in ['order_hide_rate', 'order_hide_whitelist']:
            val = data.get(key, '')
            existing = c.execute("SELECT id FROM system_settings WHERE key=%s", (key,)).fetchone()
            if existing:
                c.execute("UPDATE system_settings SET value=%s WHERE key=%s", (str(val), key))
            else:
                c.execute("INSERT INTO system_settings (key, value, description) VALUES (%s, %s, '')", (key, str(val)))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/settings/duplicate-filter', methods=['GET'])
def get_duplicate_filter():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        enabled = c.execute("SELECT value FROM system_settings WHERE key='duplicate_filter_enabled'").fetchone()
        days = c.execute("SELECT value FROM system_settings WHERE key='duplicate_days'").fetchone()
        limit = c.execute("SELECT value FROM system_settings WHERE key='duplicate_limit'").fetchone()
        conn.close()
        return jsonify({'code': 200, 'data': {
            'duplicate_filter_enabled': int(enabled['value']) if enabled and enabled['value'] not in ('false','0') else 0,
            'duplicate_days': int(days['value']) if days else 7,
            'duplicate_limit': int(limit['value']) if limit else 5
        }})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/settings/duplicate-filter/save', methods=['POST'])
def save_duplicate_filter():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for key in ['duplicate_filter_enabled', 'duplicate_days', 'duplicate_limit']:
            val = data.get(key, '')
            existing = c.execute("SELECT id FROM system_settings WHERE key=%s", (key,)).fetchone()
            if existing:
                c.execute("UPDATE system_settings SET value=%s WHERE key=%s", (str(val), key))
            else:
                c.execute("INSERT INTO system_settings (key, value, description) VALUES (%s, %s, '')", (key, str(val)))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})


# ==================== P0-4: 柜组管理 ====================

@bp.route('/admin/cabinet-groups', methods=['GET'])
def cabinet_groups_list():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        keyword = request.args.get('keyword', '')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        where = "WHERE 1=1"
        params = []
        if keyword:
            where += " AND (group_code LIKE %s OR name LIKE %s)"
            params += [f'%{keyword}%', f'%{keyword}%']
        total = c.execute(f"SELECT COUNT(*) FROM cabinet_groups {where}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM cabinet_groups {where} ORDER BY id DESC LIMIT %s OFFSET %s", params + [limit, (page-1)*limit]).fetchall()
        groups = []
        for r in rows:
            g = dict(r)
            cabinet_count = c.execute("SELECT COUNT(*) FROM cabinets WHERE group_id=%s", (g['id'],)).fetchone()[0]
            g['cabinet_count'] = cabinet_count
            groups.append(g)
        conn.close()
        return jsonify({'code': 200, 'data': {'list': groups, 'total': total}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/admin/cabinet-groups/save', methods=['POST'])
def cabinet_groups_save():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if data.get('id'):
            c.execute("UPDATE cabinet_groups SET group_code=%s, name=%s, location_id=%s WHERE id=%s",
                      (data.get('group_code',''), data.get('name',''), data.get('location_id'), data['id']))
        else:
            c.execute("INSERT INTO cabinet_groups (location_id, group_code, name, status, created_at) VALUES (%s, %s, %s, 1, NOW())",
                      (data.get('location_id'), data.get('group_code',''), data.get('name','')))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/admin/cabinet-groups/delete', methods=['POST'])
def cabinet_groups_delete():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM cabinet_groups WHERE id=%s", (data['id'],))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/admin/cabinet-groups/cabinets', methods=['GET'])
def cabinet_groups_cabinets():
    try:
        group_id = request.args.get('group_id')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        rows = c.execute("SELECT * FROM cabinets WHERE group_id=%s", (group_id,)).fetchall()
        conn.close()
        return jsonify({'code': 200, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/admin/cabinet-groups/by-code', methods=['GET'])
def cabinet_groups_by_code():
    try:
        code = request.args.get('code')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM cabinet_groups WHERE group_code=%s", (code,)).fetchone()
        if not row:
            return jsonify({'code': 404, 'message': '柜组不存在'})
        g = dict(row)
        cabinets = c.execute("SELECT * FROM cabinets WHERE group_id=%s", (g['id'],)).fetchall()
        g['cabinets'] = [dict(c2) for c2 in cabinets]
        conn.close()
        return jsonify({'code': 200, 'data': g})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
'''

# 读取现有文件
with open('/home/ubuntu/smart-locker/routes/admin_v2.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 检查是否已添加
if '# P0-3: 系统设置管理' in content:
    print('API code already exists, skip')
else:
    # 追加API代码
    with open('/home/ubuntu/smart-locker/routes/admin_v2.py', 'a', encoding='utf-8') as f:
        f.write(API_CODE)
    print('API code appended to admin_v2.py')

# 同时在_ensure_tables中添加after_sales表创建
old_ensure = 'def _ensure_tables():'
if old_ensure in content and 'after_sales' not in content[content.find(old_ensure):content.find(old_ensure)+500]:
    # 在_ensure_tables函数中添加after_sales
    old_alarm_create = 'c.execute("""CREATE TABLE IF NOT EXISTS alarms('
    new_after_sales = '''c.execute("""CREATE TABLE IF NOT EXISTS after_sales(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_no TEXT UNIQUE,
        cabinet_id INTEGER,
        location_id INTEGER,
        device_id TEXT,
        fault_type TEXT,
        description TEXT,
        status TEXT DEFAULT 'pending',
        handler TEXT,
        handler_note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alarms('''
    
    with open('/home/ubuntu/smart-locker/routes/admin_v2.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace(old_alarm_create, new_after_sales)
    with open('/home/ubuntu/smart-locker/routes/admin_v2.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('after_sales table creation added to _ensure_tables()')
else:
    print('_ensure_tables already has after_sales or pattern not found')

# 验证Python语法
import py_compile
try:
    py_compile.compile('/home/ubuntu/smart-locker/routes/admin_v2.py', doraise=True)
    print('Python syntax OK')
except py_compile.PyCompileError as e:
    print(f'Python syntax ERROR: {e}')
