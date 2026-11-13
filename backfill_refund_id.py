#!/usr/bin/env python3
"""存量 refund_id 回填：从微信查询真实退款单号写入 orders.refund_id
- 处理 payments 有 RF 商户单号(存量) 或 完全无流水但微信有退款的订单
- 只回填微信侧 refund_status=SUCCESS 的真实退款单号
- 断点续跑：每处理一笔写进度，中断后从上次位置继续
用法: python3 backfill_refund_id.py            # 从0开始
      python3 backfill_refund_id.py --resume   # 断点续跑
"""
import sys, os, time, json
sys.path.insert(0, "/home/ubuntu/smart-locker")
import psycopg2, psycopg2.extras
from helpers import get_channel_wxpay

DB_CFG = {"host":"127.0.0.1","port":6432,"user":"locker_admin","password":"locker_pass_2024","dbname":"smart_locker"}
PROGRESS_FILE = "/tmp/backfill_refund_id_progress.txt"
LOG_FILE = "/tmp/backfill_refund_id.log"

RESUME = "--resume" in sys.argv

def log(msg):
    line = "%s %s" % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def get_resume_pos():
    if not os.path.exists(PROGRESS_FILE):
        return None
    with open(PROGRESS_FILE) as f:
        content = f.read().strip()
    return content if content else None

def save_progress(order_no):
    with open(PROGRESS_FILE, 'w') as f:
        f.write(order_no)

def main():
    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 取所有 refund_id 为空 且 已退款 的订单（含支付渠道）
    cur.execute("""
        SELECT o.id, o.order_no, o.payment_channel_id, o.refund_time
        FROM orders o
        WHERE o.refund_status IN ('refunded','success')
          AND (o.refund_id IS NULL OR o.refund_id = '')
          AND o.payment_channel_id IS NOT NULL
        ORDER BY o.id
    """)
    rows = cur.fetchall()
    total = len(rows)
    log("待处理订单总数: %d" % total)

    resume_pos = get_resume_pos() if RESUME else None
    if resume_pos:
        log("断点续跑，从订单 %s 之后继续" % resume_pos)

    # 渠道缓存
    channel_cache = {}
    def get_payer(ch_id):
        if ch_id not in channel_cache:
            cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur2.execute("SELECT * FROM payment_channels WHERE id=%s", (ch_id,))
            ch = cur2.fetchone()
            cur2.close()
            if not ch:
                channel_cache[ch_id] = None
                return None
            payer, _ = get_channel_wxpay(dict(ch))
            channel_cache[ch_id] = payer
        return channel_cache[ch_id]

    done = 0
    filled = 0
    skipped = 0
    failed = 0
    start_t = time.time()

    for row in rows:
        oid = row['id']
        ono = row['order_no']
        ch_id = row['payment_channel_id']

        if resume_pos and ono <= resume_pos:
            done += 1
            continue

        payer = get_payer(ch_id)
        if payer is None:
            log("SKIP 无支付渠道 order=%s" % ono)
            skipped += 1
            done += 1
            save_progress(ono)
            continue

        # 查微信退款记录
        try:
            res = payer.refund_query(out_trade_no=ono)
        except Exception as e:
            log("ERR 查询异常 order=%s: %s" % (ono, e))
            failed += 1
            done += 1
            save_progress(ono)
            time.sleep(2)
            continue

        if res.get('return_code') != 'SUCCESS' or res.get('result_code') != 'SUCCESS':
            log("SKIP 微信查询失败 order=%s msg=%s" % (ono, res.get('return_msg') or res.get('err_code_des')))
            skipped += 1
            done += 1
            save_progress(ono)
            continue

        # 取第一条 SUCCESS 退款
        real_rid = ''
        real_status = ''
        cnt = int(res.get('refund_count') or 0)
        for i in range(cnt):
            st = res.get('refund_status_%d' % i) or ''
            rid = res.get('refund_id_%d' % i) or ''
            if st == 'SUCCESS' and rid:
                real_rid = rid
                real_status = st
                break

        if real_rid:
            try:
                uc = conn.cursor()
                uc.execute("UPDATE orders SET refund_id=%s WHERE id=%s AND (refund_id IS NULL OR refund_id='')", (real_rid, oid))
                conn.commit()
                uc.close()
                filled += 1
                log("FILL order=%s rid=%s" % (ono, real_rid))
            except Exception as e:
                conn.rollback()
                log("ERR 更新失败 order=%s: %s" % (ono, e))
                failed += 1
        else:
            log("SKIP 微信无SUCCESS退款 order=%s count=%s" % (ono, cnt))
            skipped += 1

        done += 1
        save_progress(ono)

        # 节流 + 进度
        if done % 20 == 0:
            el = time.time() - start_t
            rate = done / el if el > 0 else 0
            eta = (total - done) / rate / 60 if rate > 0 else 0
            log("进度 %d/%d 已回填%d 跳过%d 失败%d 速率%.1f笔/s 预计剩余%.0f分钟" % (done, total, filled, skipped, failed, rate, eta))
        time.sleep(0.15)

    conn.close()
    log("===== 完成 ===== 总数%d 已回填%d 跳过%d 失败%d 耗时%.0f分钟" % (total, filled, skipped, failed, (time.time()-start_t)/60))

if __name__ == '__main__':
    main()
