#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S830-20261007] 换新微信小程序：后端配置一键脚本（带 dry-run）

用途
----
拿到新小程序的 AppID / AppSecret / 订阅模板ID 之后，一条命令完成后端配置：
  1) 写/更新 wx_accounts（appid + secret + name + subject）
  2) 登记 3 个订阅模板（subscribe_general / subscribe_refund / subscribe_deposit）
  3) （可选 --activate）用 switch_to() 切换到新号 —— 自带探活防呆
  4) 打印「还需要人工做什么」清单（openid_prefix 回填、支付渠道绑定）

安全
----
  · 默认 dry-run 之外的写操作都要显式 --apply（不加就是只打印，绝不写库）
  · --activate 会调微信真实接口探活，探活不过会拒绝切换（wx_config 内置）
  · 不删任何东西；已有的旧账号记录一律保留

用法
----
  # 先看要改什么（不写库）
  python3 switch_wx_mp.py --appid wxXXXX --secret YYYY --name "新小程序" \
      --subject "某某公司" --tpl-general g1 --tpl-refund r1 --tpl-deposit d1

  # 确认无误后真正写库（不切生效号）
  python3 switch_wx_mp.py ... --apply

  # 写库 + 切换生效号（会先探活）
  python3 switch_wx_mp.py ... --apply --activate
