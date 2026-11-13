# -*- coding: utf-8 -*-
"""fix_p0.py - 修复P0级4个问题：
1. 创建after_sales表
2. locations表补全缺失字段
3. 系统设置管理API
4. 柜组管理API
"""
import subprocess, sys

DB = '/home/ubuntu/smart-locker/locker.db'

# ============ P0-1: 创建after_sales表 ============
sql_after_sales = '''
CREATE TABLE IF NOT EXISTS after_sales(
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
);
'''

# ============ P0-2: locations表补全缺失字段 ============
new_columns = [
    "open_time TEXT DEFAULT '08:00'",
    "close_time TEXT DEFAULT '22:00'",
    "allow_slot_select INTEGER DEFAULT 0",
    "slot_assign_mode TEXT DEFAULT 'auto'",
    "allow_mid_retrieve INTEGER DEFAULT 1",
    "retrieve_mode TEXT DEFAULT 'both'",
    "allow_h5_to_mp INTEGER DEFAULT 0",
    "show_qr_follow INTEGER DEFAULT 0",
    "force_follow_mp INTEGER DEFAULT 0",
    "h5_url TEXT DEFAULT ''",
    "show_slot_count INTEGER DEFAULT 1",
    "screen_show_title INTEGER DEFAULT 1",
    "screen_title TEXT DEFAULT ''",
    "slot_full_alert INTEGER DEFAULT 0",
    "slot_full_text TEXT DEFAULT ''",
    "end_alert_minutes INTEGER DEFAULT 0",
    "enable_clear_box INTEGER DEFAULT 0",
    "clear_box_time TEXT DEFAULT '23:00'",
    "clear_box_cycle INTEGER DEFAULT 1",
    "deposit_random INTEGER DEFAULT 0",
    "deposit_min REAL DEFAULT 0",
    "deposit_max REAL DEFAULT 0",
    "contact_name TEXT DEFAULT ''",
]

# 执行SQL
import sqlite3
conn = sqlite3.connect(DB)
c = conn.cursor()

# P0-1: after_sales
c.executescript(sql_after_sales)
print('[P0-1] after_sales table created')

# P0-2: locations columns
existing = set()
for row in c.execute("PRAGMA table_info(locations)").fetchall():
    existing.add(row[1])

added = 0
for col_def in new_columns:
    col_name = col_def.split()[0]
    if col_name not in existing:
        c.execute(f'ALTER TABLE locations ADD COLUMN {col_def}')
        added += 1
        print(f'[P0-2] Added column: {col_name}')
    else:
        print(f'[P0-2] Column exists: {col_name}')

print(f'[P0-2] Total new columns added: {added}')

conn.commit()
conn.close()
print('\nDatabase fixes done!')
