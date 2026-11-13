#!/usr/bin/env python3
"""
迁移脚本：将 user_balances + phone_openids 中的用户合并到 app_users，
并回写 user_id 到三张表。

合并规则：
1. 按 unionid 合并（同一 unionid 的不同 openid/mp_openid/phone 归同一用户）
2. 无 unionid 时，按 phone 合并
3. 既无 unionid 也无 phone 的，按 openid/mp_openid 各自独立创建
"""
import psycopg2
import psycopg2.extras

DB_CONF = {
    'host': '127.0.0.1',
    'port': 6432,
    'user': 'locker_admin',
    'password': 'locker_pass_2024',
    'dbname': 'smart_locker',
}

def get_conn():
    conn = psycopg2.connect(**DB_CONF)
    conn.autocommit = False
    return conn

def collect_all_identities(conn):
    """收集所有身份标识，返回 list of dict"""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    # 从 user_balances 收集
    cur.execute("""
        SELECT DISTINCT
            COALESCE(openid, '') as openid,
            COALESCE(mp_openid, '') as mp_openid,
            COALESCE(phone, '') as phone,
            COALESCE(unionid, '') as unionid,
            COALESCE(wechat_name, '') as wechat_name
        FROM user_balances
        WHERE openid != '' OR mp_openid != '' OR phone != ''
    """)
    balances_rows = [dict(r) for r in cur.fetchall()]
    
    # 从 phone_openids 收集
    cur.execute("""
        SELECT DISTINCT
            COALESCE(openid, '') as openid,
            COALESCE(mp_openid, '') as mp_openid,
            COALESCE(phone, '') as phone
        FROM phone_openids
        WHERE openid != '' OR mp_openid != '' OR phone != ''
    """)
    po_rows = [dict(r) for r in cur.fetchall()]
    
    cur.close()
    return balances_rows, po_rows

def merge_users(balances_rows, po_rows):
    """
    合并逻辑：用 Union-Find 思想，把所有标识按 unionid / phone 聚类。
    返回 list of merged user dicts。
    """
    # 收集所有记录，以 (openid, mp_openid) 为唯一键合并
    all_records = {}  # key: (openid, mp_openid) -> merged info
    
    for r in balances_rows:
        key = (r['openid'], r['mp_openid'])
        if key not in all_records:
            all_records[key] = {'openid': r['openid'], 'mp_openid': r['mp_openid'],
                                'phones': set(), 'unionids': set(), 'wechat_names': set()}
        if r['phone']:
            all_records[key]['phones'].add(r['phone'])
        if r['unionid']:
            all_records[key]['unionids'].add(r['unionid'])
        if r['wechat_name']:
            all_records[key]['wechat_names'].add(r['wechat_name'])
    
    for r in po_rows:
        key = (r['openid'], r['mp_openid'])
        if key not in all_records:
            all_records[key] = {'openid': r['openid'], 'mp_openid': r['mp_openid'],
                                'phones': set(), 'unionids': set(), 'wechat_names': set()}
        if r['phone']:
            all_records[key]['phones'].add(r['phone'])
    
    # 用 Union-Find 按 unionid 和 phone 聚类
    keys = list(all_records.keys())
    parent = {k: k for k in keys}
    
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    
    # 按 unionid 聚类
    unionid_map = {}  # unionid -> first key
    for k in keys:
        for uid in all_records[k]['unionids']:
            if uid in unionid_map:
                union(k, unionid_map[uid])
            else:
                unionid_map[uid] = k
    
    # 按 phone 聚类
    phone_map = {}  # phone -> first key
    for k in keys:
        for ph in all_records[k]['phones']:
            if ph in phone_map:
                union(k, phone_map[ph])
            else:
                phone_map[ph] = k
    
    # 合并同组
    groups = {}  # root -> merged info
    for k in keys:
        root = find(k)
        if root not in groups:
            groups[root] = {'openid': '', 'mp_openid': '', 'phones': set(),
                           'unionids': set(), 'wechat_names': set()}
        g = groups[root]
        if all_records[k]['openid'] and not g['openid']:
            g['openid'] = all_records[k]['openid']
        if all_records[k]['mp_openid'] and not g['mp_openid']:
            g['mp_openid'] = all_records[k]['mp_openid']
        g['phones'].update(all_records[k]['phones'])
        g['unionids'].update(all_records[k]['unionids'])
        g['wechat_names'].update(all_records[k]['wechat_names'])
    
    # 转为 list
    result = []
    for root, g in groups.items():
        phones = sorted(g['phones'])
        unionids = sorted(g['unionids'])
        wechat_names = sorted(g['wechat_names'], key=len, reverse=True)
        result.append({
            'openid': g['openid'],
            'mp_openid': g['mp_openid'],
            'phone': phones[0] if phones else '',
            'all_phones': phones,
            'unionid': unionids[0] if unionids else '',
            'wechat_name': wechat_names[0] if wechat_names else '',
        })
    
    return result

