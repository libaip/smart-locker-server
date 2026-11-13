# -*- coding: utf-8 -*-
"""fix_dbpath.py - 添加DB_PATH全局变量"""

FILE = '/home/ubuntu/smart-locker/routes/admin_v2.py'

with open(FILE, 'r', encoding='utf-8') as f:
    content = f.read()

if 'DB_PATH =' not in content.split('\n', 1410)[0]:
    # 在import sqlite3后添加DB_PATH
    old = "import sqlite3\n"
    new = "import sqlite3\nDB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'locker.db')\n"
    content = content.replace(old, new, 1)
    with open(FILE, 'w', encoding='utf-8') as f:
        f.write(content)
    print('DB_PATH added')
else:
    print('DB_PATH already exists')

# 验证
import py_compile
try:
    py_compile.compile(FILE, doraise=True)
    print('Python syntax OK')
except py_compile.PyCompileError as e:
    print(f'Syntax error: {e}')
