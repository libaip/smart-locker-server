# -*- coding: utf-8 -*-
"""
[FIX-20260913] 清理"公众号事件被当成投诉"造成的数据污染

背景：webhook 曾把公众号收到的任何消息/事件都存成投诉；近 7 天 550 条"投诉"里
真正用户主动发文字的只有 51 条，其余是点菜单(view_miniprogram)、订阅事件等。
其中 391 条触发了"投诉自动原路退款"，还让 589 人被加进"当天投诉白名单"。

本脚本做三件事：
  1) 导出记录（不删不改）：被这类假投诉自动退款的订单清单
  2) 把假投诉标记作废（complaint_type='event_ignore', status='9', reply 前缀说明）
  3) 删掉"由假投诉产生"的白名单条目（该 unionid 有假投诉、且没有任何真投诉）

判定"假投诉"：source='wechat_mp' 且 它关联的公众号消息全部是事件(msg_type='event')
判定"真投诉"：source='wechat_mp' 且 关联里有非事件消息(用户真发的文字/图片/语音)

用法：python3 cleanup_fake_complaints.py            # 干跑，只报数
      python3 cleanup_fake_complaints.py --real     # 真做（先写备份文件）
"""
import os
import sys
import csv
import time

DSN = 'postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker'
BK = '/home/ubuntu/smart-locker/backups/fakecmp_' + time.strftime('%Y%m%d_%H%M%S')
REAL = '--real' in sys.argv

FAKE = """c.source = 'wechat_mp'
  AND EXISTS (SELECT 1 FROM wx_oa_messages m WHERE m.complaint_id = c.id)
  AND NOT EXISTS (SELECT 1 FROM wx_oa_messages m WHERE m.complaint_id = c.id AND m.msg_type <> 'event')"""
REAL_C = """c.source = 'wechat_mp'
  AND EXISTS (SELECT 1 FROM wx_oa_messages m WHERE m.complaint_id = c.id AND m.msg_type <> 'event')"""

WL_FAKE_ONLY = """w.source = 'complaint'
  AND coalesce(w.unionid,'') <> ''
  AND EXISTS (SELECT 1 FROM users u WHERE u.unionid = w.unionid
              AND EXISTS (SELECT 1 FROM complaints c WHERE c.user_phone = u.phone AND %s))
  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.unionid = w.unionid
              AND EXISTS (SELECT 1 FROM complaints c WHERE c.user_phone = u.phone AND %s))""" % (FAKE, REAL_C)


