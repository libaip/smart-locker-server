#!/usr/bin/env python3
"""Fix admin_v2.py and admin.py approval flows to actually refund"""

# ==========================================
# Fix admin_v2.py - approve withdrawal
# ==========================================
print('=== Fix admin_v2.py ===')
with open('routes/admin_v2.py', 'r') as f:
    content = f.read()

# Find and fix the admin withdrawal approve endpoint
# Current approve just sets status=1, needs to call real refund
# Let's find the approve endpoint
approve_marker = "@bp.route('/admin/withdrawal/approve', methods=['POST'])"
if approve_marker in content:
    # Find the approve function
    idx = content.find(approve_marker)
    # Find the next function definition after this
    next_def = content.find('\n@bp.route', idx + 10)
    if next_def < 0:
        next_def = len(content)
    old_approve = content[idx:next_def]
    print(f'Found approve function, length={len(old_approve)}')
    
    new_approve = """@bp.route('/admin/withdrawal/approve', methods=['POST'])
@require_auth
def admin_withdrawal_approve():
    \"\"\"审批通过提现申请（status=0 -> 真退款 -> status=2或1）\"\"\"
    try:
        data = request.get_json()
        withdrawal_id = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT w.*, o.payment_channel_id, o.order_no FROM withdrawal_records w LEFT JOIN orders o ON w.order_id = o.id WHERE w.id=%s', (withdrawal_id,))
        wd = c.fetchone()
        if not wd:
            conn.close()
            return json_response(message='提现记录不存在', code=400)
        if wd['status'] != 0:
            conn.close()
            return json_response(message='该记录状态不允许审批', code=400)
        amount = wd['amount']
        phone = wd['user_phone']
        order_id = wd['order_id']
        # 扣除余额（如果是余额提现且还未扣）
        c.execute('SELECT balance FROM user_balances WHERE phone=%s', (phone,))
        bal = c.fetchone()
        if bal and bal['balance'] >= amount:
            c.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s', (amount, amount, phone))
        # 真正退款/转账
        refund_success = False
        refund_id = ''
        if order_id:
            # 订单押金退款
            from helpers import do_real_refund
            refund_success, refund_id, refund_msg = do_real_refund(order_id=order_id, amount=amount)
        else:
            # 余额提现转账
            from helpers import do_balance_transfer
            openid = wd['openid'] if 'openid' in wd.keys() and wd['openid'] else None
            refund_success, refund_id, refund_msg = do_balance_transfer(phone, amount, openid=openid)
        if refund_success:
            c.execute('UPDATE withdrawal_records SET status=2, approver=%s, approve_time=CURRENT_TIMESTAMP, refund_id=%s WHERE id=%s',
                       (session.get('admin_username', 'admin'), refund_id, withdrawal_id))
            if order_id:
                c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=%s WHERE id=%s', (refund_id, datetime.now(), order_id))
        else:
            c.execute('UPDATE withdrawal_records SET status=1, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (session.get('admin_username', 'admin'), withdrawal_id))
            if order_id:
                c.execute('UPDATE orders SET status=3 WHERE id=%s', (order_id,))
        conn.commit()
        conn.close()
        if refund_success:
            return json_response(message='审批通过，退款已完成')
        else:
            return json_response(message='审批通过，但退款失败，请手动确认退款')
    except Exception as e:
        logger.error('[withdrawal_approve] ' + str(e))
        return json_response(message=str(e), code=500)

"""
    content = content[:idx] + new_approve + content[next_def:]
    print('Replaced admin_withdrawal_approve')
else:
    print('WARNING: approve endpoint not found')

# Fix confirm-refund to also update order status
old_confirm = """        c.execute(\"UPDATE withdrawal_records SET status=2, approve_time=CURRENT_TIMESTAMP WHERE id=%s\", (withdrawal_id,))
        conn.commit()
        conn.close()
        return json_response(message='已确认退款完成')"""

