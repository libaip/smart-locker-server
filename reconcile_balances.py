#!/usr/bin/env python3
"""
user_balances 对账修复脚本 v2
数据源: orders (refund_mark=1) + withdrawal_records (status=2)
策略: 每个phone的correct值放到最新一条balance记录，多openid的其余记录清零
"""
import psycopg2
from psycopg2.extras import RealDictCursor
import json
from datetime import datetime
from collections import defaultdict

DSN = "postgresql://locker_admin:locker_pass_2024@127.0.0.1:6432/smart_locker"

def main():
    conn = psycopg2.connect(DSN, connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ========== 1. 备份 ==========
    cur.execute("SELECT id, phone, openid, balance, total_deposited, total_withdrawn FROM user_balances ORDER BY id")
    current_data = [dict(r) for r in cur.fetchall()]
    backup_file = f"/home/ubuntu/smart-locker/balance_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(backup_file, 'w') as f:
        json.dump(current_data, f, default=str, indent=2)
    print(f"[BACKUP] {len(current_data)} records -> {backup_file}")

    # ========== 2. 计算每个phone的正确值 ==========
    cur.execute("""
        WITH order_deposits AS (
            SELECT user_phone, COALESCE(SUM(deposit_amount), 0) as total_dep
            FROM orders WHERE refund_mark = 1
            GROUP BY user_phone
        ),
        wd_totals AS (
            SELECT user_phone, COALESCE(SUM(amount), 0) as total_wd
            FROM withdrawal_records WHERE status = 2
            GROUP BY user_phone
        )
        SELECT 
            ap.phone,
            COALESCE(od.total_dep, 0) as correct_deposited,
            COALESCE(wt.total_wd, 0) as correct_withdrawn,
            COALESCE(od.total_dep, 0) - COALESCE(wt.total_wd, 0) as correct_balance
        FROM (
            SELECT DISTINCT user_phone as phone FROM orders WHERE refund_mark = 1
            UNION
            SELECT DISTINCT phone FROM user_balances
        ) ap
        LEFT JOIN order_deposits od ON ap.phone = od.user_phone
        LEFT JOIN wd_totals wt ON ap.phone = wt.user_phone
    """)
    phone_correct = {r['phone']: r for r in cur.fetchall()}
    print(f"[CALC] {len(phone_correct)} phones computed")

    # ========== 3. 读取所有balance记录，按phone分组 ==========
    cur.execute("SELECT id, phone, openid, balance, total_deposited, total_withdrawn FROM user_balances ORDER BY id")
    all_records = cur.fetchall()

    phone_records = defaultdict(list)
    for rec in all_records:
        phone_records[rec['phone']].append(rec)

    # ========== 4. 计算修复计划 ==========
    fixes = []
    stats = {'total_records': len(all_records), 'no_change': 0, 'value_fix': 0, 'multi_zero': 0, 'no_orders_zero': 0}

    for phone, recs in phone_records.items():
        correct = phone_correct.get(phone)
        # 按id降序，最新的排前面
        recs_sorted = sorted(recs, key=lambda x: x['id'], reverse=True)
        
        for i, rec in enumerate(recs_sorted):
            old = {'b': float(rec['balance']), 'd': float(rec['total_deposited']), 'w': float(rec['total_withdrawn'])}
            
            if i == 0:
                # 主记录：设为correct值
                if not correct or (abs(correct['correct_balance']) < 0.01 and abs(correct['correct_deposited']) < 0.01):
                    # 没有退款订单，清零
                    new = {'b': 0, 'd': 0, 'w': 0}
                    reason = 'no_orders'
                else:
                    new = {'b': float(correct['correct_balance']), 'd': float(correct['correct_deposited']), 'w': float(correct['correct_withdrawn'])}
                    reason = 'fix'
            else:
                # 非主记录（多openid）：清零
                new = {'b': 0, 'd': 0, 'w': 0}
                reason = 'multi_zero'

            changed = abs(old['b'] - new['b']) > 0.01 or abs(old['d'] - new['d']) > 0.01 or abs(old['w'] - new['w']) > 0.01
            if changed:
                fixes.append({'id': rec['id'], 'phone': phone, 'old': old, 'new': new, 'reason': reason})
                stats[{'fix': 'value_fix', 'multi_zero': 'multi_zero', 'no_orders': 'no_orders_zero'}.get(reason, 'value_fix')] += 1
            else:
                stats['no_change'] += 1

    print(f"\n[PLAN] fixes needed: {len(fixes)}")
    print(f"  value_fix: {stats['value_fix']}")
    print(f"  multi_zero: {stats['multi_zero']}")
    print(f"  no_orders_zero: {stats['no_orders_zero']}")
    print(f"  no_change: {stats['no_change']}")

    if not fixes:
        print("[DONE] No corrections needed")
        conn.close()
        return

    # 展示一些关键修复
    neg_fixes = [f for f in fixes if f['old']['b'] < 0]
    print(f"  negative balance fixes: {len(neg_fixes)}")
    if neg_fixes[:3]:
        for f in neg_fixes[:3]:
            print(f"    phone={f['phone']}: {f['old']['b']} -> {f['new']['b']}")

    # ========== 5. 执行修复 ==========
    for fix in fixes:
        cur.execute(
            "UPDATE user_balances SET balance=%s, total_deposited=%s, total_withdrawn=%s WHERE id=%s",
            (fix['new']['b'], fix['new']['d'], fix['new']['w'], fix['id'])
        )
    conn.commit()
    print(f"\n[EXEC] {len(fixes)} records updated")

    # ========== 6. 验证 ==========
    cur.execute("SELECT COUNT(*) as cnt FROM user_balances WHERE balance < 0")
    neg_remaining = cur.fetchone()['cnt']
    cur.execute("SELECT COUNT(*) as cnt FROM user_balances WHERE ABS(balance - (total_deposited - total_withdrawn)) > 0.01")
    mismatch_remaining = cur.fetchone()['cnt']
    cur.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(balance),0) as total FROM user_balances")
    total_stats = cur.fetchone()

    print(f"\n[VERIFY]")
    print(f"  Negative balances: {neg_remaining} (was 71)")
    print(f"  Math mismatches: {mismatch_remaining} (was 330)")
    print(f"  Total records: {total_stats['cnt']}")
    print(f"  Total balance sum: {float(total_stats['total']):.2f}")
    
    conn.close()
    print("\n[DONE] Reconciliation complete")

if __name__ == '__main__':
    main()
