#!/usr/bin/env python3
"""身份一致性每日巡检（只读，不修改任何数据）。"""
import os
import sys
import time
import json
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

LOG_PATH = '/home/ubuntu/smart-locker/logs/identity_consistency.log'
ALERT_PATH = '/home/ubuntu/smart-locker/logs/identity_consistency_alert.log'
STATE_PATH = '/home/ubuntu/smart-locker/logs/identity_consistency_state.json'
SAMPLE_LIMIT = 20


def run_check(cur, name, sql, params=(), warning=False):
    cur.execute(sql, params)
    rows = cur.fetchall()
    return {
        'name': name,
        'warning': warning,
        'count': len(rows),
        'samples': rows[:SAMPLE_LIMIT],
    }


def main():
    start = time.time()
    conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    checks = []
    checks.append(run_check(cur, 'phone_multi_unionid', """
        SELECT phone, COUNT(DISTINCT NULLIF(unionid,'')) AS unions,
               array_agg(DISTINCT NULLIF(unionid,'')) AS unionids
        FROM users
        WHERE NULLIF(phone,'') IS NOT NULL AND NULLIF(unionid,'') IS NOT NULL
        GROUP BY phone HAVING COUNT(DISTINCT NULLIF(unionid,'')) > 1
        ORDER BY unions DESC
    """, warning=True))
    checks.append(run_check(cur, 'unionid_multi_phone', """
        SELECT unionid, COUNT(DISTINCT phone) AS phones,
               array_agg(DISTINCT phone) AS phone_list
        FROM users
        WHERE NULLIF(unionid,'') IS NOT NULL AND NULLIF(phone,'') IS NOT NULL
        GROUP BY unionid HAVING COUNT(DISTINCT phone) > 1
        ORDER BY phones DESC
    """))
    checks.append(run_check(cur, 'phone_duplicate_union_wallet', """
        SELECT phone, COUNT(*) AS rows_cnt, COUNT(DISTINCT user_id) AS accounts,
               array_agg(DISTINCT user_id) AS user_ids
        FROM user_balances
        WHERE NULLIF(phone,'') IS NOT NULL AND NULLIF(unionid,'') IS NOT NULL
        GROUP BY phone HAVING COUNT(*) > 1 AND COUNT(DISTINCT unionid) = 1
        ORDER BY accounts DESC
    """))
    checks.append(run_check(cur, 'orphan_order_user_id', """
        SELECT o.id AS order_id, o.user_phone, o.user_id
        FROM orders o
        WHERE o.user_id > 0 AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = o.user_id)
        ORDER BY o.id DESC
    """))
    checks.append(run_check(cur, 'orphan_balance_user_id', """
        SELECT b.id, b.phone, b.user_id
        FROM user_balances b
        WHERE b.user_id > 0 AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = b.user_id)
        ORDER BY b.id DESC
    """))
    checks.append(run_check(cur, 'orphan_phone_openid_user_id', """
        SELECT p.id, p.phone, p.user_id
        FROM phone_openids p
        WHERE p.user_id > 0 AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)
        ORDER BY p.id DESC
    """))
    checks.append(run_check(cur, 'half_identity_users_mp_empty',
        """
        SELECT u.id AS user_id, u.phone, b.id AS balance_id, b.mp_openid
        FROM users u
        JOIN user_balances b ON b.phone = u.phone
        WHERE NULLIF(u.phone,'') IS NOT NULL
          AND (u.mp_openid IS NULL OR u.mp_openid = '')
          AND NULLIF(b.mp_openid,'') IS NOT NULL
        ORDER BY b.id DESC
        """,
        warning=True))
    checks.append(run_check(cur, 'cross_phone_order_identity',
        """
        SELECT o.id AS order_id, o.user_phone, o.user_id, u.phone AS users_phone
        FROM orders o JOIN users u ON u.id = o.user_id
        WHERE o.user_id > 0 AND NULLIF(o.user_phone,'') IS NOT NULL
          AND NULLIF(u.phone,'') IS NOT NULL AND o.user_phone != u.phone
        ORDER BY o.id DESC
        """,
        warning=True))
    conn.close()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    prev_state = {}
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            prev_state = json.load(f)
    except Exception:
        prev_state = {}
    has_baseline = bool(prev_state)

    deltas = []
    for c in checks:
        if c['warning']:
            continue
        prev = prev_state.get(c['name'], {}).get('count')
        if prev is not None and c['count'] > prev:
            deltas.append(f"{c['name']}(+{c['count'] - prev})")

    new_state = {c['name']: {'count': c['count']} for c in checks}
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print('state write failed:', e)

    hard = [c for c in checks if not c['warning'] and c['count'] > 0]
    warn = [c for c in checks if c['warning'] and c['count'] > 0]

    lines = []
    lines.append(f'=== identity consistency check {now} ===')
    for c in checks:
        lines.append(f'{c["name"]}: count={c["count"]}'
                     f'{" (warning)" if c["warning"] else ""}')
        for r in c['samples']:
            lines.append('  ' + str(r))
    lines.append(f'hard_issues={sum(c["count"] for c in hard)} '
                 f'warnings={sum(c["count"] for c in warn)} '
                 f'duration={time.time()-start:.2f}s '
                 f'increased={",".join(deltas) if deltas else "none"}')
    lines.append('')
    output = '\n'.join(lines)
    print(output)

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(output)
    except Exception as e:
        print('log write failed:', e)

    alert = deltas if has_baseline else []
    if alert:
        alert_line = now + ' identity hard issues increased: ' + ', '.join(alert)
        try:
            os.makedirs(os.path.dirname(ALERT_PATH), exist_ok=True)
            with open(ALERT_PATH, 'a', encoding='utf-8') as f:
                f.write(alert_line + '\n')
        except Exception as e:
            print('alert write failed:', e)
    print('baseline:', 'established' if not has_baseline else 'ok',
          '| alert:', 'yes' if alert else 'no')
    return 1 if alert else 0


if __name__ == '__main__':
    sys.exit(main())