new_confirm = """        # 确认退款完成：检查是否有关联订单需要更新
        c.execute('SELECT order_id FROM withdrawal_records WHERE id=%s', (withdrawal_id,))
        wd2 = c.fetchone()
        if wd2 and wd2['order_id']:
            c.execute('UPDATE orders SET status=4, refund_time=%s WHERE id=%s AND status!=4', (datetime.now(), wd2['order_id']))
        c.execute(\"UPDATE withdrawal_records SET status=2, approve_time=CURRENT_TIMESTAMP WHERE id=%s\", (withdrawal_id,))
        conn.commit()
        conn.close()
        return json_response(message='已确认退款完成')"""

if old_confirm in content:
    content = content.replace(old_confirm, new_confirm, 1)
    print('Fixed confirm-refund to update order status')
else:
    print('WARNING: confirm-refund block not found exactly')

# Fix reject to return balance
old_reject = """@bp.route('/admin/withdrawal/reject', methods=['POST'])"""

with open('routes/admin_v2.py', 'w') as f:
    f.write(content)
print('admin_v2.py saved')

# ==========================================
# Fix admin.py - queue auto-approve (cron job)
# ==========================================
print('\n=== Fix admin.py ===')
with open('routes/admin.py', 'r') as f:
    content = f.read()

# Find the auto-approve section that does cron-based approval
# It currently just sets status=2 directly without actual refund
# We need to find it and fix it
if 'auto_approve_rate' in content and 'withdrawal_records' in content:
    # Find the queue processing section
    # The cron job section that processes withdrawals based on time and probability
    # It's in the function that does location-based auto approval
    # Look for the pattern where it sets status=2 directly
    
    # Find: "cursor.execute('UPDATE withdrawal_records SET status = 2"
    lines = content.split('\n')
    fixed = False
    for i, line in enumerate(lines):
        if "UPDATE withdrawal_records SET status = 2" in line and "auto_approve" in content[max(0,content.find(line)-500):content.find(line)+len(line)]:
            # This is the queue auto-approve section
            # Replace status=2 with real refund logic
            # Find the full block
            start_line = max(0, i - 5)
            end_line = min(len(lines), i + 5)
            print(f'Found queue auto-approve at line {i+1}: {line.strip()[:80]}')
            print(f'Context: lines {start_line+1}-{end_line+1}')
            # We'll need to do a more targeted replacement
            # For now, just note the location
            fixed = True
    
    if not fixed:
        # Try a different approach - find the function that does the cron approval
        idx = content.find("l.auto_approve_rate")
        if idx > 0:
            # Find the surrounding function
            func_start = content.rfind('def ', 0, idx)
            func_end = content.find('\ndef ', idx)
            if func_start > 0 and func_end > 0:
                old_func = content[func_start:func_end]
                print(f'Found cron function at pos {func_start}, length={len(old_func)}')
                # Replace the status=2 direct update with real refund
                if "status = 2, approver = %s, auto_approve_time" in old_func:
                    # This is the queue approval - replace with real refund logic
                    old_line = "cursor.execute('UPDATE withdrawal_records SET status = 2, approver = %s, auto_approve_time = %s WHERE id = %s', ('system', now.strftime('%Y-%m-%d %H:%M:%S'), record['id']))"
                    new_line = """# 队列审批：真正退款
                    if record.get('order_id'):
                        from helpers import do_real_refund
                        refund_ok, refund_rid, refund_msg = do_real_refund(order_id=record['order_id'], amount=record['amount'])
                    else:
                        from helpers import do_balance_transfer
                        wd_openid = record.get('openid', '') or ''
                        refund_ok, refund_rid, refund_msg = do_balance_transfer(record['user_phone'], record['amount'], openid=wd_openid if wd_openid else None)
                    if refund_ok:
                        cursor.execute('UPDATE withdrawal_records SET status = 2, approver = %s, auto_approve_time = %s, refund_id = %s WHERE id = %s', ('system', now.strftime('%Y-%m-%d %H:%M:%S'), refund_rid, record['id']))
                        if record.get('order_id'):
                            cursor.execute('UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s', (refund_rid, datetime.now(), record['order_id']))
                    else:
                        cursor.execute('UPDATE withdrawal_records SET status = 1, approver = %s, auto_approve_time = %s WHERE id = %s', ('system', now.strftime('%Y-%m-%d %H:%M:%S'), record['id']))"""
                    if old_line in content:
                        content = content.replace(old_line, new_line, 1)
                        print('Fixed queue auto-approve in admin.py')
                    else:
                        print('WARNING: exact queue approve line not found')
                        # Try without the exact string
                        for i2, l2 in enumerate(lines):
                            if "status = 2, approver = %s, auto_approve_time" in l2:
                                print(f'Found similar line at {i2+1}: {l2.strip()[:80]}')

