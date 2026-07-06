# -*- coding: utf-8 -*-
"""fix_menuGroups.py - 修复menuGroupsData不在Vue data中的问题"""

HTML_FILE = '/home/ubuntu/smart-locker/static/admin-v2.html'

with open(HTML_FILE, 'r', encoding='utf-8') as f:
    html = f.read()

changes = 0

# 1. 在Vue data中添加menuGroupsData空数组
old_data = "adminUser:'管理员',openGroups:{},"
new_data = "adminUser:'管理员',openGroups:{},menuGroupsData:[],"
if old_data in html:
    html = html.replace(old_data, new_data, 1)
    changes += 1
    print('[1] Added menuGroupsData to Vue data')
else:
    print('[1] SKIP - not found')

# 2. mounted里赋值menuGroupsData
old_mounted = "this.openGroups=og;"
new_mounted = "this.openGroups=og;this.menuGroupsData=menuGroupsData;"
if old_mounted in html:
    html = html.replace(old_mounted, new_mounted, 1)
    changes += 1
    print('[2] Added menuGroupsData assignment in mounted')
else:
    print('[2] SKIP - not found')

# 3. 删除app.menuGroupsData=menuGroupsData（现在通过Vue data管理）
old_assign = "app.menuGroupsData=menuGroupsData;"
new_assign = "// menuGroupsData now in Vue data"
if old_assign in html:
    html = html.replace(old_assign, new_assign)
    changes += 1
    print('[3] Removed app.menuGroupsData assignment')
else:
    print('[3] SKIP - not found')

with open(HTML_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'\nDone! {changes} changes applied.')
