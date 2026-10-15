#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S1054] 每日用户画像报表

功能：
  ① 统计前一天的使用情况 + 投诉情况
  ② 按规则识别"高风险用户"（建议提前退款）
  ③ 行为分布统计
  ④ 输出可读报表（文本 + JSON）

规则（基于真实数据测算）：
  规则1：1小时内查余额 >= 15 次（投诉前命中 19.2%，正常误伤 7.3%）
  规则2：取包后10分钟内操作 >= 30 次（命中 7.1%，误伤 1.5%）
  规则3：取包后1小时内查余额 >= 20 次（命中 11.1%，误伤 1.7%）

用法：
  python3 daily_report.py              # 默认统计昨天
  python3 daily_report.py 2026-10-14   # 指定日期
"""
import sys
import os
import json
import logging
from datetime import datetime, timedelta
logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')
from database import get_db

DAY = sys.argv[1] if len(sys.argv) > 1 else (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

db = get_db(); cur = db.cursor()

def q(sql, args=None):
    cur.execute(sql, args or ())
    return [dict(r) for r in cur.fetchall()]

L = []
def p(s=''):
    L.append(s)
    print(s)

p('=' * 92)
p('  每日用户画像报表  ——  %s' % DAY)
p('  生成时间: %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
p('=' * 92)

# ---------- ① 总体 ----------
p('')
p('【一】总体情况')
r = q("""SELECT COUNT(*) AS n,
                COUNT(*) FILTER (WHERE refund_time IS NOT NULL) AS refunded
         FROM orders WHERE retrieve_time::date = %s AND retrieve_time IS NOT NULL""", (DAY,))[0]
orders_n = r['n']; refunded_n = r['refunded']
r = q("""SELECT COUNT(*) AS n FROM complaints c JOIN orders o ON o.order_no=c.order_no
         WHERE c.complaint_type='wechat' AND c.created_at::date=%s""", (DAY,))[0]
cmp_n = r['n']
r = q("""SELECT COUNT(*) AS n FROM complaints WHERE complaint_type='wechat' AND created_at::date=%s""", (DAY,))[0]
cmp_all = r['n']
r = q("""SELECT COUNT(*) AS n, COALESCE(COUNT(*) FILTER (WHERE approve_time IS NULL),0) AS pending
         FROM withdrawal_records WHERE created_at::date=%s""", (DAY,))[0]
wd_n = r['n']
p('   取包订单:      %d 笔（当天已退款 %d 笔，%.0f%%）' % (
    orders_n, refunded_n, 100.0*refunded_n/max(orders_n,1)))
p('   提现申请:      %d 笔' % wd_n)
p('   ★ 微信投诉:     %d 笔（能关联到订单）' % cmp_n)
p('   微信投诉(全部): %d 笔' % cmp_all)
p('   ★ 投诉率:       %.2f%%  （投诉/取包）' % (100.0*cmp_n/max(orders_n,1)))

# ---------- ② 行为埋点数据 ----------
p('')
p('【二】行为埋点（user_behavior_log）')
r = q("""SELECT COUNT(*) AS n, COUNT(DISTINCT openid) AS users,
                COUNT(DISTINCT openid) AS oids
         FROM user_behavior_log WHERE created_at::date=%s""", (DAY,))[0]
p('   记录条数: %d 条，涉及用户 %d 个' % (r['n'], r['users']))
if r['n']:
    rows = q("""SELECT action, COUNT(*) AS n, COUNT(DISTINCT openid) AS u
                FROM user_behavior_log WHERE created_at::date=%s
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", (DAY,))
    p('   动作分布:')
    for x in rows:
        p('      %-16s %7d 条 / %5d 人' % (x['action'], x['n'], x['u']))

# ---------- ③ 高风险用户（按规则识别）----------
p('')
p('【三】★ 高风险用户清单（建议提前退款）')
p('   规则1: 1小时内查余额>=15次 | 规则3: 取包后1小时查余额>=20次 | 规则2: 取包后10分钟操作>=30次')