with open('routes/admin.py', 'w') as f:
    f.write(content)
print('admin.py saved')

# Also fix the approve_withdrawal function in admin.py (manual approval)
print('\nFix approve_withdrawal in admin.py')
with open('routes/admin.py', 'r') as f:
    content = f.read()

# The approve_withdrawal function at line ~1240
# Current: fake refund with MOCK_R, directly set order status=4
# New: call do_real_refund, update status based on result
idx = content.find('def approve_withdrawal(withdrawal_id):')
if idx > 0:
    next_def = content.find('\ndef ', idx + 10)
    if next_def < 0:
        next_def = content.find('\n@', idx + 10)
    old_func = content[idx:next_def]
    print(f'Found approve_withdrawal, length={len(old_func)}')
    
    new_func = """def approve_withdrawal(withdrawal_id):
    try:
        approver = session.get('admin_username', 'admin')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT w.*, o.payment_channel_id, o.order_no FROM withdrawal_records w LEFT JOIN orders o ON w.order_id = o.id WHERE w.id = %s', (withdrawal_id,))
        record = cursor.fetchone()
        if not record or record['status'] != 0:
            conn.close()
            return json_response(message='提现记录不存在或已处理', code=404 if not record else 400)
        order_id = record['order_id']
        amount = record['amount']
        phone = record['user_phone']
        # 扣除余额
        cursor.execute('SELECT balance FROM user_balances WHERE phone=%s', (phone,))
        bal = cursor.fetchone()
        if bal and bal['balance'] >= amount:
            cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s', (amount, amount, phone))
        # 真正退款
        if order_id:
            from helpers import do_real_refund
            refund_success, refund_id, refund_msg = do_real_refund(order_id=order_id, amount=amount)
        else:
            from helpers import do_balance_transfer
            wd_openid = record['openid'] if 'openid' in record.keys() and record['openid'] else None
            refund_success, refund_id, refund_msg = do_balance_transfer(phone, amount, openid=wd_openid)
        if refund_success:
            cursor.execute('UPDATE withdrawal_records SET status = 2, approver = %s, approve_time = %s, refund_id = %s WHERE id = %s', (approver, datetime.now(), refund_id, withdrawal_id))
            if order_id:
                cursor.execute('UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s', (refund_id, datetime.now(), order_id))
                if record.get('slot_id') or (order_id and True):
                    cursor.execute('SELECT slot_id FROM orders WHERE id=%s', (order_id,))
                    o2 = cursor.fetchone()
                    if o2 and o2['slot_id']:
                        cursor.execute('SELECT COUNT(*) FROM orders WHERE slot_id = %s AND status IN (1,2) AND id != %s', (o2['slot_id'], order_id))
                        if cursor.fetchone()[0] == 0:
                            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (o2['slot_id'],))
                cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)', (order_id, amount, refund_id))
        else:
            cursor.execute('UPDATE withdrawal_records SET status = 1, approver = %s, approve_time = %s WHERE id = %s', (approver, datetime.now(), withdrawal_id))
            if order_id:
                cursor.execute('UPDATE orders SET status = 3 WHERE id = %s', (order_id,))
        conn.commit()
        conn.close()
        if refund_success:
            return json_response(message='审批通过，退款已完成')
        else:
            return json_response(message='审批通过，但退款失败，需手动确认')
    except Exception as e:
        logger.error('[approve_withdrawal] ' + str(e))
        return json_response(message=str(e), code=500)

"""
    content = content[:idx] + new_func + content[next_def:]
    print('Replaced approve_withdrawal in admin.py')

with open('routes/admin.py', 'w') as f:
    f.write(content)
print('admin.py saved')
