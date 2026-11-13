#!/usr/bin/env python3
"""批量结束被403/500拦截的卡住订单，退押金到余额，推订阅通知。"""
import sys, os, traceback, logging
from datetime import datetime

sys.path.insert(0, '/home/ubuntu/smart-locker')
import psycopg2
from psycopg2.extras import RealDictCursor
from config import DATABASE_URL

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger('batch_end_stuck')

# 模板ID：押金退还通知
TEMPLATE_ID = "5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A"

def get_conn():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    return conn

def find_user_openid(cursor, phone, unionid=''):
    """查找用户的小程序openid用于发订阅消息"""
    try:
        # 先查 user_balances
        cursor.execute("SELECT mp_openid FROM user_balances WHERE user_phone=%s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (phone,))
        row = cursor.fetchone()
        if row and row.get('mp_openid'):
            mp = row['mp_openid']
            if not mp.startswith('oLhbm2'):  # 排除公众号openid
                return mp
        # 查 phone_openids
        if unionid:
            cursor.execute("SELECT mp_openid, openid FROM phone_openids WHERE phone=%s AND unionid=%s LIMIT 2", (phone, unionid))
        else:
            cursor.execute("SELECT mp_openid, openid FROM phone_openids WHERE phone=%s LIMIT 2", (phone,))
        rows = cursor.fetchall()
        if len(rows) == 1:
            return rows[0].get('mp_openid') or rows[0].get('openid') or ''
        for r in rows:
            mp = r.get('mp_openid') or ''
            if mp and not mp.startswith('oLhbm2'):
                return mp
    except Exception as e:
        logger.error(f'查找openid失败 phone={phone}: {e}')
    return ''

def send_notification(order_id, phone, openid, deposit_amount):
    """发送押金退还订阅消息"""
    if not openid:
        logger.warning(f'订单{order_id} 无openid，跳过通知')
        return False
    try:
        from helpers import send_wx_subscribe_message
        subscribe_data = {
            "amount6": {"value": "¥{:.2f}".format(float(deposit_amount or 0))},
            "time4": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")},
            "thing7": {"value": "已退还至小程序用户钱包"},
            "thing2": {"value": "请自行点击此通知消息跳转"我的钱包"提现"}
        }
        send_wx_subscribe_message('', TEMPLATE_ID, subscribe_data, phone=phone, page="pages/mine/mine")
        return True
    except Exception as e:
        logger.error(f'订单{order_id} 发送订阅消息失败: {e}')
        return False

def main():
    dry_run = '--dry-run' in sys.argv
    
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    # 查找所有卡住的订单：status=2 且 store_time > 10分钟前
    cursor.execute("""
        SELECT o.id, o.order_no, o.user_phone, o.cabinet_id, o.compartment_number,
               o.slot_id, o.deposit_amount, o.openid, o.unionid, o.mp_openid,
               o.store_time, o.user_id,
               cs.board_no, cs.lock_no, c.mainboard_device_id, c.mainboard_source
        FROM orders o
        JOIN cabinet_slots cs ON o.slot_id = cs.id
        JOIN cabinets c ON o.cabinet_id = c.id
        WHERE o.status = 2
          AND o.store_time < NOW() - INTERVAL '10 minutes'
        ORDER BY o.store_time ASC
    """)
    orders = cursor.fetchall()
    logger.info(f'找到 {len(orders)} 个卡住订单（status=2, 超过10分钟）')
    
    if dry_run:
        for o in orders[:5]:
            logger.info(f"  [DRY-RUN] 订单{o['id']} phone={o['user_phone']} cabinet={o['cabinet_id']} slot={o['compartment_number']} deposit={o['deposit_amount']} store_time={o['store_time']}")
        logger.info(f'  ... 共{len(orders)}个')
        conn.close()
        return
    
    # 备份
    logger.info('备份卡住的订单...')
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stuck_orders_batch_end_20260807 AS
        SELECT o.* FROM orders o
        WHERE o.status = 2 AND o.store_time < NOW() - INTERVAL '10 minutes'
    """)
    conn.commit()
    logger.info('备份完成')
    
    success = 0
    fail = 0
    notified = 0
    
    for o in orders:
        order_id = o['id']
        try:
            # 1. 释放柜格
            if o['slot_id']:
                cursor.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s AND status=2", (o['slot_id'],))
            
            # 2. 结束订单
            cursor.execute("""
                UPDATE orders SET status=3, retrieve_time=NOW(), refund_mark=1, logical_mark='end'
                WHERE id=%s
            """, (order_id,))
            
            # 3. 退押金到余额
            deposit = o['deposit_amount'] or 0
            phone = o['user_phone'] or ''
            openid = o.get('openid') or ''
            unionid = o.get('unionid') or ''
            mp_openid = o.get('mp_openid') or ''
            user_id = o.get('user_id') or 0
            
            if deposit > 0 and phone:
                # upsert user_balances
                cursor.execute("""
                    INSERT INTO user_balances (user_phone, openid, unionid, mp_openid, balance, total_deposited, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_phone) DO UPDATE SET 
                        balance = user_balances.balance + %s,
                        total_deposited = user_balances.total_deposited + %s
                """, (phone, openid, unionid, mp_openid, deposit, deposit, user_id, deposit, deposit))
                
                # 写入余额明细
                cursor.execute("""
                    INSERT INTO user_balance_details (user_phone, order_id, amount, status)
                    VALUES (%s, %s, %s, 'available')
                    ON CONFLICT (order_id) DO NOTHING
                """, (phone, order_id, deposit))
            
            conn.commit()
            success += 1
            
            # 4. 发送订阅通知
            notify_openid = mp_openid or openid
            if not notify_openid:
                notify_openid = find_user_openid(cursor, phone, unionid)
            
            if notify_openid:
                if send_notification(order_id, phone, notify_openid, deposit):
                    notified += 1
            else:
                logger.warning(f'订单{order_id} phone={phone} 无openid可发通知')
            
            if success % 50 == 0:
                logger.info(f'进度: {success}/{len(orders)} 已完成, {notified} 已通知')
                
        except Exception as e:
            logger.error(f'订单{order_id} 处理失败: {e}')
            conn.rollback()
            fail += 1
    
    logger.info(f'=== 完成 === 成功: {success}, 失败: {fail}, 通知: {notified}/{success}')
    conn.close()

if __name__ == '__main__':
    main()
