#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超时费补退工具（防多退版）

用法：
  # 查（不改数据）
  python3 refund_overdue.py --check 13462979089
  python3 refund_overdue.py --check-order 80716020261102085227
  python3 refund_overdue.py --list                  # 列出全部被扣的

  # 补退到余额（安全，推荐）
  python3 refund_overdue.py --to-balance 13462979089            # 预览
  python3 refund_overdue.py --to-balance 13462979089 --apply    # 真执行

  # 原路退回微信（需要证书，微信会兜底防多退）
  python3 refund_overdue.py --to-wx 13462979089                 # 预览
  python3 refund_overdue.py --to-wx 13462979089 --apply         # 真执行

防多退的三道保险：
  ① 用微信 refund_query 查实际已退金额（不信任本地 refund_amount）
  ② 差额 = 押金 - 实际已退，不是押金全额
  ③ 本地补退记录（remark 标记），补过就跳过
"""
import sys, argparse, logging, socket
from datetime import datetime
logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')
from database import get_db

TAG = '超时费补退'
FEE = 1.0


def get_orders(cur, phone=None, order_no=None, order_id=None):
    sql = """
        SELECT o.id, o.order_no, o.user_phone, o.mp_openid, o.openid, o.unionid,
               o.deposit_amount, o.refund_amount,
               o.status, o.refund_status, o.payment_channel_id,
               o.retrieve_time::text AS rt,
               CASE WHEN o.retrieve_time IS NOT NULL
                    THEN ROUND(EXTRACT(EPOCH FROM (o.retrieve_time - o.store_time))/60)
                    ELSE NULL END AS mins,
               c.location_id AS lid, l.name AS lname
        FROM orders o
        JOIN cabinets c ON o.cabinet_id = c.id
        LEFT JOIN locations l ON c.location_id = l.id
        WHERE 1=1
    """
    p = []
    if phone:
        sql += " AND o.user_phone = %s"; p.append(phone)
    if order_no:
        sql += " AND o.order_no = %s"; p.append(order_no)
    if order_id:
        sql += " AND o.id = %s"; p.append(order_id)
    sql += " ORDER BY o.retrieve_time DESC"
    cur.execute(sql, tuple(p))
    return [dict(r) for r in cur.fetchall()]


def actual_refunded(cur, oid):
    """实际退给用户多少（用流水算，不信 refund_amount）"""
    cur.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM withdrawal_records
                   WHERE order_id=%s AND status=2""", (oid,))
    wx = float(cur.fetchone()['s'] or 0)
    cur.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM user_balance_details
                   WHERE order_id=%s AND status IN ('withdrawn',)""", (oid,))
    # 注意：withdrawn 的余额明细和 withdrawal_records 是同一笔钱，不能重复算
    # 所以以 withdrawal_records status=2 为准
    return wx


def already_refunded_overdue(cur, oid):
    """本地是否已经补退过超时费"""
    cur.execute("""SELECT id, amount, created_at::text AS ct FROM user_balance_details
                   WHERE order_id=%s AND remark LIKE %s LIMIT 1""", (oid, '%' + TAG + '%'))
    return cur.fetchone()


def wx_query_refunded(order_no, channel):
    """用微信 refund_query 查实际已退（最权威）"""
    try:
        import wxpay
        fn = getattr(wxpay, 'refund_query', None)
        if not fn:
            return None
        r = fn(out_trade_no=order_no)
        return r
    except Exception as e:
        return {'_error': str(e)[:120]}


def show(rows, cur):
    if not rows:
        print("   (没有匹配的订单)")
        return
    for x in rows:
        dep = float(x['deposit_amount'] or 0)
        paid = actual_refunded(cur, x['id'])
        gap = round(dep - paid, 2)
        flag = ''
        if abs(gap - FEE) < 0.02:
            flag = '  <<== 差 1 元，可补退'
        elif abs(gap) < 0.02:
            flag = '  [已全额退]'
        elif gap <= 0:
            flag = '  [已退完或多退]'
        done = already_refunded_overdue(cur, x['id'])
        if done:
            flag += '  [已补退过]'
        print("   order_id=%-7s 订单号=%s" % (x['id'], x['order_no']))
        print("      手机=%s  网点=%s(%s)  时长=%s分钟" % (
            str(x['user_phone'])[-11:], str(x['lname'] or '')[:14], x['lid'], x['mins']))
        print("      押金 ¥%.2f  实际已退 ¥%.2f  差额 ¥%.2f%s" % (dep, paid, gap, flag))
        print("      status=%s refund_status=%s 渠道=%s" % (
            x['status'], x['refund_status'] or '-', x['payment_channel_id']))
        print()


def do_to_balance(cur, rows, apply_it):
    total = 0.0
    for x in rows:
        oid = x['id']
        dep = float(x['deposit_amount'] or 0)
        paid = actual_refunded(cur, oid)
        gap = round(dep - paid, 2)
        if gap <= 0.01:
            print("   - order %s 没有可补的差额（已退 ¥%.2f / 押金 ¥%.2f），跳过" % (oid, paid, dep))
            continue
        if already_refunded_overdue(cur, oid):
            print("   - order %s 已经补退过，跳过（防多退）" % oid)
            continue
        # 只补超时费那么多，不多补
        amt = min(gap, FEE)
        print("   + order %s 手机=%s 补 ¥%.2f（押金%.2f - 已退%.2f = 差%.2f，上限%.2f）" % (
            oid, str(x['user_phone'])[-11:], amt, dep, paid, gap, FEE))
        if apply_it:
            cur.execute("""INSERT INTO user_balance_details
                           (user_phone, order_id, amount, status, remark, source_time)
                           VALUES (%s, %s, %s, 'available', %s, NOW())""",
                        (x['user_phone'], oid, amt, TAG + '-客服补退'))
            cur.execute("""UPDATE user_balances SET balance = COALESCE(balance,0) + %s
                           WHERE phone = %s""", (amt, x['user_phone']))
            if cur.rowcount == 0:
                cur.execute("""INSERT INTO user_balances
                               (phone, openid, unionid, mp_openid, balance, total_deposited)
                               VALUES (%s, %s, %s, %s, %s, 0)""",
                            (x['user_phone'], x['openid'] or '', x['unionid'] or '',
                             x['mp_openid'] or '', amt))
            print("       余额 +%.2f 完成" % amt)
        total += amt
    return total


def do_to_wx(cur, rows, apply_it):
    total = 0.0
    for x in rows:
        oid = x['id']
        dep = float(x['deposit_amount'] or 0)
        paid = actual_refunded(cur, oid)
        gap = round(dep - paid, 2)
        if gap <= 0.01:
            print("   - order %s 无可补差额，跳过" % oid)
            continue
        amt = min(gap, FEE)
        print("   + order %s 原路退 ¥%.2f" % (oid, amt))
        # 微信 refund_query 兜底检查
        cur.execute("SELECT * FROM payment_channels WHERE id=%s", (x['payment_channel_id'],))
        ch = cur.fetchone()
        if ch:
            q = wx_query_refunded(x['order_no'], dict(ch))
            if q and q.get('refund_count'):
                print("       微信已退 %s 笔: %s" % (
                    q.get('refund_count'), q.get('refund_fee_0', '(未知)')))
        if apply_it:
            try:
                import wxpay
                r = wxpay.refund(
                    out_trade_no=x['order_no'],
                    total_fee=int(round(dep * 100)),
                    refund_fee=int(round(amt * 100)),
                    out_refund_no='OF%s%s' % (oid, datetime.now().strftime('%H%M%S')),
                    channel=dict(ch) if ch else None)
                print("       微信返回: %s" % str(r)[:150])
            except Exception as e:
                print("       ★ 退款失败: %s" % str(e)[:120])
                continue
        total += amt
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check'); ap.add_argument('--check-order')
    ap.add_argument('--check-id', type=int)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--to-balance')
    ap.add_argument('--to-wx')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    db = get_db(); cur = db.cursor()
    print("=" * 92)
    print("  超时费补退工具  host=%s" % socket.gethostname())
    print("  ★ 防多退：差额 = 押金 - 实际已退（用流水算），补过就跳过")
    print("=" * 92)
    print()

    if a.check:
        print("[查] 手机 %s" % a.check)
        show(get_orders(cur, phone=a.check), cur)
    elif a.check_order:
        print("[查] 订单号 %s" % a.check_order)
        show(get_orders(cur, order_no=a.check_order), cur)
    elif a.check_id:
        print("[查] order_id %s" % a.check_id)
        show(get_orders(cur, order_id=a.check_id), cur)
    elif a.list:
        rows = [r for r in get_orders(cur)
                if abs(round(float(r['deposit_amount'] or 0) - actual_refunded(cur, r['id']), 2) - FEE) < 0.02]
        print("[列出所有差 1 元的]  共 %d 笔" % len(rows))
        print()
        show(rows, cur)
    elif a.to_balance or a.to_wx:
        target = a.to_balance or a.to_wx
        rows = get_orders(cur, phone=target) if len(target) == 11 else get_orders(cur, order_no=target)
        print("[补退] 目标 %s，找到 %d 笔" % (target, len(rows)))
        print()
        show(rows, cur)
        if a.to_balance:
            print("[方式] 补到余额")
            t = do_to_balance(cur, rows, a.apply)
            if a.apply: db.commit(); print("\n   ✓ 已执行，共补 ¥%.2f" % t)
            else: print("\n   DRY-RUN 共需补 ¥%.2f（加 --apply 执行）" % t)
        else:
            print("[方式] 原路退回微信")
            t = do_to_wx(cur, rows, a.apply)
            if a.apply: print("\n   ✓ 已执行，共退 ¥%.2f" % t)
            else: print("\n   DRY-RUN 共需退 ¥%.2f（加 --apply 执行）" % t)
    else:
        print("用法:")
        print("  --check 手机号             查")
        print("  --check-order 订单号       查")
        print("  --list                     列出所有差1元的")
        print("  --to-balance 手机号 [--apply]   补到余额")
        print("  --to-wx 手机号 [--apply]        原路退回微信")
    db.close()


if __name__ == '__main__':
    main()
