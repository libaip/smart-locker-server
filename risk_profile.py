# -*- coding: utf-8 -*-
"""[S1018] 用户风险画像 —— 评分模型

设计原则（基于真实数据洞察）：
  ① 投诉次数不是全部：87% 的人只投诉 1 次 → 要对"事件驱动"和"惯犯"区分
  ② 微信官方投诉（complaint_type='wechat'）权重最高 —— 它直接影响微信收款限制
  ③ ★ 最实用的是【未解决诉求】：有处理中退款 + 焦虑查询 = 马上要投诉
  ④ 行为频次区分度只有 1.3~1.65x → 权重不能太高，作为辅助
  ⑤ 全部可解释：输出 factors 明细，客服能看到"为什么给他打高分"

评分维度（总分 100）：
  ① 历史投诉      40 分
  ② 未解决诉求    30 分  ★ 最可操作
  ③ 行为焦虑      15 分
  ④ 订单/交易     15 分

分档：0-29 低 / 30-59 中 / 60-84 高 / 85-100 极高
"""
import math
import json
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# [S1018c] 阈值按真实分数分布调过：原 85/60/30 太严，最高分只到 61，
#   导致没有任何用户能进"极高风险"。改成 70/45/20，更贴合实际分布。
LEVELS = [
    (70, 'critical', '极高风险'),
    (45, 'high',     '高风险'),
    (20, 'medium',   '中风险'),
    (0,  'low',      '低风险'),
]
LEVEL_LABEL = {'critical': '极高风险', 'high': '高风险', 'medium': '中风险', 'low': '低风险'}


def _level_of(score):
    for th, lv, _lab in LEVELS:
        if score >= th:
            return lv
    return 'low'


# ---------------------------------------------------------------- 取数据
def collect_features(cur, user_key, days_window=30):
    """从库里聚合某个手机号的画像特征。全部只读。"""
    f = {
        'user_key': user_key,
        'complaint_total': 0, 'complaint_7d': 0, 'complaint_30d': 0,
        'complaint_wechat': 0, 'complaint_days': 0, 'complaint_refunded': 0,
        'last_complaint_at': None,
        'pending_refund_cnt': 0, 'pending_refund_amt': 0.0, 'failed_withdraw_cnt': 0,
        'order_cnt': 0, 'order_amt': 0.0, 'refund_cnt': 0, 'paid_order_cnt': 0,
        'query_balance_7d': 0, 'query_orders_7d': 0, 'peak_day_queries': 0,
    }
    if not user_key:
        return f
    # ---- 投诉 ----
    try:
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS c7,
                   COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS c30,
                   COUNT(*) FILTER (WHERE complaint_type = 'wechat')                AS cwx,
                   COUNT(DISTINCT created_at::date)                                 AS cdays,
                   COUNT(*) FILTER (WHERE COALESCE(refund_status,'') IN ('SUCCESS','success','refunded')) AS cref,
                   MAX(created_at) AS last_at
            FROM complaints WHERE user_phone = %s
        """, (user_key,))
        r = cur.fetchone()
        if r:
            d = dict(r)
            f['complaint_total'] = int(d.get('total') or 0)
            f['complaint_7d'] = int(d.get('c7') or 0)
            f['complaint_30d'] = int(d.get('c30') or 0)
            f['complaint_wechat'] = int(d.get('cwx') or 0)
            f['complaint_days'] = int(d.get('cdays') or 0)
            f['complaint_refunded'] = int(d.get('cref') or 0)
            f['last_complaint_at'] = d.get('last_at')
    except Exception as e:
        logger.warning('[risk] 取投诉特征失败 %s: %s', user_key, e)

    # ---- 订单 ----
    try:
        cur.execute("""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(deposit_amount),0) AS amt,
                   COUNT(*) FILTER (WHERE status >= 2) AS paid
            FROM orders WHERE user_phone = %s
        """, (user_key,))
        r = cur.fetchone()
        if r:
            d = dict(r)
            f['order_cnt'] = int(d.get('cnt') or 0)
            f['order_amt'] = float(d.get('amt') or 0)
            f['paid_order_cnt'] = int(d.get('paid') or 0)
    except Exception as e:
        logger.warning('[risk] 取订单特征失败 %s: %s', user_key, e)

    # ---- 退款笔数（payments type=2）----
    try:
        cur.execute("SELECT COUNT(*) AS n FROM payments WHERE user_phone=%s AND type=2", (user_key,))
        r = cur.fetchone()
        f['refund_cnt'] = int((dict(r).get('n') if r else 0) or 0)
    except Exception:
        pass

    # ---- 未解决诉求：处理中的退款/提现 ----
    try:
        cur.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt
            FROM withdrawal_records
            WHERE user_phone = %s AND status IN (0,1)
        """, (user_key,))
        r = cur.fetchone()
        if r:
            d = dict(r)
            f['pending_refund_cnt'] = int(d.get('n') or 0)
            f['pending_refund_amt'] = float(d.get('amt') or 0)
    except Exception:
        pass
    # 退款失败/异常
    try:
        cur.execute("""
            SELECT COUNT(*) AS n FROM complaints
            WHERE user_phone=%s AND COALESCE(refund_status,'') NOT IN ('SUCCESS','success','refunded','')
        """, (user_key,))
        r = cur.fetchone()
        f['failed_withdraw_cnt'] = int((dict(r).get('n') if r else 0) or 0)
    except Exception:
        pass

    # ---- 行为（埋点表，有就统计）----
    try:
        cur.execute("""
            SELECT action, COUNT(*) AS n
            FROM user_behavior_log
            WHERE user_key=%s AND created_at >= NOW() - INTERVAL '7 days'
            GROUP BY action
        """, (user_key,))
        for r in cur.fetchall():
            d = dict(r)
            if d['action'] == 'balance':
                f['query_balance_7d'] = int(d['n'])
            elif d['action'] == 'orders':
                f['query_orders_7d'] = int(d['n'])
        cur.execute("""
            SELECT COALESCE(MAX(n),0) AS peak FROM (
                SELECT COUNT(*) AS n FROM user_behavior_log
                WHERE user_key=%s AND created_at >= NOW() - INTERVAL '7 days'
                GROUP BY created_at::date) t
        """, (user_key,))
        r = cur.fetchone()
        f['peak_day_queries'] = int((dict(r).get('peak') if r else 0) or 0)
    except Exception:
        pass

    return f


