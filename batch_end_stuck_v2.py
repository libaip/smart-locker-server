#!/usr/bin/env python3
"""批量结束被403/500拦截的卡住订单，退押金到余额，推订阅通知。不开门。"""
import sys, os, traceback, logging
from datetime import datetime

sys.path.insert(0, '/home/ubuntu/smart-locker')
import psycopg2
from psycopg2.extras import RealDictCursor
from config import DATABASE_URL
from helpers import deposit_already_refunded   # [S628-20260923] 押金已原路退款闸门

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger('batch_end_stuck')

TEMPLATE_ID = "5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A"

def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def find_user_openid(cursor, phone, unionid=''):
    try:
        cursor.execute("SELECT mp_openid FROM user_balances WHERE user_phone=%s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (phone,))
        row = cursor.fetchone()
        if row and row.get('mp_openid'):
            mp = row['mp_openid']
            if not mp.startswith('oLhbm2'):
                return mp
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
        logger.error(f'find openid failed phone={phone}: {e}')
    return ''

def send_notification(phone, openid, deposit_amount, order_id):
    if not openid:
        logger.warning(f'order {order_id} no openid, skip notify')
        return False
    try:
        from helpers import send_wx_subscribe_message
        subscribe_data = {
            "amount6": {"value": "Y%.2f" % float(deposit_amount or 0)},
            "time4": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")},
            "thing7": {"value": "already refund to wallet"},
            "thing2": {"value": "go to mine wallet withdraw"}
        }
        # use the real chinese template from the original code
        subscribe_data = {
            "amount6": {"value": ("\u00a5%.2f" % float(deposit_amount or 0))},
            "time4": {"value": datetime.now().strftime("%Y-%m-%d %H:%M")},
            "thing7": {"value": "\u5df2\u9000\u8fd8\u81f3\u5c0f\u7a0b\u5e8f\u7528\u6237\u94b1\u5305"},
            "thing2": {"value": "\u8bf7\u81ea\u884c\u70b9\u51fb\u6b64\u901a\u77e5\u6d88\u606f\u8df3\u8f6c\u6211\u7684\u94b1\u5305\u63d0\u73b0"}
        }
        send_wx_subscribe_message('', TEMPLATE_ID, subscribe_data, phone=phone, page="pages/mine/mine")
        return True
    except Exception as e:
        logger.error(f'order {order_id} notify failed: {e}')
        return False

def main():
    dry_run = '--dry-run' in sys.argv
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT o.id, o.order_no, o.user_phone, o.cabinet_id, o.compartment_number,
               o.slot_id, o.deposit_amount, o.openid, o.unionid, o.mp_openid,
               o.store_time, o.user_id, o.refund_status, o.refund_amount
        FROM orders o
        WHERE o.status = 2
          AND o.store_time < NOW() - INTERVAL '10 minutes'
        ORDER BY o.store_time ASC
    """)
    orders = cursor.fetchall()
    logger.info(f'found {len(orders)} stuck orders (status=2, >10min)')

    if dry_run:
        by_age = {'over_1day': 0, 'over_1hour': 0, 'over_10min': 0}
        for o in orders:
            mins = (datetime.now() - o['store_time']).total_seconds() / 60
            if mins > 1440: by_age['over_1day'] += 1
            elif mins > 60: by_age['over_1hour'] += 1
            else: by_age['over_10min'] += 1
        logger.info(f'by age: {by_age}')
        for o in orders[:3]:
            logger.info(f"  sample: id={o['id']} phone={o['user_phone']} cab={o['cabinet_id']} slot={o['compartment_number']} deposit={o['deposit_amount']} time={o['store_time']}")
        conn.close()
        return

    # backup
    logger.info('backing up stuck orders...')
    cursor.execute("DROP TABLE IF EXISTS stuck_orders_batch_end_20260807_v2")
    cursor.execute("""
        CREATE TABLE stuck_orders_batch_end_20260807_v2 AS
        SELECT o.* FROM orders o
        WHERE o.status = 2 AND o.store_time < NOW() - INTERVAL '10 minutes'
    """)
    conn.commit()
    logger.info('backup done')

    success = 0
    fail = 0
    notified = 0
    no_openid_count = 0

    for o in orders:
        order_id = o['id']
        try:
            # 1. release slot (NO door open)
            if o['slot_id']:
                cursor.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s AND status=2", (o['slot_id'],))

            # 2. end order
            cursor.execute("""
                UPDATE orders SET status=3, retrieve_time=NOW(), refund_mark=1, logical_mark='end'
                WHERE id=%s
            """, (order_id,))

            # 3. refund deposit to balance
            deposit = o['deposit_amount'] or 0
            phone = o['user_phone'] or ''
            openid = o.get('openid') or ''
            unionid = o.get('unionid') or ''
            mp_openid = o.get('mp_openid') or ''
            user_id = o.get('user_id') or 0

            # [S628-20260923] 已原路退款的订单不再把押金计入余额
            if deposit > 0 and phone and not deposit_already_refunded(o):
                cursor.execute("""
                    INSERT INTO user_balances (phone, openid, unionid, mp_openid, balance, total_deposited, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (phone) DO UPDATE SET
                        balance = user_balances.balance + %s,
                        total_deposited = user_balances.total_deposited + %s
                """, (phone, openid, unionid, mp_openid, deposit, deposit, user_id, deposit, deposit))
                cursor.execute("""
                    INSERT INTO user_balance_details (user_phone, order_id, amount, status)
                    VALUES (%s, %s, %s, 'available')
                    ON CONFLICT (order_id) DO NOTHING
                """, (phone, order_id, deposit))

            conn.commit()
            success += 1

            # 4. send notify (no door open)
            notify_openid = mp_openid or openid
            if not notify_openid:
                notify_openid = find_user_openid(cursor, phone, unionid)
            if notify_openid:
                if send_notification(phone, notify_openid, deposit, order_id):
                    notified += 1
            else:
                no_openid_count += 1

            if success % 50 == 0:
                logger.info(f'progress: {success}/{len(orders)} done, {notified} notified')

        except Exception as e:
            logger.error(f'order {order_id} failed: {e}')
            conn.rollback()
            fail += 1

    logger.info(f'=== DONE === success={success} fail={fail} notified={notified} no_openid={no_openid_count}')
    conn.close()

if __name__ == '__main__':
    main()
