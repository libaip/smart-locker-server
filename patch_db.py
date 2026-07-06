import sqlite3
with open('/home/ubuntu/smart-locker/database.py', 'a') as f:
    f.write('''

# ===== compatibility: sqlite3 support %s param style =====
_orig_sqlite3_execute = sqlite3.Cursor.execute
def _sqlite3_execute_hook(self, sql, params=None):
    if params is not None and  %s in str(sql):
        sql = sql.replace(%s, ?)
    if params is not None:
        return _orig_sqlite3_execute(self, sql, params)
    return _orig_sqlite3_execute(self, sql)
sqlite3.Cursor.execute = _sqlite3_execute_hook
''')
print('Patch file written')