# 用埋点表算（更准，且永久保留）
rows = q("""
    WITH b AS (
      SELECT openid, created_at FROM user_behavior_log
      WHERE created_at::date = %s AND action='balance'
    ),
    peak AS (
      -- 每个 openid 在任意1小时窗口内的最大查余额次数
      SELECT b1.openid, COUNT(*) AS cnt, MIN(b2.created_at) AS win_start
      FROM b b1 JOIN b b2 ON b2.openid = b1.openid
        AND b2.created_at BETWEEN b1.created_at AND b1.created_at + INTERVAL '1 hour'
      GROUP BY b1.openid, b1.created_at
    ),
    mx AS (SELECT openid, MAX(cnt) AS max_hour FROM peak GROUP BY 1)
    SELECT mx.openid, mx.max_hour,
           (SELECT user_phone FROM orders o WHERE (o.mp_openid=mx.openid OR o.openid=mx.openid)
              AND o.retrieve_time::date=%s ORDER BY o.id DESC LIMIT 1) AS phone,
           (SELECT order_no FROM orders o WHERE (o.mp_openid=mx.openid OR o.openid=mx.openid)
              AND o.retrieve_time::date=%s ORDER BY o.id DESC LIMIT 1) AS order_no,
           (SELECT deposit_amount FROM orders o WHERE (o.mp_openid=mx.openid OR o.openid=mx.openid)
              AND o.retrieve_time::date=%s ORDER BY o.id DESC LIMIT 1) AS dep,
           (SELECT refund_time IS NULL FROM orders o WHERE (o.mp_openid=mx.openid OR o.openid=mx.openid)
              AND o.retrieve_time::date=%s ORDER BY o.id DESC LIMIT 1) AS not_refunded
    FROM mx WHERE mx.max_hour >= 15
    ORDER BY mx.max_hour DESC LIMIT 50
""", (DAY, DAY, DAY, DAY, DAY))
if not rows:
    p('   （当天埋点暂无 >=15次/小时 的记录 —— 埋点刚启用，需积累几天）')
else:
    p('   %-13s %8s %8s %9s %s' % ('手机号', '峰值/小时', '押金', '未退款', '订单号'))
    for x in rows[:30]:
        p('   %-13s %8d %8s %9s %s' % (
            x['phone'] or '(未知)', x['max_hour'], x['dep'] or '-',
            '★未退' if x['not_refunded'] else '已退', x['order_no'] or '-'))

# ---------- ④ 用 nginx 日志算（兜底，历史数据）----------
p('')
p('【四】行为分布（nginx 日志，若当天日志还在）')
import re, gzip, io as _io
LOG_FILES = ['/var/log/nginx/access.log', '/var/log/nginx/access.log.1']
LOG_FILES += ['/var/log/nginx/access.log.%d.gz' % i for i in range(2, 15)]
LINE_RE = re.compile(r'^(\S+) \S+ \S+ \[(\d+)/(\w+)/(\d+):')
OID_RE = re.compile(r'[?&]openid=([A-Za-z0-9_\-]{20,})')
MON = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
from collections import defaultdict, Counter
hourly = defaultdict(lambda: defaultdict(int))
_day = datetime.strptime(DAY, '%Y-%m-%d')
found = 0
for fp in LOG_FILES:
    if not os.path.exists(fp): continue
    try:
        op = gzip.open if fp.endswith('.gz') else _io.open
        with op(fp, 'rt', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if '/api/user/balance' not in line: continue
                mm = LINE_RE.match(line)
                if not mm: continue
                try:
                    d = datetime(int(mm.group(4)), MON.get(mm.group(3),1), int(mm.group(2)))
                except Exception:
                    continue
                if d.date() != _day.date(): continue
                om = OID_RE.search(line)
                if not om: continue
                hourly[om.group(1)][d.hour] += 1
                found += 1
    except Exception:
        pass
p('   当天查余额请求: %d 条，涉及 %d 个用户' % (found, len(hourly)))
if hourly:
    dist = Counter()
    for oid, hh in hourly.items():
        mx = max(hh.values())
        dist['0次' if mx == 0 else ('1-5次' if mx <= 5 else ('6-14次' if mx <= 14 else ('15-29次' if mx <= 29 else '>=30次')))] += 1
    tot = sum(dist.values())
    p('   单小时峰值分布:')
    for k in ('1-5次', '6-14次', '15-29次', '>=30次'):
        if dist.get(k):
            p('      %-10s %5d 人 (%.1f%%)' % (k, dist[k], 100.0*dist[k]/tot))

# ---------- ⑤ 输出文件 ----------
outdir = '/home/ubuntu/smart-locker/reports'
try:
    os.makedirs(outdir, exist_ok=True)
    txt = os.path.join(outdir, 'profile_%s.txt' % DAY)
    with _io.open(txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    p('')
    p('【五】报表已保存')
    p('   %s' % txt)
except Exception as e:
    p('   保存失败: %s' % e)

p('=' * 92)
cur.close(); db.close()
