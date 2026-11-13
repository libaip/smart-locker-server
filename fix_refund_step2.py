#!/usr/bin/env python3
"""Fix routes/user.py - Fix user_withdraw and deposit_end_storage to actually refund"""

# ==========================================
# Fix user_withdraw (余额提现)
# ==========================================
print('=== Fix user_withdraw ===')
with open('routes/user.py', 'r') as f:
    content = f.read()

# Replace the auto_approve block in user_withdraw
# Current: deduct balance + insert status=1 + return "退款处理中"
# New: deduct balance + call do_balance_transfer + status=2 on success, status=1 on failure
old_auto = """        if withdraw_mode == 'auto_approve':
            # 自动审批模式：直接扣除余额并批准
            cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s', 
                           (amount, amount, phone))
            cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, auto_approve_time, click_count) VALUES (NULL, %s, %s, 1, datetime(\"now\"), 1)',
                           (phone, amount))
            withdrawal_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return json_response(data={
                'withdrawal_id': withdrawal_id,
                'status': 'approved',
                'message': '提现申请已自动通过，退款处理中'
            })"""

new_auto = """        if withdraw_mode == 'auto_approve':
            # 自动审批模式：扣除余额 + 真正退款
            cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s', 
                           (amount, amount, phone))
            # 真正调用退款/转账API
            from helpers import do_balance_transfer
            transfer_success, transfer_id, transfer_msg = do_balance_transfer(phone, amount, openid=data.get('openid'))
            if transfer_success:
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, auto_approve_time, openid, click_count) VALUES (NULL, %s, %s, 2, datetime(\"now\"), %s, 1)',
                               (phone, amount, data.get('openid', '')))
                withdrawal_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return json_response(data={
                    'withdrawal_id': withdrawal_id,
                    'status': 'refunded',
                    'message': '提现成功，退款已到账'
                })
            else:
                # 退款失败，记录为退款中状态，需后台手动确认
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, auto_approve_time, openid, click_count) VALUES (NULL, %s, %s, 1, datetime(\"now\"), %s, 1)',
                               (phone, amount, data.get('openid', '')))
                withdrawal_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return json_response(data={
                    'withdrawal_id': withdrawal_id,
                    'status': 'pending_refund',
                    'message': '提现申请已通过，退款处理中'
                })"""

if old_auto in content:
    content = content.replace(old_auto, new_auto, 1)
    print('Fixed user_withdraw auto_approve')
else:
    print('WARNING: auto_approve block not found in user_withdraw, trying alternative match')
    # Try to find a more flexible match
    if "withdraw_mode == 'auto_approve'" in content and 'INSERT INTO withdrawal_records' in content:
        # Find and replace the section
        start = content.find("if withdraw_mode == 'auto_approve':")
        end = content.find("else:", start)
        if start > 0 and end > 0:
            old_section = content[start:end]
            new_section = """if withdraw_mode == 'auto_approve':
            # 自动审批模式：扣除余额 + 真正退款
            cursor.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s', 
                           (amount, amount, phone))
            from helpers import do_balance_transfer
            transfer_success, transfer_id, transfer_msg = do_balance_transfer(phone, amount, openid=data.get('openid'))
            if transfer_success:
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, auto_approve_time, openid, click_count) VALUES (NULL, %s, %s, 2, datetime(\"now\"), %s, 1)',
                               (phone, amount, data.get('openid', '')))
                withdrawal_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return json_response(data={
                    'withdrawal_id': withdrawal_id,
                    'status': 'refunded',
                    'message': '提现成功，退款已到账'
                })
            else:
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, auto_approve_time, openid, click_count) VALUES (NULL, %s, %s, 1, datetime(\"now\"), %s, 1)',
                               (phone, amount, data.get('openid', '')))
                withdrawal_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return json_response(data={
                    'withdrawal_id': withdrawal_id,
                    'status': 'pending_refund',
                    'message': '提现申请已通过，退款处理中'
                })
        """
            content = content[:start] + new_section + content[end:]
            print('Fixed user_withdraw auto_approve (alternative)')

# Also fix manual_approve to pass openid
old_manual = """            cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count) VALUES (NULL, %s, %s, 0, 1)',
                           (phone, amount))"""
new_manual = """            cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, openid, click_count) VALUES (NULL, %s, %s, 0, %s, 1)',
                           (phone, amount, data.get('openid', '')))"""
if old_manual in content:
    content = content.replace(old_manual, new_manual, 1)
    print('Fixed user_withdraw manual_approve to include openid')

with open('routes/user.py', 'w') as f:
    f.write(content)
print('user_withdraw saved')

# ==========================================
# Fix deposit_end_storage (取包退押金)
# ==========================================
print('\n=== Fix deposit_end_storage ===')
with open('routes/user.py', 'r') as f:
    content = f.read()

# The current deposit_end_storage does a fake refund.
# New logic: check location withdraw_mode, call real refund API
# Find the section from "refund_amount = order['deposit_amount']" to the end of the function

