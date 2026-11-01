#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[S1114] 行为决策引擎 behavior_decision.py

流程：
  ① 扫描"取包后 6 小时内"的订单
  ② 对每个订单：读行为 → 算分 → 判定
  ③ 记录决策（可审计）
  ④ 灰度模式：只记录，不退款（dry_run=1）
  ⑤ 正式模式：得分>=阈值 → 自动退款（方式一：不管有没有提现）

使用方法：
  python3 behavior_decision.py --dry-run     # 只记录
  python3 behavior_decision.py --apply       # 实际退款
  python3 behavior_decision.py --stats       # 看统计
"""
import sys
import os
import json
import logging
import argparse
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('behavior_decision')

sys.path.insert(0, '/home/ubuntu/smart-locker')
from database import get_db
import behavior_scorer as bs


def to_dt(v):
    """把数据库返回的时间值统一成 datetime（兼容字符串）"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)[:19]
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def get_orders_to_judge(db, window_min=360):
    """
    取需要判定的订单：
      · status=3（已取包未退款）
      · 取包时间在 window_min 内
      · 未退款
      · ★ [S1139] 排除"设备补报"订单：store→retrieve > 12小时
        （设备离线后补报，retrieve_time 实际是补报时刻，不是真实取包时刻，
          用它判定会用错时间窗 -> 可能误退款。正常订单 95% 都在 6 小时内）
    """
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT o.id AS id, o.order_no AS order_no, o.mp_openid AS mp_openid,
                   o.user_phone AS user_phone, o.deposit_amount AS deposit_amount,
                   o.retrieve_time AS retrieve_time, o.status AS status,
                   o.refund_time AS refund_time
            FROM orders o
            WHERE o.status = 3
              AND o.retrieve_time IS NOT NULL
              AND o.retrieve_time >= NOW() - INTERVAL '%s minutes'
              AND o.refund_time IS NULL
              AND COALESCE(o.mp_openid,'') <> ''
              AND o.store_time IS NOT NULL
              AND EXTRACT(EPOCH FROM (o.retrieve_time - o.store_time)) <= 43200
            ORDER BY o.retrieve_time DESC
        """ % int(window_min))
        out = []
        for r in cur.fetchall():
            if isinstance(r, dict):
                d = {k: r.get(k) for k in ('id', 'order_no', 'mp_openid', 'user_phone',
                                           'deposit_amount', 'retrieve_time', 'status',
                                           'refund_time')}
            else:
                d = dict(zip(['id', 'order_no', 'mp_openid', 'user_phone', 'deposit_amount',
                              'retrieve_time', 'status', 'refund_time'], r))
            out.append(d)
        return out
    finally:
        cur.close()


def already_judged(db, order_id):
    """该订单是否已经判定过（避免重复）"""
    cur = db.cursor()
    try:
        cur.execute("""SELECT id AS a, is_high_risk AS b, action AS c FROM behavior_decision_log
                       WHERE order_id=%s ORDER BY id DESC LIMIT 1""", (order_id,))
        r = cur.fetchone()
        if not r:
            return None
        if isinstance(r, dict):
            return {'id': r.get('a'), 'is_high_risk': r.get('b'), 'action': r.get('c')}
        return {'id': r[0], 'is_high_risk': r[1], 'action': r[2]}
    finally:
        cur.close()


def do_refund(db, order, result):
    """
    执行退款（方式一：不管用户有没有提现，直接退）
    复用现有提现/退款表结构
    返回 (refund_id, ok, msg)
    """
    cur = db.cursor()
    try:
        amount = float(order.get('deposit_amount') or 0)
        if amount <= 0:
            return None, False, '押金为0'
        # 幂等：该订单是否已有退款单
        cur.execute("""SELECT id FROM withdrawal_records WHERE order_id=%s
                       AND status IN (0,1,2) LIMIT 1""", (order['id'],))
        ex = cur.fetchone()
        if ex:
            return ex[0], True, '已有退款单 %s' % ex[0]
        # 建退款单（approver 标记来源，便于审计/回滚）
        cur.execute("""
            INSERT INTO withdrawal_records
              (order_id, user_phone, amount, status, click_count, approver, apply_time, openid)
            VALUES (%s, %s, %s, 0, 0, %s, NOW(), %s)
            RETURNING id
        """, (order['id'], order.get('user_phone'), amount,
              '行为规则自动退款', order.get('mp_openid')))
        rid = cur.fetchone()[0]
        db.commit()
        logger.info('[退款] 订单%s 金额%.2f 退款单%s' % (order['order_no'], amount, rid))
        return rid, True, 'ok'
    except Exception as e:
        logger.error('[退款失败] 订单%s: %s' % (order.get('order_no'), e))
        try:
            db.rollback()
        except Exception:
            pass
        return None, False, str(e)
    finally:
        cur.close()


def run(dry_run=True, window_min=360, verbose=False):
    """主流程"""
    db = get_db()
    bs.ensure_tables(db)
    cfg = bs.load_config(db)
    if not cfg['enabled']:
        logger.info('打分功能已关闭（enabled=0）')
        return
    th = cfg['threshold']
    table = cfg['table']
    win = window_min or cfg['window_min']

    orders = get_orders_to_judge(db, win)
    logger.info('待判定订单: %d 笔（窗口 %d 分钟，阈值 %.1f 分，dry_run=%s）' % (
        len(orders), win, th, dry_run))

    n_judged = n_risk = n_refund = 0
    for o in orders:
        o['retrieve_time'] = to_dt(o.get('retrieve_time'))
        t_from = o['retrieve_time']
        t_to = (t_from + timedelta(minutes=win)) if t_from else datetime.now()
        feat = bs.features_from_log(db, o['mp_openid'], t_from, t_to)
        result = bs.judge(feat, table, th)
        n_judged += 1
        if result['is_high_risk']:
            n_risk += 1
        action = 'record_only'
        refund_id = None
        if result['is_high_risk']:
            prev = already_judged(db, o['id'])
            if prev and prev['action'] == 'auto_refund':
                continue    # 已退过，跳过
            if dry_run:
                action = 'record_only'
            else:
                refund_id, ok, msg = do_refund(db, o, result)
                action = 'auto_refund' if ok else 'refund_failed'
                if ok:
                    n_refund += 1
        bs.log_decision(db, o, feat, result, action=action,
                        dry_run=dry_run, refund_id=refund_id, window_min=win)
        if verbose and result['is_high_risk']:
            logger.info('  ★订单%s 得分%.0f → %s | %s' % (
                o['order_no'], result['score'], action, result['reason']))

    logger.info('完成：判定 %d 笔，高风险 %d 笔，实际退款 %d 笔' % (n_judged, n_risk, n_refund))
    print_stats(db)
    db.close()


def print_stats(db):
    """统计"""
    cur = db.cursor()
    try:
        cur.execute("""SELECT COUNT(*) AS 总数,
                              COUNT(*) FILTER (WHERE is_high_risk=1) AS 高风险,
                              COUNT(*) FILTER (WHERE action='auto_refund') AS 已退款,
                              COALESCE(SUM(deposit_amount) FILTER (WHERE is_high_risk=1),0) AS 金额
                       FROM behavior_decision_log WHERE created_at >= NOW() - INTERVAL '14 days'""")
        r = cur.fetchone()
        print('\n  ── 近14天决策统计 ──')
        print('     判定总数: %d 笔' % r[0])
        print('     高风险:   %d 笔 (%.1f%%)' % (r[1], 100.0*r[1]/max(r[0],1)))
        print('     已退款:   %d 笔' % r[2])
        print('     涉及金额: %.2f 元' % r[3])
        cur.execute("""SELECT hits FROM behavior_decision_log
                       WHERE is_high_risk=1 AND created_at >= NOW() - INTERVAL '14 days'""")
        from collections import Counter
        c = Counter()
        for (h,) in cur.fetchall():
            if not h: continue
            for item in (h if isinstance(h, list) else json.loads(h)):
                c[item.get('signal')] += 1
        if c:
            print('\n  ── 命中信号分布 ──')
            for k, v in c.most_common():
                print('     %-24s %d 次' % (k, v))
    finally:
        cur.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='实际退款（默认只记录）')
    ap.add_argument('--window', type=int, default=0, help='判定窗口（分钟）')
    ap.add_argument('--stats', action='store_true', help='只看统计')
    ap.add_argument('--verbose', action='store_true')
    a = ap.parse_args()
    if a.stats:
        db = get_db(); print_stats(db); db.close()
    else:
        run(dry_run=not a.apply, window_min=a.window, verbose=a.verbose)
