#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S884] 每日体检 —— 自动发现"余额/身份/退款/投诉"异常，不用等用户投诉

用法：
    cd /home/ubuntu/smart-locker && python3 scripts/daily_health.py
    python3 scripts/daily_health.py --hours 24          # 看过去 24 小时
    python3 scripts/daily_health.py --sample 30         # 余额抽样校验的样本数

退出码：0 = 全部正常；1 = 有告警（方便挂 cron 时判断）
只读，不改任何数据。
"""
import sys
import os
import argparse
import logging
from datetime import datetime, timedelta

logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_db  # noqa: E402

ALERTS = []


def _h(title):
    print('')
    print('=' * 84)
    print('  ' + title)
    print('=' * 84)


def info(msg):
    print('      %s' % msg)


def _verdict(name, value, threshold, unit='', higher_is_better=True, warn_msg=''):
    ok = (value >= threshold) if higher_is_better else (value <= threshold)
    mark = '✅' if ok else '❌'
    print('   %s %-34s %s%s   (阈值 %s%s)' % (mark, name, value, unit, threshold, unit))
    if not ok:
        ALERTS.append('%s = %s%s%s%s' % (name, value, unit,
                                         '，' if warn_msg else '',
                                         warn_msg))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=int, default=24)
    ap.add_argument('--sample', type=int, default=20)
    ap.add_argument('--push', action='store_true',
                    help='把结论推送到微信（PushPlus）')
    args = ap.parse_args()

    since = datetime.now() - timedelta(hours=args.hours)
    since_s = since.strftime('%Y-%m-%d %H:%M:%S')
    db = get_db()
    cur = db.cursor()

    print('=' * 84)
    print('  智能寄存柜 · 每日体检')
    print('  时间: %s    统计区间: 最近 %d 小时（自 %s）' % (
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'), args.hours, since_s))
    print('=' * 84)

    # ---------------------------------------------------------------
    _h('① 身份绑定：新注册用户的 user_id 有没有绑上（"余额显示0"的总开关）')
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE COALESCE(user_id,0)=0) AS zero
                   FROM phone_openids WHERE created_at >= %s""", (since_s,))
    d = dict(cur.fetchone())
    total = int(d['n'] or 0)
    zero = int(d['zero'] or 0)
    print('   新增身份: %s 条   其中 user_id=0: %s 条' % (total, zero))
    if total == 0:
        print('   （区间内没有新增身份）')
    else:
        rate = round(100.0 * (total - zero) / total, 1)
        _verdict('新增身份绑定率', rate, 95.0, '%', True,
                 '每天有身份绑不上 → 这些用户余额会显示 0')

    # ---------------------------------------------------------------
    _h('② 订单身份完整性：微信单有没有带小程序 openid（余额算不算得出来）')
    _WX = "payment_channel_id IN (SELECT id FROM payment_channels WHERE channel_type='wechat')"
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE COALESCE(mp_openid,'')='') AS nomp,
                          COUNT(*) FILTER (WHERE openid LIKE 'ofEDK%%') AS oa_like
                   FROM orders WHERE created_at >= %s AND """ + _WX, (since_s,))
    d = dict(cur.fetchone())
    ot = int(d['n'] or 0)
    nomp = int(d['nomp'] or 0)
    oa_like = int(d['oa_like'] or 0)
    print('   新增【微信】订单: %s 笔   mp_openid 为空: %s 笔   openid 是公众号: %s 笔' % (ot, nomp, oa_like))
    if ot:
        rate = round(100.0 * (ot - nomp) / ot, 1)
        _verdict('微信单身份完整率', rate, 85.0, '%', True, '订单身份残缺 → 用户钱包算不出这笔钱')
        if oa_like > 0:
            print('   ⚠️ 区间内有 %s 笔微信单的 openid 字段是【公众号 openid】（应为小程序）' % oa_like)
    # ★ 只看最近 2 小时（这才是"现在还在不在发生"的信号）
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE COALESCE(mp_openid,'')='') AS nomp,
                          COUNT(*) FILTER (WHERE openid LIKE 'ofEDK%%') AS oa_like
                   FROM orders WHERE created_at >= NOW() - INTERVAL '2 hours' AND """ + _WX)
    d2 = dict(cur.fetchone())
    r2 = int(d2['n'] or 0)
    if r2 > 0:
        print('   近 2 小时微信单: %s 笔，mp 为空 %s 笔，openid 是公众号 %s 笔' % (
            r2, d2['nomp'], d2['oa_like']))
        if int(d2['nomp'] or 0) > 0 or int(d2['oa_like'] or 0) > 0:
            ALERTS.append('近2小时仍有身份残缺微信单（mp为空%s / 公众号%s）'
                          % (d2['nomp'], d2['oa_like']))
            print('   ❌ 近 2 小时仍有身份残缺 → 回填链路可能又断了！')
        else:
            print('   ✅ 近 2 小时微信单身份全部完整（说明修复持续有效）')
    else:
        print('   近 2 小时没有微信单')

    # ---------------------------------------------------------------
    _h('③ 余额流水：有没有流水卡在 pending（钱冻着、算不进余额）')
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE status='pending') AS pend,
                          COUNT(*) FILTER (WHERE status='available') AS avail
                   FROM user_balance_details WHERE created_at >= %s""", (since_s,))
    d = dict(cur.fetchone())
    print('   新增流水: %s 笔   可用(available): %s 笔   冻结(pending): %s 笔' % (
        d['n'], d['avail'], d['pend']))
    # 卡住的：pending 且订单已完成、没退款、无进行中提现、超1小时
    _stuck_sql = """SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(bd.amount),0)::numeric,2) AS amt
                   FROM user_balance_details bd JOIN orders o ON o.id=bd.order_id
                   WHERE bd.status='pending' AND bd.created_at < NOW() - INTERVAL '1 hour'
                     AND o.status=3
                     AND COALESCE(NULLIF(o.refund_status,''),'none')='none'
                     AND NOT EXISTS (SELECT 1 FROM withdrawal_records w
                                      WHERE w.order_id=bd.order_id AND w.status IN (0,1,2))
                     AND bd.created_at %s"""
    # ★ 区间内新增的卡住（可行动） vs 历史积压（另案处理）
    cur.execute(_stuck_sql % '>= %s', (since_s,))
    d = dict(cur.fetchone())
    stuck_new, amt_new = int(d['n'] or 0), d['amt']
    cur.execute(_stuck_sql % '< %s', (since_s,))
    d = dict(cur.fetchone())
    backlog, amt_backlog = int(d['n'] or 0), d['amt']

    print('   ★ 区间内新卡住的 pending: %s 笔  ¥%s   ← 这是"现在还在发生"的信号' % (stuck_new, amt_new))
    print('     历史积压（更早的）    : %s 笔  ¥%s   ← 历史账，按老板口径另案' % (backlog, amt_backlog))
    if stuck_new > 0:
        ALERTS.append('区间内新卡住 pending %s 笔 ¥%s（钱冻着拿不到）' % (stuck_new, amt_new))
        print('   ❌ 有新卡住的流水 → 需要解冻')
    else:
        print('   ✅ 区间内没有新卡住的流水')

    # ---------------------------------------------------------------
    _h('④ 提现/退款：自动拒绝率高不高（高 = 用户收不到钱、容易投诉）')
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE status=2) AS ok,
                          COUNT(*) FILTER (WHERE status=3) AS fail,
                          COUNT(*) FILTER (WHERE status=3 AND COALESCE(error_msg,'') LIKE '自动拒绝%%') AS autorej
                   FROM withdrawal_records WHERE created_at >= %s""", (since_s,))
    d = dict(cur.fetchone())
    n = int(d['n'] or 0)
    print('   提现/退款记录: %s 条   成功: %s   失败: %s（其中自动拒绝 %s）' % (
        n, d['ok'], d['fail'], d['autorej']))
    if n:
        rej_rate = round(100.0 * int(d['autorej'] or 0) / n, 1)
        _verdict('自动拒绝率', rej_rate, 25.0, '%', False,
                 '拒绝率过高 → 用户收不到钱，会投诉（可调网点通过率）')
    # 失败原因 top3
    cur.execute("""SELECT LEFT(COALESCE(error_msg,'(空)'),40) AS e, COUNT(*) AS n
                   FROM withdrawal_records WHERE created_at >= %s AND status=3
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 3""", (since_s,))
    rs = [dict(r) for r in cur.fetchall()]
    if rs:
        print('   失败原因 Top3:')
        for r in rs:
            print('      %-42s %s 条' % (r['e'], r['n']))

    # ---------------------------------------------------------------
    _h('⑤ 投诉：数量和原因（对照商户号）')
    cur.execute("""SELECT COUNT(*) AS n FROM complaints WHERE created_at >= %s""", (since_s,))
    cn = int(dict(cur.fetchone())['n'] or 0)
    print('   投诉总数: %s 条' % cn)
    cur.execute("""SELECT LEFT(COALESCE(content,''),34) AS c, COALESCE(type,'') AS t,
                          COALESCE(mch_id,'') AS mch, to_char(created_at,'MM-DD HH24:MI') AS tm
                   FROM complaints WHERE created_at >= %s ORDER BY id DESC LIMIT 10""", (since_s,))
    for r in cur.fetchall():
        d = dict(r)
        print('      %s [%s/%s] %s' % (d['tm'], d['t'], d['mch'] or '-', d['c']))
    if cn >= 10:
        ALERTS.append('投诉 %s 条（偏多，建议看原因分类）' % cn)

    # ---------------------------------------------------------------
    _h('⑥ 抽样校验：账本上有钱、接口却算 0 的用户（真"余额看不到"）')
    print('   抽样 %d 个"账本余额>0"的用户，用真实接口验一遍…' % args.sample)
    cur.execute("""SELECT phone, balance FROM user_balances
                   WHERE COALESCE(balance,0) > 0
                     AND phone IN (SELECT DISTINCT user_phone FROM orders
                                   WHERE created_at >= %s)
                   ORDER BY balance DESC LIMIT %s""", (since_s, args.sample))
    cands = [dict(r) for r in cur.fetchall()]
    if not cands:
        print('   （区间内没有账本余额>0 的用户）')
    else:
        try:
            import app as _a
            import json
            client = _a.app.test_client()
            bad = []
            for c in cands:
                p = c['phone']
                cur.execute("""SELECT openid, mp_openid FROM phone_openids
                               WHERE phone=%s ORDER BY id DESC LIMIT 1""", (p,))
                rr = cur.fetchone()
                if not rr:
                    continue
                dd = dict(rr)
                best = 0.0
                for oid in [x for x in (dd['openid'], dd['mp_openid']) if x]:
                    try:
                        j = json.loads(client.get('/api/user/balance?openid=%s&phone=%s'
                                                  % (oid, p)).get_data(as_text=True))
                        best = max(best, float((j.get('data') or {}).get('available_balance') or 0))
                    except Exception:
                        pass
                if best <= 0:
                    # 有进行中提现的算正常
                    cur.execute("""SELECT COUNT(*) AS n FROM withdrawal_records
                                   WHERE user_phone=%s AND status IN (0,1,2)""", (p,))
                    if int(dict(cur.fetchone())['n'] or 0) == 0:
                        bad.append((p, c['balance']))
            print('   ★ 账本有钱但接口算 0（无进行中提现）: %s 个' % len(bad))
            for p, b in bad[:10]:
                print('      %-14s 账本 ¥%s' % (p, b))
            if bad:
                ALERTS.append('账本有钱但接口算0：%s 个用户' % len(bad))
        except Exception as e:
            print('   ⚠️ 抽样失败: %s' % str(e)[:100])

    # ---------------------------------------------------------------
    _h('体检结论')
    if ALERTS:
        print('   ❌ 发现 %d 项异常：' % len(ALERTS))
        for i, a in enumerate(ALERTS, 1):
            print('      %d) %s' % (i, a))
        print('')
        print('   → 这些都会直接拉高投诉。建议先看上面的明细。')
        rc = 1
    else:
        print('   ✅ 全部正常，没有发现异常。')
        rc = 0

    cur.close(); db.close()

    # ---------- 推送到微信（--push） ----------
    if args.push:
        try:
            sys.path.insert(0, '/home/ubuntu/smart-locker')
            from helpers import send_pushplus
            if ALERTS:
                title = '❌ 寄存柜体检：%d 项异常' % len(ALERTS)
                lines = ['%s %s' % (datetime.now().strftime('%m-%d %H:%M'), '（近%d小时）' % args.hours), '']
                for i, a in enumerate(ALERTS, 1):
                    lines.append('%d) %s' % (i, a))
                lines += ['', '看明细：python3 scripts/daily_health.py']
            else:
                title = '✅ 寄存柜体检：全部正常'
                lines = ['%s（近%d小时）' % (datetime.now().strftime('%m-%d %H:%M'), args.hours), '',
                         '身份绑定、订单身份、余额流水、退款、投诉 全部正常。']
            ok = send_pushplus(title, '\n'.join(lines))
            print('[push] %s' % ('已推送' if ok else '推送失败（看上面提示）'))
        except Exception as e:
            print('[push] 异常: %s' % str(e)[:120])

    print('')
    return rc


if __name__ == '__main__':
    sys.exit(main())
