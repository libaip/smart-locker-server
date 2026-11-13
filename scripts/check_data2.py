import psycopg2
c = psycopg2.connect("postgresql://locker_admin:locker_pass_2024@127.0.0.1:6432/smart_locker")
cur = c.cursor()

print("=== OVERVIEW ===")
cur.execute("SELECT COUNT(*) FROM user_balances")
total = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM user_balances WHERE openid IS NOT NULL AND openid != ''")
has_openid = cur.fetchone()[0]
print("Total users: %d  With openid: %d  Phone only: %d" % (total, has_openid, total - has_openid))

print("\n=== 1. SAME PHONE, MULTIPLE OPENIDS ===")
cur.execute("SELECT phone, COUNT(DISTINCT openid) as cnt FROM user_balances WHERE openid IS NOT NULL AND openid != '' GROUP BY phone HAVING COUNT(DISTINCT openid) > 1 ORDER BY cnt DESC")
rows = cur.fetchall()
if rows:
    for r in rows:
        print("Phone: %s  OpenIDs: %d" % (r[0], r[1]))
else:
    print("None found")

print("\n=== 2. BALANCE MISMATCH (balance != deposited - withdrawn) ===")
cur.execute("SELECT phone, balance, total_deposited, total_withdrawn, total_deposited - total_withdrawn as expected FROM user_balances WHERE ABS(balance - (total_deposited - total_withdrawn)) > 0.5 ORDER BY ABS(balance - (total_deposited - total_withdrawn)) DESC LIMIT 10")
rows = cur.fetchall()
if rows:
    for r in rows:
        print("Phone: %s | balance=%.0f | deposited=%.0f | withdrawn=%.0f | expected=%.0f" % (r[0], r[1], r[2], r[3], r[4]))
else:
    print("All balances match")

print("\n=== 3. EXCESS BALANCE (balance > expected) ===")
cur.execute("SELECT phone, balance, total_deposited - total_withdrawn as expected, balance - (total_deposited - total_withdrawn) as excess FROM user_balances WHERE balance > total_deposited - total_withdrawn + 0.5 ORDER BY excess DESC LIMIT 10")
rows = cur.fetchall()
if rows:
    for r in rows:
        print("Phone: %s | balance=%.0f | expected=%.0f | excess=%.0f" % (r[0], r[1], r[2], r[3]))
else:
    print("None (no excess balances)")

print("\n=== 4. DEFICIT BALANCE (balance < expected) ===")
cur.execute("SELECT phone, balance, total_deposited - total_withdrawn as expected, (total_deposited - total_withdrawn) - balance as deficit FROM user_balances WHERE balance < total_deposited - total_withdrawn - 0.5 ORDER BY deficit DESC LIMIT 10")
rows = cur.fetchall()
if rows:
    for r in rows:
        print("Phone: %s | balance=%.0f | expected=%.0f | deficit=%.0f" % (r[0], r[1], r[2], r[3]))
else:
    print("None")

print("\n=== 5. REFUNDED ORDERS - BALANCE NOT UPDATED ===")
cur.execute("SELECT ub.phone, ub.balance, o.deposit_amount, o.refund_amount,o.status FROM user_balances ub JOIN orders o ON ub.phone = o.user_phone WHERE o.status IN (4,5) AND ub.balance > 0 AND o.refund_amount > 0 AND ub.balance >= o.refund_amount LIMIT 10")
rows = cur.fetchall()
if rows:
    for r in rows:
        print("Phone: %s | balance=%.0f | deposit=%.0f | refund=%.0f | status=%s" % (r[0], r[1], r[2], r[3], r[4]))
else:
    print("None")

c.close()
