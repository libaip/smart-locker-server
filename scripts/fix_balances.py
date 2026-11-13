import psycopg2, datetime

db = "postgresql://locker_admin:locker_pass_2024@127.0.0.1:6432/smart_locker"
c = psycopg2.connect(db)
cur = c.cursor()

# Get all user_balances
cur.execute("SELECT id, phone, COALESCE(openid,'') as openid, balance, total_deposited, total_withdrawn FROM user_balances ORDER BY phone")
rows = cur.fetchall()

print("Fixing %d user_balances records..." % len(rows))
fixed = 0
excess_total = 0
deficit_total = 0

for r in rows:
    uid, phone, openid, cur_bal, cur_dep, cur_wd = r
    
    # Calculate correct balance from orders
    cur.execute("""
        SELECT 
            COALESCE(SUM(CASE WHEN o.status IN (2,3) THEN o.deposit_amount ELSE 0 END), 0)
            - COALESCE(SUM(CASE WHEN o.status IN (4,5) THEN COALESCE(o.refund_amount, 0) ELSE 0 END), 0)
        FROM orders o 
        WHERE o.user_phone = %s AND COALESCE(o.openid, '') = %s AND o.status NOT IN (1, 6)
    """, (phone, openid))
    order_bal = float(cur.fetchone()[0])
    
    # Total withdrawn from withdrawal_records
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM withdrawal_records WHERE user_phone = %s AND COALESCE(openid, '') = %s", (phone, openid))
    withdrawn = float(cur.fetchone()[0])
    
    true_balance = order_bal - withdrawn
    
    if cur_bal is None: cur_bal = 0
    if true_balance < 0: true_balance = 0
    if abs(true_balance - cur_bal) > 0.01:
        diff = true_balance - cur_bal
        if diff > 0:
            deficit_total += diff
        else:
            excess_total += abs(diff)
        print("  %s openid=%s: balance %.2f -> %.2f (dep=%.0f wd=%.0f diff=%+.2f)" % (phone, openid[:8] if openid else '(none)', cur_bal, true_balance, order_bal, withdrawn, diff))
        cur.execute("UPDATE user_balances SET balance=%s, total_deposited=%s, total_withdrawn=%s WHERE id=%s", (true_balance, order_bal, withdrawn, uid))
        fixed += 1

c.commit()
print("\nFixed: %d records" % fixed)
print("Total excess removed: %.2f yuan" % excess_total)
print("Total deficit added: %.2f yuan" % deficit_total)
print("Net: %.2f yuan" % (deficit_total - excess_total))
c.close()