# Replace the refund logic block
old_refund_block = """        refund_amount = order['deposit_amount']
        compartment_number = order['slot_number'] or order['compartment_number']
        refund_success = True
        refund_id = None
        if order['transaction_id'] and refund_amount > 0:
            if is_mock_mode():
                refund_id = 'MOCK_R' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
            else:
                refund_id = 'MOCK_R' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
        cursor.execute('INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time, retrieve_time) VALUES (%s, %s, %s, %s, 1, %s, %s)',
                       (order['cabinet_id'], order['compartment_number'], order['user_phone'], order['access_code'], order['store_time'], datetime.now()))
        new_status = 4 if refund_success else 6
        cursor.execute('UPDATE orders SET status = %s, refund_id = %s, refund_time = %s WHERE id = %s', (new_status, refund_id, datetime.now(), order_id))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        if refund_amount > 0:
            cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, %s)', (order_id, refund_amount, refund_id, 1 if refund_success else 0))
        conn.commit()
        conn.close()"""

new_refund_block = """        refund_amount = order['deposit_amount']
        compartment_number = order['slot_number'] or order['compartment_number']
        cursor.execute('INSERT INTO storage_records (cabinet_id, compartment_number, user_phone, access_code, status, store_time, retrieve_time) VALUES (%s, %s, %s, %s, 1, %s, %s)',
                       (order['cabinet_id'], order['compartment_number'], order['user_phone'], order['access_code'], order['store_time'], datetime.now()))
        # 根据网点退款模式处理
        cursor.execute('SELECT l.withdraw_mode, l.show_refunding_status FROM cabinets c JOIN locations l ON c.location_id = l.id WHERE c.id = %s', (order['cabinet_id'],))
        loc_row = cursor.fetchone()
        withdraw_mode = loc_row['withdraw_mode'] if loc_row else 'auto_approve'
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        if withdraw_mode == 'auto_approve':
            # 自动审批：立即真退款
            from helpers import do_real_refund
            refund_success, refund_id, refund_msg = do_real_refund(order_id=order_id, amount=refund_amount)
            if refund_success:
                new_status = 4  # 已退款
                cursor.execute('UPDATE orders SET status = %s, refund_id = %s, refund_time = %s WHERE id = %s', (new_status, refund_id, datetime.now(), order_id))
                if refund_amount > 0:
                    cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)', (order_id, refund_amount, refund_id))
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 2, \"system\", %s)',
                               (order_id, order['user_phone'], refund_amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            else:
                new_status = 6  # 退款异常
                cursor.execute('UPDATE orders SET status = %s WHERE id = %s', (new_status, order_id))
                if refund_amount > 0:
                    cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 0)', (order_id, refund_amount, ''))
                cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status, auto_approve_time) VALUES (%s, %s, %s, 1, %s)',
                               (order_id, order['user_phone'], refund_amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        else:
            # 人工/队列审批：标记已结算，创建待审核提现记录
            new_status = 3  # 已结算(退款中)
            cursor.execute('UPDATE orders SET status = %s WHERE id = %s', (new_status, order_id))
            cursor.execute('INSERT INTO withdrawal_records (order_id, user_phone, amount, status) VALUES (%s, %s, %s, 0)',
                           (order_id, order['user_phone'], refund_amount))
        conn.commit()
        conn.close()"""

if old_refund_block in content:
    content = content.replace(old_refund_block, new_refund_block, 1)
    print('Fixed deposit_end_storage refund logic')
else:
    print('WARNING: deposit_end_storage refund block not found exactly')
    # Try to find the key markers
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if "refund_amount = order['deposit_amount']" in line and i > 600 and i < 750:
            print(f'Found refund_amount at line {i+1}')
        if "new_status = 4 if refund_success else 6" in line and i > 600 and i < 750:
            print(f'Found new_status at line {i+1}')

# Fix the return messages for deposit_end_storage
old_return_success = """        if refund_success:
            return json_response({'message': '取物完成，保证金已退还', 'order_id': order_id, 'refund_amount': refund_amount, 'refund_id': refund_id, 'compartment_number': compartment_number})
        return json_response({'message': '取物完成，退款处理中', 'order_id': order_id, 'refund_amount': 0, 'refund_status': 'pending', 'compartment_number': compartment_number}, code=200)"""

new_return = """        if withdraw_mode == 'auto_approve' and new_status == 4:
            return json_response({'message': '取物完成，保证金已退还', 'order_id': order_id, 'refund_amount': refund_amount, 'refund_id': refund_id, 'compartment_number': compartment_number})
        elif withdraw_mode == 'auto_approve' and new_status == 6:
            return json_response({'message': '取物完成，退款异常', 'order_id': order_id, 'refund_amount': 0, 'compartment_number': compartment_number})
        else:
            return json_response({'message': '取物完成，退款审批中', 'order_id': order_id, 'refund_amount': 0, 'refund_status': 'pending', 'compartment_number': compartment_number})"""

if old_return_success in content:
    content = content.replace(old_return_success, new_return, 1)
    print('Fixed deposit_end_storage return messages')
else:
    print('WARNING: deposit_end_storage return block not found exactly')

with open('routes/user.py', 'w') as f:
    f.write(content)
print('routes/user.py saved')
