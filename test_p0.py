# -*- coding: utf-8 -*-
"""test_p0.py - 测试P0修复结果"""
import requests, json, urllib3
urllib3.disable_warnings()

BASE = 'https://localhost/api'
s = requests.Session()
s.verify = False

# 登录
r = s.post(f'{BASE}/admin/login', json={"username":"admin","password":"admin123"})
token = r.json()['data']['token']
s.headers.update({'Authorization': f'Bearer {token}'})

tests = [
    ('GET', '/settings', None, '系统设置-读取'),
    ('POST', '/settings/save', {"deposit_amount":"25"}, '系统设置-保存'),
    ('GET', '/settings/order-visibility', None, '订单可见性-读取'),
    ('GET', '/settings/duplicate-filter', None, '重复过滤-读取'),
    ('GET', '/admin/cabinet-groups', None, '柜组列表'),
    ('GET', '/admin/cabinet-groups/by-code%scode=a123', None, '柜组按编码查询'),
    ('GET', '/admin/after-sales', None, '售后工单列表'),
]

print("=== P0 API Tests ===")
for method, path, data, name in tests:
    url = BASE + path
    if method == 'GET':
        r = s.get(url)
    else:
        r = s.post(url, json=data)
    d = r.json()
    code = d.get('code')
    msg = d.get('message','')
    data_val = d.get('data')
    if isinstance(data_val, dict):
        info = f"keys={list(data_val.keys())[:5]}"
    elif isinstance(data_val, list):
        info = f"count={len(data_val)}"
    else:
        info = str(data_val)[:50] if data_val else ''
    status = '✅' if code == 200 else '❌'
    print(f"{status} {name}: code={code} {msg} {info}")

print("\n=== Done ===")