# ---------------------------------------------------------------- 打分
def score_features(f):
    """把特征打成 0-100 分，并给出可解释明细。返回 (score, level, factors)"""
    factors = []

    # ============ ① 历史投诉（最高 55）★ 最强信号 ============
    #   [S1018b] 改成分段函数：原版对数曲线在 5 次以上就压平了，
    #   导致"投诉5次"和"投诉20次"几乎同分（37 vs 40）-> 无法区分惯犯。
    s_cmp = 0.0
    ct = f.get('complaint_total') or 0
    if ct > 0:
        if ct >= 30:
            base = 50.0
        elif ct >= 11:
            base = 42.0
        elif ct >= 4:
            base = 32.0
        elif ct >= 2:
            base = 20.0
        else:
            base = 12.0
        wx_b = min(10.0, (f.get('complaint_wechat') or 0) * 1.5)      # 官方投诉权重最高
        rec = min(8.0, (f.get('complaint_7d') or 0) * 4.0 + (f.get('complaint_30d') or 0) * 0.6)
        habit = min(6.0, max(0, (f.get('complaint_days') or 0) - 1) * 1.5)   # 跨天=惯犯
        malice = min(8.0, (f.get('complaint_refunded') or 0) * 2.5)          # 已退款还闹=恶意
        s_cmp = min(55.0, base + wx_b + rec + habit + malice)
        factors.append({'k': 'complaint', 'label': '历史投诉', 'score': round(s_cmp, 1),
                        'note': '共%d次(官方%d/近7天%d/分布在%d天/已退款后仍投诉%d)' % (
                            ct, f.get('complaint_wechat') or 0, f.get('complaint_7d') or 0,
                            f.get('complaint_days') or 0, f.get('complaint_refunded') or 0)})

    # ============ ② 未解决诉求（最高 25）★ 最可操作 ============
    s_pend = 0.0
    pc = f.get('pending_refund_cnt') or 0
    if pc > 0:
        s_pend += min(16.0, 11.0 + pc * 2.5)          # 有处理中退款就已经很危险
    if (f.get('failed_withdraw_cnt') or 0) > 0:
        s_pend += min(9.0, (f['failed_withdraw_cnt']) * 3.0)
    s_pend = min(25.0, s_pend)
    if s_pend > 0:
        factors.append({'k': 'pending', 'label': '未解决诉求', 'score': round(s_pend, 1),
                        'note': '处理中退款%d笔(¥%.2f)/退款异常%d笔' % (
                            pc, f.get('pending_refund_amt') or 0, f.get('failed_withdraw_cnt') or 0)})

    # ============ ③ 行为焦虑（最高 10）============
    #   [S1018b] 实测行为频次区分度只有 1.3~1.65x -> 降权到 10 分，只作辅助信号
    s_beh = 0.0
    q = (f.get('query_balance_7d') or 0) + (f.get('query_orders_7d') or 0)
    if q >= 30:
        s_beh = 8.0
    elif q >= 20:
        s_beh = 6.5
    elif q >= 10:
        s_beh = 5.0
    elif q >= 5:
        s_beh = 3.0
    if (f.get('peak_day_queries') or 0) >= 20:
        s_beh = min(10.0, s_beh + 2.0)
    if s_beh > 0:
        factors.append({'k': 'behavior', 'label': '行为焦虑度', 'score': round(s_beh, 1),
                        'note': '近7天查余额%d次/查订单%d次/单日峰值%d次' % (
                            f.get('query_balance_7d') or 0, f.get('query_orders_7d') or 0,
                            f.get('peak_day_queries') or 0)})

    # ============ ④ 订单/交易（最高 10）============
    s_ord = 0.0
    oc = f.get('order_cnt') or 0
    if oc >= 2:
        rate = (f.get('refund_cnt') or 0) / float(oc)
        if rate >= 0.8:
            s_ord += 6.0
        elif rate >= 0.5:
            s_ord += 4.0
        elif rate >= 0.3:
            s_ord += 2.0
    if oc == 0 and ct > 0:
        s_ord += 4.0          # 没订单却投诉 → 异常
    if ct >= 3 and oc > 0 and (f.get('refund_cnt') or 0) >= ct:
        s_ord += 3.0          # 投诉数 ≈ 退款数 → 纯粹为退款反复折腾
    s_ord = min(10.0, s_ord)
    if s_ord > 0:
        factors.append({'k': 'order', 'label': '订单/交易特征', 'score': round(s_ord, 1),
                        'note': '订单%d笔(成功%d)/退款%d笔' % (
                            oc, f.get('paid_order_cnt') or 0, f.get('refund_cnt') or 0)})

    # ============ ⑤ 组合加成（雪上加霜效应）============
    #   单个维度都高还不够说明问题；【多个高危维度同时命中】才是真危险。
    #   这些组合都是从真实投诉数据里总结的行为模式。
    s_comb = 0.0
    _ct = f.get('complaint_total') or 0
    _wx = f.get('complaint_wechat') or 0
    _ref = f.get('complaint_refunded') or 0
    _pend = f.get('pending_refund_cnt') or 0
    comb_notes = []
    if _ct >= 5 and _wx >= 3:
        s_comb += 10.0; comb_notes.append('多次投诉且官方投诉>=3')
    if _ref >= 3:
        s_comb += 8.0; comb_notes.append('已退款仍反复投诉>=3次')
    if _pend > 0 and s_beh >= 5.0:
        s_comb += 8.0; comb_notes.append('有处理中退款+焦虑查询(马上要爆)')
    if _ct >= 10 and (f.get('complaint_days') or 0) >= 5:
        s_comb += 7.0; comb_notes.append('高频投诉跨5天以上(惯犯)')
    if _ct >= 3 and (f.get('order_cnt') or 0) == 0:
        s_comb += 5.0; comb_notes.append('无订单却反复投诉(异常)')
    s_comb = min(20.0, s_comb)
    if s_comb > 0:
        factors.append({'k': 'combo', 'label': '组合风险', 'score': round(s_comb, 1),
                        'note': '；'.join(comb_notes)})

    score = int(round(min(100.0, max(0.0, s_cmp + s_pend + s_beh + s_ord + s_comb))))
    return score, _level_of(score), factors


