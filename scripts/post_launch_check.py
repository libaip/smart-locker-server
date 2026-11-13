#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S889] 上线后全面检测 —— 针对某个小程序，查配置 + 真实业务数据 + 异常

用法：
    python3 scripts/post_launch_check.py                    # 自动取当前生效小程序
    python3 scripts/post_launch_check.py --appid wx...      # 指定
    python3 scripts/post_launch_check.py --hours 6          # 统计区间
"""
import sys
import os
import argparse
import logging
import subprocess
from datetime import datetime, timedelta

logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as _a                              # noqa: E402
from database import get_db                   # noqa: E402
import helpers                                # noqa: E402

BAD = []
WARN = []


def h(t):
    print('')
    print('=' * 86)
    print('  ' + t)
    print('=' * 86)


def line(k, v, mark=''):
    print('   %-30s %s %s' % (k, v, mark))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--appid', default='')
    ap.add_argument('--hours', type=int, default=24)
    args = ap.parse_args()

    since = (datetime.now() - timedelta(hours=args.hours)).strftime('%Y-%m-%d %H:%M:%S')
    db = get_db()
    cur = db.cursor()

    appid = args.appid.strip()
    if not appid:
        cur.execute("""SELECT appid FROM wx_accounts WHERE acct_type='mp'
                       ORDER BY is_active DESC, priority ASC, id DESC LIMIT 1""")
        r = cur.fetchone()
        appid = (dict(r)['appid'] if r else '') or ''

    print('=' * 86)
    print('  上线后全面检测')
    print('  小程序: %s' % appid)
    print('  时间: %s    区间: 近 %d 小时' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), args.hours))
    print('=' * 86)

    # ---- 账号 ----
    h('一、账号与配置')
    cur.execute("""SELECT id, name, appid, openid_prefix, subject, mch_relation, is_active, secret
                   FROM wx_accounts WHERE appid=%s""", (appid,))
    r = cur.fetchone()
    if not r:
        print('   ❌ 账号没登记'); BAD.append('账号未登记')
        acc = None
    else:
        acc = dict(r)
        line('账号', '%s (id=%s)' % (acc['name'], acc['id']))
        line('openid 前缀', acc['openid_prefix'] or '(空)')
        line('主体', acc['subject'] or '(空)')
        line('商户关系', acc['mch_relation'])
        line('启用', acc['is_active'])
        line('secret', '已配置' if (acc['secret'] or '').strip() else '❌ 缺失')
        if not (acc['secret'] or '').strip():
            BAD.append('secret 缺失')

    # ---- 渠道 ----
    h('二、支付渠道')
    cur.execute("""SELECT id, name, mch_id, app_id, is_active, cert_name,
                          COALESCE(total_count,0) AS cnt, COALESCE(total_amount,0) AS amt
                   FROM payment_channels WHERE app_id=%s ORDER BY is_active DESC, id""", (appid,))
    chs = [dict(x) for x in cur.fetchall()]
    if not chs:
        print('   ❌ 没有渠道绑定'); BAD.append('无支付渠道')
        ch_ids = []
    else:
        ch_ids = [c['id'] for c in chs if str(c['is_active']) == '1']
        for c in chs:
            line('渠道 %s' % c['id'], '%s mch=%s 启用=%s 证书=%s 累计%d单/¥%s' % (
                c['name'], c['mch_id'], c['is_active'], c['cert_name'] or '-', c['cnt'], c['amt']))
        if not ch_ids:
            BAD.append('没有启用的渠道')
        else:
            print('   ✅ 启用渠道: %s' % ch_ids)

    # ---- 真实流量 ----
    h('三、真实业务数据（区间内）')
    if ch_ids:
        cur.execute("""SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE status >= 2) AS paid,
                              COUNT(*) FILTER (WHERE status = 1) AS unpaid,
                              COUNT(*) FILTER (WHERE status = 5) AS cancelled,
                              COUNT(*) FILTER (WHERE status = 3) AS done,
                              COUNT(*) FILTER (WHERE status = 4) AS refunded,
                              ROUND(COALESCE(SUM(deposit_amount) FILTER (WHERE status >= 2),0)::numeric,2) AS amt
                       FROM orders WHERE payment_channel_id = ANY(%s) AND created_at >= %s""",
                    (ch_ids, since))
        d = dict(cur.fetchone())
        total, paid = int(d['total'] or 0), int(d['paid'] or 0)
        line('订单总数', total)
        line('已支付', '%s（未付 %s / 已取消 %s）' % (paid, d['unpaid'], d['cancelled']))
        line('已取包', d['done'])
        line('已退款', d['refunded'])
        line('收款合计', '¥%s' % d['amt'])
        if total:
            rate = round(100.0 * paid / total, 1)
            line('支付转化率', '%s%%' % rate, '✅' if rate >= 70 else '⚠️')
            if rate < 70:
                WARN.append('支付转化率偏低 %s%%' % rate)
            if int(d['unpaid'] or 0) > total * 0.3:
                WARN.append('未支付订单偏多（%s）' % d['unpaid'])

        # 身份完整率（区分"区间内"与"最近2小时"——后者才是"现在还在不在发生"）
        cur.execute("""SELECT COUNT(*) AS n,
                              COUNT(*) FILTER (WHERE COALESCE(mp_openid,'')='') AS nomp,
                              COUNT(*) FILTER (WHERE user_id IS NULL OR user_id = 0) AS nouid
                       FROM orders WHERE payment_channel_id = ANY(%s) AND created_at >= %s""",
                    (ch_ids, since))
        d = dict(cur.fetchone())
        line('订单身份（区间内）', 'mp_openid 空 %s / user_id 为0 %s' % (d['nomp'], d['nouid']),
             '（含修复前历史）' if int(d['nomp'] or 0) else '✅')
        cur.execute("""SELECT COUNT(*) AS n,
                              COUNT(*) FILTER (WHERE COALESCE(mp_openid,'')='') AS nomp,
                              COUNT(*) FILTER (WHERE user_id IS NULL OR user_id = 0) AS nouid
                       FROM orders WHERE payment_channel_id = ANY(%s)
                         AND created_at >= NOW() - INTERVAL '2 hours'""", (ch_ids,))
        d2 = dict(cur.fetchone())
        line('订单身份（近2小时）', '%s 笔中 mp空 %s / user_id为0 %s' % (d2['n'], d2['nomp'], d2['nouid']),
             '✅' if int(d2['nomp'] or 0) == 0 and int(d2['nouid'] or 0) == 0 else '❌')
        if int(d2['nomp'] or 0) > 0 or int(d2['nouid'] or 0) > 0:
            BAD.append('近2小时订单身份残缺（mp空%s/user_id0 %s）' % (d2['nomp'], d2['nouid']))

    # 身份绑定率（该渠道涉及的用户）
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE COALESCE(user_id,0)=0) AS zero
                   FROM phone_openids WHERE created_at >= %s""", (since,))
    d = dict(cur.fetchone())
    n = int(d['n'] or 0)
    if n:
        rate = round(100.0 * (n - int(d['zero'] or 0)) / n, 1)
        line('新增身份绑定率', '%s%%（%s 条中 %s 条没绑上）' % (rate, n, d['zero']),
             '✅' if rate >= 95 else '❌')
        if rate < 95:
            BAD.append('新增身份绑定率 %s%%' % rate)

    # ---- 余额可见性 ----
    h('四、余额可见性（用户能不能看到自己的钱）')
    cur.execute("""SELECT COUNT(DISTINCT user_phone) AS n FROM user_balance_details
                   WHERE status='available' AND created_at >= %s""", (since,))
    line('区间内产生可用余额的用户', dict(cur.fetchone())['n'])
    cur.execute("""SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(bd.amount),0)::numeric,2) AS amt
                   FROM user_balance_details bd JOIN orders o ON o.id=bd.order_id
                   WHERE bd.status='pending' AND bd.created_at >= %s
                     AND bd.created_at < NOW() - INTERVAL '1 hour'
                     AND o.status=3 AND COALESCE(NULLIF(o.refund_status,''),'none')='none'
                     AND NOT EXISTS (SELECT 1 FROM withdrawal_records w
                                      WHERE w.order_id=bd.order_id AND w.status IN (0,1,2))""", (since,))
    d = dict(cur.fetchone())
    line('区间内新卡住的流水', '%s 笔 ¥%s' % (d['n'], d['amt']),
         '✅' if int(d['n'] or 0) == 0 else '❌')
    if int(d['n'] or 0) > 0:
        BAD.append('有新卡住流水 %s 笔' % d['n'])

    # ---- 退款/提现 ----
    h('五、退款/提现')
    cur.execute("""SELECT COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE status=2) AS ok,
                          COUNT(*) FILTER (WHERE status=3) AS fail,
                          COUNT(*) FILTER (WHERE status=3 AND COALESCE(error_msg,'') LIKE '自动拒绝%%') AS rej
                   FROM withdrawal_records WHERE created_at >= %s""", (since,))
    d = dict(cur.fetchone())
    n = int(d['n'] or 0)
    line('提现/退款总数', '%s（成功 %s / 失败 %s，其中自动拒绝 %s）' % (n, d['ok'], d['fail'], d['rej']))
    if n:
        rr = round(100.0 * int(d['rej'] or 0) / n, 1)
        line('自动拒绝率', '%s%%' % rr, '✅' if rr <= 25 else '⚠️')
        if rr > 25:
            WARN.append('自动拒绝率偏高 %s%%' % rr)

    # ---- 异常日志 ----
    h('六、异常日志（区间内）')
    try:
        out = subprocess.run(
            ['journalctl', '-u', 'smart-locker', '-u', 'smart-locker-admin',
             '--since', since, '--no-pager'],
            capture_output=True, text=True, timeout=60).stdout or ''
        tb = out.count('Traceback')
        err = len([l for l in out.splitlines() if ' ERROR ' in l or 'CRITICAL' in l])
        # 已知噪音：客户端断开连接（socket sendall / ConnectionReset）——无害，不算异常
        noise_kw = ('sendall', 'ConnectionResetError', 'BrokenPipeError',
                    'client disconnected', 'socket.error')
        tb_lines = [i for i, l in enumerate(out.splitlines()) if 'Traceback' in l]
        noise = 0
        for i in tb_lines:
            blk = '\n'.join(out.splitlines()[i:i + 14])
            if any(k in blk for k in noise_kw):
                noise += 1
        real_tb = tb - noise
        line('Traceback', '%s（其中客户端断开 %s 个·无害 / 真异常 %s 个）' % (tb, noise, real_tb),
             '✅' if real_tb == 0 else '❌')
        line('ERROR 行数', err, '✅' if err < 20 else '⚠️')
        if real_tb > 0:
            BAD.append('日志有 %s 个真异常 Traceback' % real_tb)
            print('   真异常片段:')
            for i in tb_lines:
                blk = out.splitlines()[i:i + 14]
                if not any(k in '\n'.join(blk) for k in noise_kw):
                    for l in blk[-3:]:
                        print('     %s' % l[-150:])
                    break
    except Exception as e:
        line('日志检查', '跳过（%s）' % str(e)[:60])

    # ---- 订阅模板 ----
    h('七、订阅消息模板')
    if acc:
        cur.execute("""SELECT biz, LEFT(template_id,20) AS t FROM wx_templates
                       WHERE account_id=%s ORDER BY biz""", (acc['id'],))
        ts = [dict(x) for x in cur.fetchall()]
        if ts:
            for t in ts:
                line(t['biz'], t['t'] + '…')
        else:
            line('专属模板', '无（回落通用模板）', '⚠️')
        for biz in ('subscribe_deposit', 'subscribe_general', 'subscribe_refund'):
            cur.execute("""SELECT COUNT(*) AS n FROM wx_templates
                           WHERE biz=%s AND account_id=%s""", (biz, acc['id']))
            c = int(dict(cur.fetchone())['n'] or 0)
            if c == 0:
                # 若该通知在代码里是关闭的，就不算问题
                off = False
                if biz == 'subscribe_deposit':
                    try:
                        rr = subprocess.run(['grep', '-n', '_SEND_STORAGE_SUCCESS_NOTIFY = False',
                                             'routes/payment.py'], capture_output=True, text=True)
                        off = bool((rr.stdout or '').strip())
                    except Exception:
                        off = False
                if off:
                    print('   ℹ️ 缺少专属模板: %s —— 但代码里该通知【已关闭】，当前无影响' % biz)
                else:
                    print('   ⚠️ 缺少专属模板: %s（对应通知发不出）' % biz)
                    WARN.append('缺模板 %s' % biz)

    # ---- 结论 ----
    h('检测结论')
    print('   ❌ 必须处理: %d 项' % len(BAD))
    for i, x in enumerate(BAD, 1):
        print('      %d) %s' % (i, x))
    print('   ⚠️ 建议处理: %d 项' % len(WARN))
    for i, x in enumerate(WARN, 1):
        print('      %d) %s' % (i, x))
    if not BAD and not WARN:
        print('   ✅ 全部正常，可以放心用')
    cur.close(); db.close()
    print('')
    return 1 if BAD else 0


if __name__ == '__main__':
    sys.exit(main())