"""
import sys
import argparse

sys.path.insert(0, '/home/ubuntu/smart-locker')

TPL_BIZ = [
    ('subscribe_general', 'tpl_general', '通用订阅通知'),
    ('subscribe_refund',  'tpl_refund',  '退款到账通知'),
    ('subscribe_deposit', 'tpl_deposit', '存包/结束寄存通知'),
]


def main():
    ap = argparse.ArgumentParser(description='换新微信小程序：后端配置')
    ap.add_argument('--appid', required=True, help='新小程序 AppID')
    ap.add_argument('--secret', default='', help='AppSecret（不填则只更新其它字段）')
    ap.add_argument('--name', default='', help='账号名称（后台显示用）')
    ap.add_argument('--subject', default='', help='主体公司名')
    ap.add_argument('--tpl-general', default='', help='subscribe_general 模板ID')
    ap.add_argument('--tpl-refund', default='', help='subscribe_refund 模板ID')
    ap.add_argument('--tpl-deposit', default='', help='subscribe_deposit 模板ID')
    ap.add_argument('--mch-id', default='', help='（仅提示）配套商户号')
    ap.add_argument('--apply', action='store_true', help='真正写库（缺省=只打印不写）')
    ap.add_argument('--activate', action='store_true', help='写库后切换为生效号（会探活）')
    a = ap.parse_args()

    DRY = not a.apply
    print('=' * 92)
    print('  换新微信小程序 —— 后端配置%s' % ('【预演 DRY-RUN，不写库】' if DRY else '【正式执行】'))
    print('=' * 92)
    print('  目标 AppID   : %s' % a.appid)
    print('  名称         : %s' % (a.name or '(未填)'))
    print('  主体         : %s' % (a.subject or '(未填)'))
    print('  AppSecret    : %s' % (('已填，长度=%d' % len(a.secret)) if a.secret else '(未填)'))
    for biz, attr, desc in TPL_BIZ:
        print('  模板 %-20s: %s' % (biz, (getattr(a, attr) or '(未填)')))
    if a.mch_id:
        print('  配套商户号   : %s' % a.mch_id)
    print()

    # ---------------- 0) 连库 + 现状 ----------------
    import wx_config as C
    try:
        from helpers import get_db
        C.bind(get_db)
    except Exception as e:
        print('  !! 绑定数据库失败: %s' % e)
        return 2

    rows = C.list_accounts('mp') or []
    print('########## 现状：wx_accounts 里的微信小程序（%d 个） ##########' % len(rows))
    for r in rows:
        mark = '★生效' if r.get('is_active') else '     '
        print('  %s id=%-3s appid=%-22s 前缀=%-8s %s' % (
            mark, r.get('id'), str(r.get('appid'))[:22],
            str(r.get('openid_prefix') or '-')[:8], str(r.get('name'))[:34]))

    target = next((r for r in rows if str(r.get('appid')) == a.appid), None)
    print()
    if target:
        print('  → 目标 appid 已存在：id=%s「%s」，将【更新】它' % (target.get('id'), target.get('name')))
    else:
        print('  → 目标 appid 不存在，将【新增】一条 wx_accounts 记录')

    # ---------------- 1) 写 wx_accounts ----------------
    print()
    print('########## 步骤 1：写 wx_accounts ##########')
    if target:
        upd = {}
        if a.secret:
            upd['secret'] = a.secret
        if a.name:
            upd['name'] = a.name
        if a.subject:
            upd['subject'] = a.subject
        if not upd:
            print('   （没有要更新的字段，跳过）')
        else:
            print('   要更新的字段: %s' % ', '.join(upd.keys()))
            if DRY:
                print('   [dry-run] 将调用 C.update_account(%s, ...)' % target.get('id'))
            else:
                try:
                    C.update_account(target.get('id'), **upd)
                    print('   ✅ 已更新 id=%s' % target.get('id'))
                except Exception as e:
                    print('   !! 更新失败: %s' % e)
                    return 2
        new_id = target.get('id')
    else:
        print('   新增字段: appid=%s, name=%s, subject=%s, secret=%s' % (
            a.appid, a.name or '(空)', a.subject or '(空)',
            ('长度%d' % len(a.secret)) if a.secret else '(空)'))
        if DRY:
            print('   [dry-run] 将调用 C.create_account(\'mp\', ...)')
            new_id = None
        else:
            if not (a.secret and a.name):
                print('   !! 新增账号必须同时给 --secret 和 --name')
                return 2
            try:
                new_id = C.create_account('mp', a.name, a.appid, a.secret,
                                          subject=a.subject, note='[S830] 换新微信小程序')
                print('   ✅ 已新增 id=%s' % new_id)
            except Exception as e:
                print('   !! 新增失败: %s' % e)
                return 2

    # ---------------- 2) 登记订阅模板 ----------------
    print()
    print('########## 步骤 2：登记订阅模板（channel=mp, account_id=%s） ##########' % new_id)
    tpl_args = {biz: (getattr(a, attr) or '').strip() for biz, attr, _ in TPL_BIZ}
    any_tpl = any(tpl_args.values())
    if not any_tpl:
        print('   （三个模板 ID 都没填，跳过）')
    else:
        for biz, _attr, desc in TPL_BIZ:
            tid = tpl_args[biz]
            if not tid:
                print('   %-20s (未填) —— 跳过；该功能在新号上不会收到通知' % biz)
                continue
            print('   %-20s -> %s   (%s)' % (biz, tid, desc))
            if DRY:
                print('        [dry-run] 将调用 C.set_template(%r, \'mp\', %r, account_id=%s)' % (biz, tid, new_id))
            else:
                try:
                    C.set_template(biz, 'mp', tid, account_id=(new_id or 0),
                                   note='[S830] 新微信小程序')
                    print('        ✅ 已登记')
                except Exception as e:
                    print('        !! 登记失败: %s' % e)

    # ---------------- 3) 切换生效号 ----------------
    print()
    print('########## 步骤 3：切换生效号 ##########')
    if not a.activate:
        print('   （未加 --activate，跳过切换）')
        print('   将来要切的时候执行：')
        print('     python3 -c "import sys;sys.path.insert(0,\'/home/ubuntu/smart-locker\');'
              'import wx_config as C; from helpers import get_db; C.bind(get_db);'
              'print(C.switch_to(\'mp\', %s, reason=\'换新微信小程序\'))"' % (new_id or '<id>'))
    else:
        if DRY or new_id is None:
            print('   [dry-run] 将调用 C.switch_to(\'mp\', %s)（会先探活，不通就拒绝切换）' % (new_id or '<id>'))
        else:
            print('   正在探活 + 切换（会真调微信 cgi-bin/token）...')
            ok, msg = C.switch_to('mp', new_id, reason='换新微信小程序', operator='S830')
            print('   %s %s' % ('✅' if ok else '!!', msg))
            if not ok:
                print('   → 切换被拒绝。请先把 AppID/AppSecret 改成真实的、或先在后台点探活。')

    # ---------------- 4) 还需要人工做什么 ----------------
    rows2 = C.list_accounts('mp') or []
    tgt = next((r for r in rows2 if str(r.get('appid')) == a.appid), None)
    print()
    print('########## 步骤 4：还需要人工做的事 ##########')
    print('  ① ★ 回填 openid_prefix（最容易漏，不做新用户会被认错号）')
    print('     方法：新小程序上传体验版 → 用手机扫一次 → 看日志里的 [wx_login] 输出')
    print('     命令：ssh 到生产后  grep "\\[wx_login\\]" /var/log/... 或 journalctl -u smart-locker')
    print('     拿到 openid 后取【前 6 位】，写进 wx_accounts.openid_prefix')
    if tgt:
        print('     当前 openid_prefix = %r' % (tgt.get('openid_prefix') or ''))
    print('  ② 支付渠道 payment_channels：新增/启用绑这个 appid 的商户号（is_active=1）')
    print('     注意：wx_accounts.mch_relation 不是 ok 的话，switch_to 会警告"支付可能失败"')
    print('  ③ 微信小程序后台：服务器域名(request 合法域名) + 业务域名')
    print('  ④ 微信商户平台：把新 appid 关联到商户号（产品中心 → AppID 账号管理）')
    if a.mch_id:
        print('  ⑤ 你给的配套商户号是 %s —— 确认它在 payment_channels 里且绑的是新 appid' % a.mch_id)

    print()
    print('=' * 92)
    print('  %s' % ('【预演结束】确认无误后把 --apply 加上重跑一次' if DRY else '【执行结束】'))
    print('  回滚：wx_accounts 里把旧号 is_active 设回 1 即可（switch_to 会写切换日志）')
    print('=' * 92)
    return 0


if __name__ == '__main__':
    sys.exit(main())