def build_profile(cur, user_key):
    """算一个人的完整画像。返回 dict（可直接写库）。"""
    f = collect_features(cur, user_key)
    score, level, factors = score_features(f)
    return {
        'user_key': user_key,
        'risk_score': score,
        'risk_level': level,
        'complaint_total': f['complaint_total'], 'complaint_7d': f['complaint_7d'],
        'complaint_30d': f['complaint_30d'], 'complaint_wechat': f['complaint_wechat'],
        'complaint_days': f['complaint_days'], 'complaint_refunded': f['complaint_refunded'],
        'last_complaint_at': f['last_complaint_at'],
        'pending_refund_cnt': f['pending_refund_cnt'], 'pending_refund_amt': f['pending_refund_amt'],
        'failed_withdraw_cnt': f['failed_withdraw_cnt'],
        'order_cnt': f['order_cnt'], 'order_amt': f['order_amt'],
        'refund_cnt': f['refund_cnt'], 'paid_order_cnt': f['paid_order_cnt'],
        'query_balance_7d': f['query_balance_7d'], 'query_orders_7d': f['query_orders_7d'],
        'peak_day_queries': f['peak_day_queries'],
        'factors': json.dumps(factors, ensure_ascii=False),
        'note': ' | '.join('%s%.0f' % (x['label'], x['score']) for x in factors)[:250],
    }


