#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[S1117] 行为打分日报 behavior_report.py

每天输出：
  ① 决策统计（判定数/高风险/已退款/金额）
  ② 命中信号分布
  ③ 得分分布
  ④ 与投诉的对比（触发组 vs 未触发组的投诉率）
  ⑤ 建议（阈值是否需要调整）
"""
import sys, os, json, logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')
from database import get_db

OUT_DIR = '/home/ubuntu/smart-locker/reports'
os.makedirs(OUT_DIR, exist_ok=True)


def main():
    db = get_db()
    cur = db.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    lines = []
    def P(s=''):
        lines.append(s)
        print(s)

    P('=' * 90)
    P('  行为打分日报  %s' % datetime.now().strftime('%Y-%m-%d %H:%M'))
    P('=' * 90)

    # ---------- ① 决策统计 ----------
    cur.execute("""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE is_high_risk=1) AS risk,
               COUNT(*) FILTER (WHERE action='auto_refund') AS refunded,
               COUNT(*) FILTER (WHERE dry_run=1) AS dry,
               COALESCE(SUM(deposit_amount) FILTER (WHERE is_high_risk=1),0) AS amt
        FROM behavior_decision_log WHERE created_at >= NOW() - INTERVAL '1 day'
    """)
    r = cur.fetchone()
    P('')
    P('  【一】近24小时决策统计')
    P('     判定订单:   %d 笔' % r[0])
    P('     高风险:     %d 笔 (%.1f%%)' % (r[1], 100.0*r[1]/max(r[0],1)))
    P('     已退款:     %d 笔' % r[2])
    P('     灰度记录:   %d 笔' % r[3])
    P('     涉及金额:   %.2f 元' % r[4])

    # ---------- ② 命中信号 ----------
    cur.execute("""SELECT hits FROM behavior_decision_log
                   WHERE is_high_risk=1 AND created_at >= NOW() - INTERVAL '7 days'""")
    c = Counter()
    for row in cur.fetchall():
        h = row[0]
        if not h: continue
        if isinstance(h, str):
            try: h = json.loads(h)
            except Exception: continue
        for item in h:
            c[item.get('signal')] += 1
    P('')
    P('  【二】命中信号分布（近7天高风险）')
    if c:
        for k, v in c.most_common():
            P('     %-24s %5d 次' % (k, v))
    else:
        P('     （无数据）')

    # ---------- ③ 得分分布 ----------
    cur.execute("""SELECT score, COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE is_high_risk=1) AS risk
                   FROM behavior_decision_log
                   WHERE created_at >= NOW() - INTERVAL '7 days'
                   GROUP BY score ORDER BY score DESC""")
    P('')
    P('  【三】得分分布（近7天）')
    P('     %-8s %10s %10s' % ('得分', '判定数', '高风险'))
    for row in cur.fetchall():
        P('     %-8s %10d %10d' % (row[0], row[1], row[2]))

    # ---------- ④ 触发组 vs 未触发组的投诉率 ----------
    cur.execute("""
        SELECT
          COUNT(DISTINCT b.order_id) AS trig_orders,
          COUNT(DISTINCT b.order_id) FILTER (WHERE EXISTS (
            SELECT 1 FROM complaints c WHERE c.order_no = b.order_no AND c.complaint_type='wechat'
          )) AS trig_cmp
        FROM behavior_decision_log b
        WHERE b.is_high_risk=1 AND b.created_at >= NOW() - INTERVAL '14 days'
    """)
    t = cur.fetchone()
    trig_orders = t[0] or 0
    trig_cmp = t[1] or 0
    P('')
    P('  【四】★ 触发组的投诉情况（近14天）')
    P('     被触发的订单: %d 笔' % trig_orders)
    P('     其中被微信投诉: %d 笔 → 投诉率 %.2f%%' % (
        trig_cmp, 100.0*trig_cmp/max(trig_orders,1)))
    # 对照：整体投诉率
    cur.execute("""SELECT COUNT(*) AS n FROM orders
                   WHERE retrieve_time >= NOW() - INTERVAL '14 days' AND retrieve_time IS NOT NULL""")
    tot = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) AS n FROM complaints c JOIN orders o ON o.order_no=c.order_no
                   WHERE c.complaint_type='wechat' AND o.retrieve_time >= NOW() - INTERVAL '14 days'""")
    totc = cur.fetchone()[0]
    P('     整体投诉率:   %.2f%%（%d/%d）' % (100.0*totc/max(tot,1), totc, tot))
    if trig_orders > 0:
        lift = (100.0*trig_cmp/trig_orders) / max(100.0*totc/max(tot,1), 0.001)
        P('     ★ 提升倍数:   %.2fx' % lift)
        if lift < 1.5:
            P('     ⚠️ 提升偏低（<1.5x），建议检查阈值或打分表')
        else:
            P('     ✅ 触发组的投诉率明显更高，打分有效')

    # ---------- ⑤ 建议 ----------
    P('')
    P('  【五】建议')
    cur.execute("SELECT key AS k, value AS v FROM behavior_score_settings")
    st = {}
    for row in cur.fetchall():
        if isinstance(row, dict):
            st[row.get('k')] = row.get('v')
        else:
            st[row[0]] = row[1]
    P('     当前阈值: %s | 窗口: %s 分钟 | 灰度: %s' % (
        st.get('threshold'), st.get('window_min'),
        '是（只记录）' if st.get('dry_run') == '1' else '否（实际退款）'))
    if st.get('dry_run') == '1':
        P('     ★ 目前是灰度模式，只记录不退款。观察 1 周后可改为实际退款。')

    P('=' * 90)
    path = os.path.join(OUT_DIR, 'behavior_%s.txt' % today)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('\n  报告已保存: %s' % path)
    cur.close(); db.close()


if __name__ == '__main__':
    main()
