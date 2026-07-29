#!/usr/bin/env python3
"""批量退款：处理 device.py 崩溃期间（02:42-08:58 北京时间）异常结算的订单"""
import sys, os, json, time
sys.path.insert(0, '/home/ubuntu/smart-locker')
import psycopg2, psycopg2.extras
from datetime import datetime
from helpers import do_real_refund

DB_CFG = {"host":"127.0.0.1","user":"locker_admin","password":"locker_pass_2024","dbname":"smart_locker"}

def main():
    conn = psycopg2.connect(**DB_CFG)
    conn.autocommit = False
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    # 查询受影响订单：故障窗口内 status=3, refund_mark=1, refund_status='none'
    cursor.execute("""
        SELECT id, order_no, user_id, user_phone, deposit_amount, payment_channel_id, 
               cabinet_id, compartment_number, created_at
        FROM orders 
        WHERE created_at >= '2026-07-28 18:42:00' 
          AND created_at <= '2026-07-29 00:58:00'
          AND refund_mark = 1 
          AND refund_status = 'none'
          AND status = 3
        ORDER BY created_at
    """)
    orders = cursor.fetchall()
    total = len(orders)
    total_amount = sum(o['deposit_amount'] for o in orders)
    print(f'共找到 {total} 笔待退款订单，总金额 {total_amount} 元')
    print('='*60)
    
    success_count = 0
    fail_count = 0
    success_amount = 0
    fail_amount = 0
    results = []
    
    for i, order in enumerate(orders):
        order_id = order['id']
        order_no = order['order_no']
        amount = order['deposit_amount']
        phone = order['user_phone']
        
        print(f'[{i+1}/{total}] 订单 {order_no} | 用户 {phone} | 金额 {amount}元 ... ', end='', flush=True)
        
        try:
            success, refund_id, refund_msg, _ = do_real_refund(
                order_id=order_id,
                order_no=order_no,
                amount=amount,
                payment_channel_id=order['payment_channel_id']
            )
            
            if success:
                # 更新订单状态
                cursor.execute(
                    "UPDATE orders SET status = 4, refund_status = 'refunded', refund_id = %s, refund_time = %s WHERE id = %s",
                    (refund_id, datetime.now(), order_id)
                )
                # 释放格口
                cursor.execute("SELECT slot_id FROM orders WHERE id = %s", (order_id,))
                slot_row = cursor.fetchone()
                if slot_row and slot_row.get('slot_id'):
                    cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (slot_row['slot_id'],))
                # 记录支付记录
                cursor.execute(
                    "INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)",
                    (order_id, amount, refund_id)
                )
                # 记录提现记录
                cursor.execute(
                    "INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 2, 'system', %s)",
                    (order_id, phone, amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                )
                conn.commit()
                success_count += 1
                success_amount += amount
                print(f'OK (refund_id={refund_id})')
                results.append({'order_no': order_no, 'phone': phone, 'amount': amount, 'status': 'success', 'refund_id': refund_id})
            else:
                # 退款失败
                cursor.execute(
                    "UPDATE orders SET status = 6, refund_id = %s, refund_time = %s WHERE id = %s",
                    ('FAIL:' + str(refund_msg)[:50], datetime.now(), order_id)
                )
                cursor.execute(
                    "INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 1, 'system', %s)",
                    (order_id, phone, amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                )
                conn.commit()
                fail_count += 1
                fail_amount += amount
                print(f'FAIL: {refund_msg}')
                results.append({'order_no': order_no, 'phone': phone, 'amount': amount, 'status': 'failed', 'error': refund_msg})
        except Exception as e:
            conn.rollback()
            fail_count += 1
            fail_amount += amount
            print(f'ERROR: {e}')
            results.append({'order_no': order_no, 'phone': phone, 'amount': amount, 'status': 'error', 'error': str(e)})
        
        # 每笔之间稍微间隔，避免触发微信限流
        time.sleep(0.5)
    
    print()
    print('='*60)
    print(f'退款完成！')
    print(f'  成功: {success_count} 笔, {success_amount} 元')
    print(f'  失败: {fail_count} 笔, {fail_amount} 元')
    print(f'  总计: {total} 笔, {total_amount} 元')
    
    # 保存结果到文件
    result_file = '/home/ubuntu/smart-locker/refund_batch_result_20260729.json'
    with open(result_file, 'w') as f:
        json.dump({
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total': total,
            'success_count': success_count,
            'fail_count': fail_count,
            'success_amount': success_amount,
            'fail_amount': fail_amount,
            'results': results
        }, f, ensure_ascii=False, indent=2)
    print(f'结果已保存到: {result_file}')
    
    conn.close()

if __name__ == '__main__':
    main()
