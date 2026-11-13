#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[S1119] 建表 + 从 nginx 日志回填行为数据

用途：
  ① 建 user_behavior_log 表
  ② 解析 nginx 日志，提取 (openid, 路径, 时间)
  ③ 批量写入表（供打分模块使用）

用法：
  python3 backfill_behavior.py --days 3        # 回填最近3天
  python3 backfill_behavior.py --days 1        # 回填最近1天
"""
import sys, os, re, io, gzip, logging, argparse
from datetime import datetime, timedelta
logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')
from database import get_db

LOG_DIR = '/var/log/nginx'
LINE_RE = re.compile(r'^(\S+) \S+ \S+ \[(\d+)/(\w+)/(\d+):(\d+):(\d+):(\d+) ([+-]\d{4})\] "(\w+) ([^ ?"]*)')
OID_RE = re.compile(r'[?&]openid=([A-Za-z0-9_\-]{20,})')
MON = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,
       'Oct':10,'Nov':11,'Dec':12}

# 只记用户侧接口（白名单）
WHITELIST = [
    '/api/user/balance', '/api/user/orders', '/api/user/order-detail',
    '/api/user/wallet/transactions', '/api/user/wallet/withdrawals',
    '/api/user/withdraw', '/api/user/help-refund', '/api/user/info',
    '/api/user/subscribe-templates', '/api/retrieve/', '/api/deposit/',
    '/api/cabinets/', '/api/active-orders/', '/api/complaints',
    '/api/wx/login', '/api/config/public',
]

def action_of(path):
    if path.startswith('/api/user/balance'): return 'balance'
    if path.startswith('/api/user/wallet/transactions'): return 'transactions'
    if path.startswith('/api/user/wallet/withdrawals'): return 'withdrawals'
    if path.startswith('/api/user/withdraw'): return 'withdraw'
    if path.startswith('/api/user/orders'): return 'orders'
    if path.startswith('/api/user/order-detail'): return 'order_detail'
    if path.startswith('/api/user/help-refund'): return 'help_refund'
    if path.startswith('/api/user/info'): return 'info'
    if path.startswith('/api/user/subscribe-templates'): return 'subscribe'
    if path.startswith('/api/retrieve'): return 'retrieve'
    if path.startswith('/api/deposit'): return 'deposit'
    if path.startswith('/api/cabinets'): return 'cabinets'
    if path.startswith('/api/active-orders'): return 'active_orders'
    if path.startswith('/api/complaints'): return 'complaints'
    if path.startswith('/api/wx/login'): return 'login'
    return 'other'

def ensure_table(db):
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_behavior_log (
            id BIGSERIAL PRIMARY KEY,
            openid TEXT,
            user_phone TEXT,
            action TEXT,
            path TEXT,
            method TEXT,
            ip TEXT,
            status_code INTEGER,
            created_at TIMESTAMP NOT NULL,
            source TEXT DEFAULT 'nginx'
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ubl_openid_time ON user_behavior_log(openid, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ubl_created ON user_behavior_log(created_at DESC)")
    db.commit()
    cur.close()

def file_list(days):
    """按天数取日志文件（新的在前）"""
    out = []
    if days >= 1 and os.path.exists(os.path.join(LOG_DIR, 'access.log')):
        out.append(os.path.join(LOG_DIR, 'access.log'))
    if days >= 2 and os.path.exists(os.path.join(LOG_DIR, 'access.log.1')):
        out.append(os.path.join(LOG_DIR, 'access.log.1'))
    # .2.gz ~ .N.gz
    for i in range(2, min(days + 2, 20)):
        fp = os.path.join(LOG_DIR, 'access.log.%d.gz' % i)
        if os.path.exists(fp):
            out.append(fp)
    return out

def parse_file(fp, since, until, batch, db):
    """解析一个日志文件，返回写入条数"""
    opener = gzip.open if fp.endswith('.gz') else io.open
    cur = db.cursor()
    n = 0
    rows = []
    try:
        with opener(fp, 'rt', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if '/api/' not in line:
                    continue
                if not any(w in line for w in WHITELIST):
                    continue
                m = LINE_RE.match(line)
                if not m:
                    continue
                day, mon, year = int(m.group(2)), MON.get(m.group(3), 0), int(m.group(4))
                hh, mm_, ss = int(m.group(5)), int(m.group(6)), int(m.group(7))
                if not mon:
                    continue
                try:
                    dt = datetime(year, mon, day, hh, mm_, ss)
                except Exception:
                    continue
                if dt < since or dt > until:
                    continue
                path = m.group(10)
                om = OID_RE.search(line)
                if not om:
                    continue
                oid = om.group(1)
                rows.append((oid, None, action_of(path), path, m.group(9),
                             m.group(1), dt, 'nginx'))
                n += 1
                if len(rows) >= batch:
                    _flush(cur, rows, db)
                    rows = []
        if rows:
            _flush(cur, rows, db)
    except Exception as e:
        print('  解析 %s 出错: %s' % (fp, e))
    finally:
        cur.close()
    return n

def _flush(cur, rows, db):
    """批量写入：手工拼多值 INSERT（兼容只实现 execute 的自定义游标）"""
    if not rows:
        return
    CHUNK = 500
    try:
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            ph = ','.join(['(%s,%s,%s,%s,%s,%s,%s,%s)'] * len(chunk))
            flat = [v for r in chunk for v in r]
            cur.execute("""INSERT INTO user_behavior_log
                (openid, user_phone, action, path, method, ip, created_at, source)
                VALUES """ + ph, flat)
        db.commit()
    except Exception as e:
        print('  写入出错: %s' % e)
        try:
            db.rollback()
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=3, help='回填天数')
    ap.add_argument('--clear', action='store_true', help='先清空表')
    a = ap.parse_args()

    db = get_db()
    ensure_table(db)
    cur = db.cursor()
    if a.clear:
        cur.execute("TRUNCATE user_behavior_log")
        db.commit()
        print('  已清空 user_behavior_log')
    cur.execute("SELECT COUNT(*) AS n FROM user_behavior_log")
    r = cur.fetchone()
    before = (r['n'] if isinstance(r, dict) else r[0])
    cur.close()

    until = datetime.now()
    since = until - timedelta(days=a.days)
    print('=' * 90)
    print('  回填行为数据：%s ~ %s（%d 天）' % (
        since.strftime('%m-%d %H:%M'), until.strftime('%m-%d %H:%M'), a.days))
    print('=' * 90)
    files = file_list(a.days)
    print('  日志文件 %d 个' % len(files))
    total = 0
    for fp in files:
        n = parse_file(fp, since, until, 5000, db)
        total += n
        print('  %-45s %8d 条' % (os.path.basename(fp), n))
    cur = db.cursor()
    cur.execute("""SELECT COUNT(*) AS n, COUNT(DISTINCT openid) AS u,
                          MIN(created_at) AS a, MAX(created_at) AS b
                   FROM user_behavior_log""")
    r = cur.fetchone()
    if isinstance(r, dict):
        n_, u_, a_, b_ = r['n'], r['u'], r['a'], r['b']
    else:
        n_, u_, a_, b_ = r
    print('')
    print('  ★ 本次写入: %d 条' % total)
    print('  ★ 表内合计: %d 条 / %d 个 openid' % (n_, u_))
    print('  ★ 时间范围: %s ~ %s' % (a_, b_))
    print('  ★ 之前: %d 条 → 现在: %d 条' % (before, n_))
    cur.close(); db.close()

if __name__ == '__main__':
    main()