def upsert_profile(cur, p):
    cur.execute("""
        INSERT INTO user_risk_profile
            (user_key, risk_score, risk_level, complaint_total, complaint_7d, complaint_30d,
             complaint_wechat, complaint_days, complaint_refunded, last_complaint_at,
             pending_refund_cnt, pending_refund_amt, failed_withdraw_cnt,
             order_cnt, order_amt, refund_cnt, paid_order_cnt,
             query_balance_7d, query_orders_7d, peak_day_queries, factors, note, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (user_key) DO UPDATE SET
            risk_score=EXCLUDED.risk_score, risk_level=EXCLUDED.risk_level,
            complaint_total=EXCLUDED.complaint_total, complaint_7d=EXCLUDED.complaint_7d,
            complaint_30d=EXCLUDED.complaint_30d, complaint_wechat=EXCLUDED.complaint_wechat,
            complaint_days=EXCLUDED.complaint_days, complaint_refunded=EXCLUDED.complaint_refunded,
            last_complaint_at=EXCLUDED.last_complaint_at,
            pending_refund_cnt=EXCLUDED.pending_refund_cnt, pending_refund_amt=EXCLUDED.pending_refund_amt,
            failed_withdraw_cnt=EXCLUDED.failed_withdraw_cnt,
            order_cnt=EXCLUDED.order_cnt, order_amt=EXCLUDED.order_amt,
            refund_cnt=EXCLUDED.refund_cnt, paid_order_cnt=EXCLUDED.paid_order_cnt,
            query_balance_7d=EXCLUDED.query_balance_7d, query_orders_7d=EXCLUDED.query_orders_7d,
            peak_day_queries=EXCLUDED.peak_day_queries, factors=EXCLUDED.factors,
            note=EXCLUDED.note, updated_at=NOW()
    """, (p['user_key'], p['risk_score'], p['risk_level'], p['complaint_total'], p['complaint_7d'],
          p['complaint_30d'], p['complaint_wechat'], p['complaint_days'], p['complaint_refunded'],
          p['last_complaint_at'], p['pending_refund_cnt'], p['pending_refund_amt'],
          p['failed_withdraw_cnt'], p['order_cnt'], p['order_amt'], p['refund_cnt'],
          p['paid_order_cnt'], p['query_balance_7d'], p['query_orders_7d'],
          p['peak_day_queries'], p['factors'], p['note']))
