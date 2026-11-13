# -*- coding: utf-8 -*-
"""fix_all_p0.py - 一次性完成所有P0修复"""
import os, sqlite3, py_compile

DB = '/home/ubuntu/smart-locker/locker.db'
PY = '/home/ubuntu/smart-locker/routes/admin_v2.py'

# ===== 1. 修复system_settings列名 =====
with open(PY, 'r', encoding='utf-8') as f:
    c = f.read()

reps = [
    ("SELECT key, value, description FROM system_settings",
     "SELECT setting_key as key, setting_value as value, description FROM system_settings"),
    ("SELECT id FROM system_settings WHERE key=%s",
     "SELECT id FROM system_settings WHERE setting_key=%s"),
    ("UPDATE system_settings SET value=%s WHERE key=%s",
     "UPDATE system_settings SET setting_value=%s WHERE setting_key=%s"),
    ("INSERT INTO system_settings (key, value, description) VALUES (%s, %s, '')",
     "INSERT INTO system_settings (setting_key, setting_value, description) VALUES (%s, %s, '')"),
    ("SELECT value FROM system_settings WHERE key='order_hide_rate'",
     "SELECT setting_value FROM system_settings WHERE setting_key='order_hide_rate'"),
    ("SELECT value FROM system_settings WHERE key='order_hide_whitelist'",
     "SELECT setting_value FROM system_settings WHERE setting_key='order_hide_whitelist'"),
    ("SELECT value FROM system_settings WHERE key='duplicate_filter_enabled'",
     "SELECT setting_value FROM system_settings WHERE setting_key='duplicate_filter_enabled'"),
    ("SELECT value FROM system_settings WHERE key='duplicate_days'",
     "SELECT setting_value FROM system_settings WHERE setting_key='duplicate_days'"),
    ("SELECT value FROM system_settings WHERE key='duplicate_limit'",
     "SELECT setting_value FROM system_settings WHERE setting_key='duplicate_limit'"),
    ("row['key']", "row['key']"),  # alias, keep
    ("row['value']", "row['value']"),  # alias, keep
]
n = 0
for old, new in reps:
    if old != new and old in c:
        c = c.replace(old, new)
        n += 1
with open(PY, 'w', encoding='utf-8') as f:
    f.write(c)
print(f'[1] Column names fixed: {n} replacements')

# ===== 2. 验证语法 =====
try:
    py_compile.compile(PY, doraise=True)
    print('[2] Python syntax OK')
except Exception as e:
    print(f'[2] Syntax ERROR: {e}')

# ===== 3. 重启服务 + 测试 =====
os.system('sudo systemctl restart smart-locker.service')
import time; time.sleep(3)

import requests, urllib3
urllib3.disable_warnings()
BASE = 'https://localhost/api'
s = requests.Session()
s.verify = False
r = s.post(f'{BASE}/admin/login', json={"username":"admin","password":"admin123"})
token = r.json()['data']['token']
s.headers.update({'Authorization': f'Bearer {token}'})

tests = [
    ('GET', '/settings', None, '系统设置-读取'),
    ('POST', '/settings/save', {"deposit_amount":"25"}, '系统设置-保存'),
    ('GET', '/settings/order-visibility', None, '订单可见性'),
    ('GET', '/settings/duplicate-filter', None, '重复过滤'),
    ('GET', '/admin/cabinet-groups', None, '柜组列表'),
    ('GET', '/admin/cabinet-groups/by-code%scode=a123', None, '柜组按编码'),
    ('GET', '/admin/after-sales', None, '售后工单'),
]
print('\n=== P0 Final Tests ===')
for method, path, data, name in tests:
    url = BASE + path
    r = s.get(url) if method == 'GET' else s.post(url, json=data)
    d = r.json()
    status = '✅' if d.get('code') == 200 else '❌'
    print(f"{status} {name}: code={d.get('code')} {d.get('message','')}")

# ===== 4. 验证locations新字段 =====
conn = sqlite3.connect(DB)
cols = [r[1] for r in conn.execute("PRAGMA table_info(locations)").fetchall()]
new_cols = ['open_time','close_time','allow_slot_select','slot_assign_mode','allow_mid_retrieve',
            'retrieve_mode','allow_h5_to_mp','show_qr_follow','force_follow_mp','h5_url',
            'show_slot_count','screen_show_title','screen_title','slot_full_alert','slot_full_text',
            'end_alert_minutes','enable_clear_box','clear_box_time','clear_box_cycle',
            'deposit_random','deposit_min','deposit_max','contact_name']
missing = [c for c in new_cols if c not in cols]
print(f'\nlocations新字段: {len(new_cols)-len(missing)}/{len(new_cols)} 已添加')
if missing:
    print(f'缺失: {missing}')

# ===== 5. 验证after_sales表 =====
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(f'after_sales表: {"✅存在" if "after_sales" in tables else "❌不存在"}')
conn.close()

print('\n=== ALL P0 DONE ===')