def main():
    conn = get_conn()
    
    print('=== 收集身份标识 ===')
    balances_rows, po_rows = collect_all_identities(conn)
    print(f'  user_balances 记录数: {len(balances_rows)}')
    print(f'  phone_openids 记录数: {len(po_rows)}')
    
    print('=== 合并用户 ===')
    merged = merge_users(balances_rows, po_rows)
    print(f'  合并后用户数: {len(merged)}')
    
    # 统计
    has_unionid = sum(1 for u in merged if u['unionid'])
    has_mp = sum(1 for u in merged if u['mp_openid'])
    has_phone = sum(1 for u in merged if u['phone'])
    print(f'  有 unionid: {has_unionid}, 有 mp_openid: {has_mp}, 有 phone: {has_phone}')
    
    # 写入 app_users
    print('=== 写入 app_users ===')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    # 清空 app_users（幂等）
    cur.execute('DELETE FROM app_users')
    
    user_id_map = {}  # (openid, mp_openid) -> user_id
    phone_to_user_id = {}  # phone -> user_id
    
    for u in merged:
        cur.execute("""
            INSERT INTO app_users (unionid, phone, openid, mp_openid, wechat_name)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (u['unionid'], u['phone'], u['openid'], u['mp_openid'], u['wechat_name']))
        uid = cur.fetchone()['id']
        user_id_map[(u['openid'], u['mp_openid'])] = uid
        for ph in u['all_phones']:
            phone_to_user_id[ph] = uid
    
    conn.commit()
    print(f'  写入 {len(merged)} 条 app_users')
    
    # 回写 user_id 到 user_balances
    print('=== 回写 user_id 到 user_balances ===')
    updated = 0
    for (openid, mp_openid), uid in user_id_map.items():
        if openid:
            cur.execute('UPDATE user_balances SET user_id = %s WHERE openid = %s AND user_id = 0', (uid, openid))
            updated += cur.rowcount
        if mp_openid:
            cur.execute('UPDATE user_balances SET user_id = %s WHERE mp_openid = %s AND user_id = 0', (uid, mp_openid))
            updated += cur.rowcount
    conn.commit()
    print(f'  更新 {updated} 条 user_balances')
    
    # 回写 user_id 到 phone_openids
    print('=== 回写 user_id 到 phone_openids ===')
    updated = 0
    for phone, uid in phone_to_user_id.items():
        cur.execute('UPDATE phone_openids SET user_id = %s WHERE phone = %s AND user_id = 0', (uid, phone))
        updated += cur.rowcount
    conn.commit()
    print(f'  更新 {updated} 条 phone_openids')
    
    # 回写 user_id 到 orders
    print('=== 回写 user_id 到 orders ===')
    updated = 0
    # 通过 openid 匹配
    for (openid, mp_openid), uid in user_id_map.items():
        if openid:
            cur.execute('UPDATE orders SET user_id = %s WHERE openid = %s AND user_id = 0', (uid, openid))
            updated += cur.rowcount
    # 通过 mp_openid 匹配（orders 表的 mp_openid 列）
    for (openid, mp_openid), uid in user_id_map.items():
        if mp_openid:
            cur.execute('UPDATE orders SET user_id = %s WHERE mp_openid = %s AND user_id = 0', (uid, mp_openid))
            updated += cur.rowcount
    # 通过 phone 匹配剩余未匹配的
    for phone, uid in phone_to_user_id.items():
        cur.execute("""
            UPDATE orders SET user_id = %s 
            WHERE user_phone = %s AND user_id = 0
        """, (uid, phone))
        updated += cur.rowcount
    conn.commit()
    print(f'  更新 {updated} 条 orders')
    
    # 验证
    print('=== 验证 ===')
    cur.execute('SELECT count(*) as cnt FROM app_users')
    print(f'  app_users: {cur.fetchone()["cnt"]}')
    cur.execute('SELECT count(*) as cnt FROM orders WHERE user_id > 0')
    print(f'  orders with user_id: {cur.fetchone()["cnt"]}')
    cur.execute('SELECT count(*) as cnt FROM orders WHERE user_id = 0')
    print(f'  orders without user_id: {cur.fetchone()["cnt"]}')
    cur.execute('SELECT count(*) as cnt FROM user_balances WHERE user_id > 0')
    print(f'  user_balances with user_id: {cur.fetchone()["cnt"]}')
    cur.execute('SELECT count(*) as cnt FROM user_balances WHERE user_id = 0')
    print(f'  user_balances without user_id: {cur.fetchone()["cnt"]}')
    cur.execute('SELECT count(*) as cnt FROM phone_openids WHERE user_id > 0')
    print(f'  phone_openids with user_id: {cur.fetchone()["cnt"]}')
    cur.execute('SELECT count(*) as cnt FROM phone_openids WHERE user_id = 0')
    print(f'  phone_openids without user_id: {cur.fetchone()["cnt"]}')
    
    # 验证余额守恒
    cur.execute('SELECT SUM(balance) FROM user_balances')
    total = cur.fetchone()['sum']
    print(f'  总余额: {total}')
    
    cur.close()
    conn.close()
    print('=== 迁移完成 ===')

if __name__ == '__main__':
    main()