def main():
    import psycopg2
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()

    def one(sql, args=()):
        cur.execute(sql, args)
        return cur.fetchone()[0]

    print('=' * 72)
    print('[清理假投诉] 模式：%s' % ('真做(--real)' if REAL else '干跑'))
    print('=' * 72)

    n_fake = one("SELECT count(*) FROM complaints c WHERE " + FAKE)
    n_fake_today = one("SELECT count(*) FROM complaints c WHERE " + FAKE + " AND c.created_at::date = current_date")
    n_fake7 = one("SELECT count(*) FROM complaints c WHERE " + FAKE + " AND c.created_at > current_date - 7")
    n_real = one("SELECT count(*) FROM complaints c WHERE " + REAL_C)
    n_wl = one("SELECT count(*) FROM withdrawal_whitelist w WHERE " + WL_FAKE_ONLY)
    n_wl_today = one("SELECT count(*) FROM withdrawal_whitelist w WHERE " + WL_FAKE_ONLY + " AND w.created_at::date = current_date")

    print('\n【要处理的东西】')
    print('  假投诉（公众号事件被当投诉）: 共 %d 条（今天 %d，近7天 %d）' % (n_fake, n_fake_today, n_fake7))
    print('  真留言（用户主动发的消息）  : %d 条（这些一律不动）' % n_real)
    print('  由假投诉产生、且该身份没有真投诉的白名单条目: %d 条（今天 %d）' % (n_wl, n_wl_today))

    print('\n【按事件类型看这些假投诉的来源】')
    cur.execute("""SELECT coalesce(m.event,'(无)') AS ev, count(DISTINCT c.id)
                   FROM complaints c JOIN wx_oa_messages m ON m.complaint_id = c.id
                   WHERE %s GROUP BY 1 ORDER BY 2 DESC LIMIT 8""" % FAKE.replace('c.source', 'c.source'))
    for ev, cnt in cur.fetchall():
        print('    %-32s %d 条' % (ev, cnt))

    print('\n【被这类假投诉自动退款的订单（只导出记录，钱已经退了、不动它）】')
    cur.execute("""SELECT o.order_no, o.user_phone, o.refund_amount, o.refund_time, c.id AS complaint_id, c.reply
                   FROM orders o JOIN complaints c ON c.order_no = o.order_no
                   WHERE %s AND o.refund_status = 'refunded'
                   ORDER BY o.refund_time DESC""" % FAKE.replace('c.source', 'c.source'))
    rows = cur.fetchall()
    total = sum(float(r[2] or 0) for r in rows)
    print('  共 %d 笔，合计 %.2f 元' % (len(rows), total))
    for r in rows[:5]:
        print('    %s  手机=%s  金额=%s  退款时间=%s' % (r[0], r[1], r[2], r[3]))

    print('\n【样本：要作废的假投诉（前 5 条）】')
    cur.execute("SELECT c.id, c.created_at, c.user_phone, c.status, left(coalesce(c.reply,''),20) FROM complaints c WHERE %s ORDER BY c.id DESC LIMIT 5" % FAKE)
    for r in cur.fetchall():
        print('    id=%s %s phone=%s status=%s reply=%s' % r)

    if not REAL:
        print('\n干跑结束：没有改任何数据。加 --real 才执行。')
        conn.close()
        return 0

    # ---------- 备份 ----------
    os.makedirs(BK, exist_ok=True)
    cur.execute("SELECT id, user_phone, type, content, order_no, status, reply, reply_time, source, complaint_type, created_at FROM complaints c WHERE " + FAKE)
    with open(os.path.join(BK, 'complaints_to_void.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['id', 'user_phone', 'type', 'content', 'order_no', 'status', 'reply', 'reply_time', 'source', 'complaint_type', 'created_at'])
        w.writerows(cur.fetchall())
    cur.execute("SELECT openid, source, remain_count, created_at, unionid, expires_at FROM withdrawal_whitelist w WHERE " + WL_FAKE_ONLY)
    with open(os.path.join(BK, 'whitelist_to_delete.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['openid', 'source', 'remain_count', 'created_at', 'unionid', 'expires_at'])
        w.writerows(cur.fetchall())
    with open(os.path.join(BK, 'refunded_orders.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['order_no', 'user_phone', 'refund_amount', 'refund_time', 'complaint_id', 'reply'])
        w.writerows(rows)
    print('\n备份已写入 %s（3 个 CSV）' % BK)

    # ---------- 1) 作废假投诉 ----------
    cur.execute("""UPDATE complaints c SET complaint_type = 'event_ignore', status = '9',
                   reply = ('[系统标记·公众号事件非投诉] ' || coalesce(reply,'')) WHERE """ + FAKE)
    print('  已标记作废的假投诉: %d 条' % cur.rowcount)
    # ---------- 2) 删白名单 ----------
    cur.execute("DELETE FROM withdrawal_whitelist w WHERE " + WL_FAKE_ONLY)
    print('  已删除的假白名单条目: %d 条' % cur.rowcount)
    conn.commit()

    # ---------- 验证 ----------
    left = one("SELECT count(*) FROM complaints c WHERE " + FAKE)
    left_wl = one("SELECT count(*) FROM withdrawal_whitelist w WHERE " + WL_FAKE_ONLY)
    print('\n【验证】剩余未处理的假投诉: %d（应为 0）；剩余假白名单: %d（应为 0）' % (left, left_wl))
    cur.execute("SELECT status, count(*) FROM complaints GROUP BY 1 ORDER BY 1")
    print('【现在投诉表的状态分布】')
    for st, cnt in cur.fetchall():
        print('    status=%s : %s 条' % (st, cnt))
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
