# -*- coding: utf-8 -*-
"""fix_imports.py - 修复jsonify导入"""
FILE = '/home/ubuntu/smart-locker/routes/admin_v2.py'

with open(FILE, 'r', encoding='utf-8') as f:
    content = f.read()

old_import = 'from flask import Blueprint, request, session\n'
new_import = 'from flask import Blueprint, request, session, jsonify\n'

if old_import in content:
    content = content.replace(old_import, new_import, 1)
    with open(FILE, 'w', encoding='utf-8') as f:
        f.write(content)
    print('jsonify import added')
else:
    if 'jsonify' in content.split('\n')[5]:
        print('jsonify already imported')
    else:
        print('Pattern not found, manual check needed')

# Verify
import py_compile
try:
    py_compile.compile(FILE, doraise=True)
    print('Python syntax OK')
except py_compile.PyCompileError as e:
    print(f'Syntax error: {e}')
