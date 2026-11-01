#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S885] 换小程序上线自检 —— 切换/新增小程序前跑一遍，5 分钟出结论

用法：
    cd /home/ubuntu/smart-locker && python3 scripts/switch_check.py
    python3 scripts/switch_check.py --appid wx191a9edc8397d0a5
    python3 scripts/switch_check.py --skip-wx      # 不调微信接口（离线检查）
    python3 scripts/switch_check.py --no-e2e       # 跳过端到端模拟

退出码：0 = 全绿可上线；1 = 有红项，先别上线
检查项：
    1 账号登记（appid/secret/前缀/主体/启用）
    2 微信侧 secret 有效性（换 access_token）
    3 前缀识别链路（新小程序身份能不能被正确认出来）
    4 支付渠道（有没有绑定该 appid 的启用渠道 + 商户号/证书）
    5 订阅消息模板（wx_templates + 字段映射）
    6 订单身份卫生（历史污染检查）
    7 端到端模拟（登录→存包→支付身份→取包→看余额）
"""
import sys
import os
import json
import argparse
import logging
from datetime import datetime

logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as _a                      # noqa: E402
from database import get_db           # noqa: E402
import helpers                        # noqa: E402

FAIL = []
WARN = []
PASS = []


def h(t):
    print('')
    print('=' * 84)
    print('  ' + t)
    print('=' * 84)


def ok(m):
    print('   ✅ %s' % m)
    PASS.append(m)


def bad(m):
    print('   ❌ %s' % m)
    FAIL.append(m)


def warn(m):
    print('   ⚠️  %s' % m)
    WARN.append(m)


def info(m):
    print('      %s' % m)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--appid', default='')
    ap.add_argument('--skip-wx', action='store_true')
    ap.add_argument('--no-e2e', action='store_true')
    args = ap.parse_args()

    db = get_db()
    cur = db.cursor()

    print('=' * 84)
    print('  换小程序 · 上线自检')
    print('  时间: %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 84)

    # 先确定要检查哪个 appid
    appid = args.appid.strip()
    if not appid:
        try:
            cur.execute("""SELECT appid FROM wx_accounts WHERE acct_type='mp'
                           ORDER BY is_active DESC, priority ASC, id DESC LIMIT 1""")
            r = cur.fetchone()
            appid = (dict(r)['appid'] if r else '') or ''
        except Exception:
            appid = ''
    if not appid:
        print('\n   ❌ 没有找到任何小程序账号，请用 --appid wx... 指定')
        return 1
    print('\n   目标小程序 appid = %s' % appid)

    # =============================================================
    h('① 账号登记（wx_accounts）')
    cur.execute("""SELECT id, acct_type, name, appid, secret, openid_prefix, subject,
                          mch_relation, is_active, priority
                   FROM wx_accounts WHERE appid=%s""", (appid,))
    r = cur.fetchone()
    if not r:
        bad('wx_accounts 里没有登记这个 appid —— 登录会失败！')
        acc = None
    else:
        acc = dict(r)
        info('id=%s 名称=%s 类别=%s' % (acc['id'], acc['name'], acc['acct_type']))
        info('前缀=%s  主体=%s  商户关系=%s  启用=%s' % (
            acc['openid_prefix'] or '(空)', acc['subject'] or '(空)',
            acc['mch_relation'], acc['is_active']))
        if acc['acct_type'] != 'mp':
            bad('acct_type=%s（应为 mp）' % acc['acct_type'])
        else:
            ok('类别正确 (mp)')
        if not (acc['secret'] or '').strip():
            bad('secret 为空 —— 换不到 access_token')
        else:
            ok('secret 已配置')
        if not (acc['openid_prefix'] or '').strip():
            warn('openid_prefix 为空 —— 首次登录时会自动回填，但建议现在就填')
        else:
            ok('openid_prefix = %s' % acc['openid_prefix'])
        if str(acc['is_active']) != '1':
            warn('is_active=%s（未启用；按 appid 登录不受影响，但"当前生效账号"逻辑会用到）' % acc['is_active'])
        else:
            ok('账号已启用')
        if not (acc['subject'] or '').strip():
            warn('subject 为空（只是备注，不影响功能）')

    # =============================================================
    h('② 微信侧 secret 有效性')
    if args.skip_wx:
        info('（--skip-wx 已跳过）')
    elif not acc or not (acc['secret'] or '').strip():
        bad('没有 secret，跳过')
    else:
        try:
            import urllib.request
            url = ('https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential'
                   '&appid=%s&secret=%s' % (appid, acc['secret']))
            with urllib.request.urlopen(url, timeout=12) as resp:
                j = json.loads(resp.read().decode('utf-8'))
            if j.get('access_token'):
                ok('secret 有效（成功换到 access_token，长度 %d）' % len(j['access_token']))
            else:
                bad('secret 无效: errcode=%s errmsg=%s' % (j.get('errcode'), j.get('errmsg')))
        except Exception as e:
            warn('调用微信失败（网络？）: %s' % str(e)[:90])

    # =============================================================
    h('③ 前缀识别链路（新小程序身份能不能被认出来）')
    pfx = (acc or {}).get('openid_prefix') or ''
    try:
        np = helpers.new_mp_openid_prefix()
        info('new_mp_openid_prefix() = %s' % np)
        if pfx and np != pfx:
            warn('"当前生效小程序前缀" = %s，与目标 %s 不一致（多小程序并存时正常）' % (np, pfx))
    except Exception as e:
        bad('new_mp_openid_prefix() 异常: %s' % str(e)[:80])
    try:
        allp = helpers.mp_prefixes_all()
        info('mp_prefixes_all() = %s' % (allp,))
        if pfx and pfx in allp:
            ok('目标前缀已在"全部小程序前缀"里')
        elif pfx:
            bad('目标前缀 %s 不在 mp_prefixes_all() 里 —— 身份会被误判！' % pfx)
    except Exception as e:
        bad('mp_prefixes_all() 异常: %s' % str(e)[:80])

    if pfx:
        test_openid = pfx + 'SWITCHCHECKSAMPLE0123456789'
        try:
            k, v = helpers._s521_ident_kind(openid=test_openid)
            info('_s521_ident_kind(openid=%s…) -> (%s, %s…)' % (pfx, k, str(v)[:10]))
            if k == 'mp_openid':
                ok('身份类别判定正确（mp_openid）')
            else:
                bad('身份类别判成了 %s（应为 mp_openid）—— 会导致认错身份列！' % k)
        except Exception as e:
            bad('_s521_ident_kind 异常: %s' % str(e)[:80])
        try:
            im = helpers.is_new_mp_identity(openid=test_openid)
            info('is_new_mp_identity(openid=…) = %s' % im)
            if im:
                ok('被识别为"新体系"身份')
            else:
                warn('未被识别为"新体系"（strict 模式下可能按老逻辑认人）')
        except Exception as e:
            warn('is_new_mp_identity 异常: %s' % str(e)[:80])
    else:
        warn('没有前缀，跳过前缀链路检查')

    # =============================================================
    h('④ 支付渠道（有没有绑定该 appid 的启用渠道）')
    cur.execute("""SELECT id, name, channel_type, mch_id, app_id, is_active, cert_name,
                          COALESCE(total_count,0) AS cnt
                   FROM payment_channels WHERE app_id=%s ORDER BY is_active DESC, id""", (appid,))
    chs = [dict(x) for x in cur.fetchall()]
    if not chs:
        bad('没有任何支付渠道绑定这个 appid —— 用户付不了款！')
    else:
        act = [c for c in chs if str(c['is_active']) == '1']
        info('共 %d 条渠道，其中启用 %d 条' % (len(chs), len(act)))
        for c in chs:
            info('  渠道%-4s %-18s mch=%-12s 启用=%s 证书=%s 单量=%s' % (
                c['id'], c['name'], c['mch_id'], c['is_active'], c['cert_name'] or '-', c['cnt']))
        if not act:
            bad('没有任何【启用】的渠道 —— 用户付不了款！')
        else:
            ok('有启用的支付渠道: %s' % ','.join(str(c['id']) for c in act))
            if len(act) > 1:
                warn('启用了多条渠道，注意轮询/权重是否符合预期')
            for c in act:
                cur.execute("""SELECT COUNT(*) AS n FROM payment_channels
                               WHERE id=%s AND (COALESCE(mch_id,'')='' OR COALESCE(api_key,'')='')""",
                            (c['id'],))
                if int(dict(cur.fetchone())['n'] or 0) > 0:
                    bad('渠道 %s 缺 mch_id 或 api_key' % c['id'])

    # =============================================================
    h('⑤ 订阅消息模板')
    row = None
    if acc:
        cur.execute("""SELECT id FROM wx_accounts WHERE appid=%s LIMIT 1""", (appid,))
        rr = cur.fetchone()
        if rr:
            row = dict(rr)['id']
    if row is None:
        warn('拿不到账号 id，跳过模板检查')
    else:
        cur.execute("""SELECT biz, template_id, page FROM wx_templates
                       WHERE account_id=%s ORDER BY biz""", (row,))
        ts = [dict(x) for x in cur.fetchall()]
        if not ts:
            warn('该账号没有专属模板行（会回落到 account_id=0 的通用模板）')
        else:
            for t in ts:
                info('  %-20s %s…  page=%s' % (t['biz'], str(t['template_id'])[:18], t['page']))
            ok('已配置 %d 个专属模板' % len(ts))
        # 关键 biz 是否齐全
        for biz in ('subscribe_deposit', 'subscribe_general', 'subscribe_refund'):
            cur.execute("""SELECT COUNT(*) AS n FROM wx_templates
                           WHERE biz=%s AND (account_id=%s OR account_id=0)""", (biz, row))
            if int(dict(cur.fetchone())['n'] or 0) == 0:
                warn('缺少模板 %s（对应通知发不出去）' % biz)
            cur.execute("""SELECT COUNT(*) AS n FROM wx_templates
                           WHERE biz=%s AND account_id=%s""", (biz, row))
            info('   %s 专属行: %s' % (biz, dict(cur.fetchone())['n']))
        # 字段映射
        try:
            mp = getattr(helpers, '_SUBSCRIBE_FIELD_MAP', {})
            got = [k for k in mp.keys() if isinstance(k, tuple) and k[0] == row]
            if got:
                ok('字段映射已覆盖该账号: %s' % ','.join(k[1] for k in got))
            else:
                warn('字段映射里没有 account_id=%s 的条目（会用通用映射）' % row)
        except Exception as e:
            warn('字段映射检查异常: %s' % str(e)[:60])

    # =============================================================
    h('⑥ 订单身份卫生（历史污染检查）')
    cur.execute("""SELECT COUNT(*) AS n FROM orders
                   WHERE app_id IS NOT NULL AND openid LIKE 'ofEDK%%' AND COALESCE(mp_openid,'')=''""")
    n1 = int(dict(cur.fetchone())['n'] or 0)
    info('openid 是公众号、mp_openid 为空的订单: %s 笔（历史遗留，不影响新单）' % n1)
    cur.execute("""SELECT COUNT(*) AS n FROM orders
                   WHERE created_at >= NOW() - INTERVAL '2 hours'
                     AND COALESCE(mp_openid,'')=''""")
    n2 = int(dict(cur.fetchone())['n'] or 0)
    if n2 > 0:
        warn('最近 2 小时有 %s 笔订单 mp_openid 为空 —— 留意身份回填是否正常' % n2)
    else:
        ok('最近 2 小时订单身份都完整')

    # =============================================================
    h('⑦ 端到端模拟（登录 → 存包身份 → 取包 → 看余额）')
    if args.no_e2e:
        info('（--no-e2e 已跳过）')
    else:
        TEST_OPENID = (pfx or 'zzzzzz') + 'SWITCHCHECK0123456789'
        TEST_PHONE = '13900008899'
        TEST_ORDER = 'SWITCHCHECK' + datetime.now().strftime('%m%d%H%M')
        try:
            # 清理
            for sql, a in [("DELETE FROM user_balance_details WHERE user_phone=%s", (TEST_PHONE,)),
                           ("DELETE FROM user_balances WHERE phone=%s", (TEST_PHONE,)),
                           ("DELETE FROM phone_openids WHERE phone=%s", (TEST_PHONE,)),
                           ("DELETE FROM orders WHERE order_no=%s", (TEST_ORDER,))]:
                cur.execute(sql, a)
            db.commit()

            client = _a.app.test_client()
            resp = client.post('/api/user/link-openid', json={
                'phone': TEST_PHONE, 'openid': TEST_OPENID, 'mp_openid': TEST_OPENID,
                'appid': appid, 'wechat_name': 'SWITCHCHECK'})
            info('POST /api/user/link-openid -> HTTP %s' % resp.status_code)
            cur.execute("SELECT user_id FROM phone_openids WHERE phone=%s", (TEST_PHONE,))
            rr = cur.fetchone()
            uid = int(dict(rr)['user_id'] or 0) if rr else 0
            if uid:
                ok('身份绑定成功 user_id=%s' % uid)
            else:
                bad('身份没绑上 user_id —— 正是"余额显示 0"的根因！')

            # 造订单 + 流水
            cur.execute("""SELECT cabinet_id, slot_id, compartment_number, access_code, group_id,
                                  deposit_amount, per_use_price, payment_channel_id
                           FROM orders WHERE status=3 AND cabinet_id IS NOT NULL
                           ORDER BY id DESC LIMIT 1""")
            t = cur.fetchone()
            if t and uid:
                t = dict(t)
                cur.execute("""INSERT INTO orders (order_no, user_phone, cabinet_id, slot_id,
                                   compartment_number, access_code, deposit_amount, per_use_price,
                                   status, store_time, group_id, payment_channel_id,
                                   openid, mp_openid, user_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,3,NOW(),%s,%s,'',%s,%s)
                               RETURNING id""",
                            (TEST_ORDER, TEST_PHONE, t['cabinet_id'], t['slot_id'],
                             t['compartment_number'], t['access_code'], t['deposit_amount'],
                             t['per_use_price'], t['group_id'], t['payment_channel_id'],
                             TEST_OPENID, uid))
                oid = dict(cur.fetchone())['id']
                helpers.upsert_user_balance_row(cur, phone=TEST_PHONE, openid='',
                                                mp_openid=TEST_OPENID, user_id=uid,
                                                strict_identity=True)
                cur.execute("""INSERT INTO user_balance_details (user_phone, order_id, amount, status)
                               VALUES (%s,%s,%s,'available') ON CONFLICT (order_id) DO NOTHING""",
                            (TEST_PHONE, oid, t['deposit_amount']))
                db.commit()
                info('造测试订单 id=%s 押金=%s' % (oid, t['deposit_amount']))
                j = json.loads(client.get('/api/user/balance?openid=%s&phone=%s'
                                          % (TEST_OPENID, TEST_PHONE)).get_data(as_text=True))
                bal = (j.get('data') or {}).get('available_balance')
                info('GET /api/user/balance -> available_balance=%s' % bal)
                if bal is not None and float(bal or 0) > 0:
                    ok('端到端通过：钱包能看到钱（%s）' % bal)
                else:
                    bad('端到端失败：钱包显示 0 —— 上线前必须查清！')
            else:
                warn('没有可复制的订单模板，跳过造单环节')

            # 清理
            for sql, a in [("DELETE FROM user_balance_details WHERE user_phone=%s", (TEST_PHONE,)),
                           ("DELETE FROM user_balances WHERE phone=%s", (TEST_PHONE,)),
                           ("DELETE FROM phone_openids WHERE phone=%s", (TEST_PHONE,)),
                           ("DELETE FROM orders WHERE order_no=%s", (TEST_ORDER,))]:
                cur.execute(sql, a)
            db.commit()
            info('（测试数据已清理）')
        except Exception as e:
            bad('端到端模拟异常: %s' % str(e)[:120])

    # =============================================================
    h('自检结论')
    print('   ✅ 通过 %d 项    ⚠️ 提醒 %d 项    ❌ 失败 %d 项' % (len(PASS), len(WARN), len(FAIL)))
    if FAIL:
        print('')
        print('   ❌ 有失败项，【先别上线】：')
        for i, f in enumerate(FAIL, 1):
            print('      %d) %s' % (i, f))
    if WARN:
        print('')
        print('   ⚠️ 提醒（不影响上线，但建议处理）：')
        for i, w in enumerate(WARN, 1):
            print('      %d) %s' % (i, w))
    if not FAIL:
        print('')
        print('   → 关键项全绿，可以上线；上线后马上跑一次 daily_health.py 复查。')
    cur.close(); db.close()
    print('')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
