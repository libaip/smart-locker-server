# -*- coding: utf-8 -*-
"""
种子数据：把生产环境「当前真实」的小程序 / 公众号 / 模板ID / 域名 写进配置中心。
所有值都是从 106 服务器代码里读出来的真实值，不是编的。

注意：这个文件只应该被 wxcfg_3a_seed.py 通过 import 使用（那里会把连接绑到
      175 生产库）。直接 python3 seed_prod.py 跑是没有绑定数据库的，会报错，
      这是故意的——避免不小心建成一个本地 sqlite 库。
"""

import wx_config as C

# ============================================================
# 账号池（mp=小程序，oa=公众号）
# ============================================================
SEED_ACCOUNTS = [
    # --- 小程序 ---
    dict(acct_type='mp', name='伧置(当前使用)', appid='wxcabd4cbdb3096c4b',
         secret='f8d9d68772401f4fdda4a2d2d6143988', token='smartlocker2024',
         subject='重庆科莱维科技有限公司', mch_relation='ok',
         priority=10, is_active=1,
         note='当前正式使用；config.WX_MP_APP_ID'),
    dict(acct_type='mp', name='备用小程序-异主体(待注册)', appid='REPLACE_ME_MP_BACKUP',
         secret='', subject='（待定：另找一家公司主体）', mch_relation='pending',
         priority=20, is_active=0,
         note='计划：异主体注册→开放平台控制台绑到同一开放平台账号(unionid 一致, 异主体上限5个)→商户平台加关联appid→域名白名单→订阅模板申请'),
    dict(acct_type='mp', name='科莱智(旧号-已停用)', appid='wx57eaea52dcfff4e8',
         secret='', subject='重庆科莱维科技有限公司', mch_relation='none',
         priority=90, is_active=0,
         note='旧小程序，功能已迁到伧置；system_settings.mp_appid 里还残留这个值（没人读，建议清理）'),
    # --- 公众号 ---
    dict(acct_type='oa', name='智能寄存柜(当前使用)', appid='wxd85204d0ec930d46',
         secret='552e27fa9a260a6640bf6983bd3470f5', token='smartlocker2024',
         subject='重庆科莱维科技有限公司', mch_relation='ok',
         priority=10, is_active=1,
         note='当前正式使用；config.WX_APP_ID；H5 网页授权 + 模板消息都走它'),
    dict(acct_type='oa', name='备用公众号-异主体(待注册)', appid='REPLACE_ME_OA_BACKUP',
         secret='', subject='（待定：另找一家公司主体）', mch_relation='pending',
         priority=20, is_active=0,
         note='计划：异主体注册→绑同一开放平台账号(unionid 一致)；注意微信"群体发消息"每月限 4 条/用户'),
]

# ============================================================
# 消息模板（真实 ID，实测可发）
#   channel='mp' → 小程序订阅通知(/cgi-bin/message/subscribe/bizsend，仅能跳小程序)
#   channel='oa' → 公众号模板消息(/cgi-bin/message/template/send，需用户已关注，不限量)
# ============================================================
SEED_TEMPLATES = [
    # --- 小程序订阅通知（routes/user.py:/user/subscribe-templates 里硬编码的那三个）---
    dict(biz='subscribe_deposit', channel='mp',
         template_id='Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk',
         page='pages/subscribe/subscribe',
         fields={'thing1': '寄存网点', 'thing2': '柜门号', 'amount3': '金额', 'time4': '时间', 'thing15': '备注'},
         note='寄存成功；原代码 routes/user.py:_deposit'),
    dict(biz='subscribe_refund', channel='mp',
         template_id='lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c',
         page='pages/mine/mine',
         fields={'character_string1': '订单号', 'amount2': '退款金额', 'time5': '时间', 'thing4': '状态', 'thing3': '备注'},
         note='退款成功；原代码 routes/user.py:_withdraw'),
    dict(biz='subscribe_general', channel='mp',
         template_id='PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA',
         page='pages/mine/mine',
         fields={'amount1': '金额', 'time2': '时间', 'thing3': '状态', 'thing4': '备注'},
         note='押金退还；原代码里硬编码 8 处（app.py:1187、routes/user.py:806/1076/1646/1874/3195、admin_v2.py:1264/6809）'),
    # --- 公众号模板消息（实测发过 errcode=0 的四个）---
    dict(biz='oa_deposit_ok', channel='oa',
         template_id='5z2doOa5kTMCePS8EBsEHxC4zP30l_JfH-JbHWK7YGE',
         page='',
         fields={'thing8': '网点', 'character_string7': '柜门号', 'amount9': '金额', 'time1': '时间'},
         note='存储柜寄存成功通知'),
    dict(biz='oa_deposit_end', channel='oa',
         template_id='j-BcmxGLg7zef8NfHBZ9vE3BfAQj9_9RRrFUuSjRtWc',
         page='',
         fields={'thing1': '网点', 'character_string7': '柜门号', 'time2': '开始时间', 'time3': '结束时间'},
         note='寄存结束通知'),
    dict(biz='oa_refund_ok', channel='oa',
         template_id='vbVbCS87M6lAnv7U2McBsasFQhwOh-yo1QoSamCYjO8',
         page='',
         fields={'character_string1': '订单号', 'amount2': '退款金额'},
         note='退款成功通知；⚠️ 退款方式(const9)绝对不能传值，传了就 47003，不传就不显示'),
    dict(biz='oa_withdraw_ok', channel='oa',
         template_id='Ft5almHmhRkAHpS8bor9QBjxR7N8jsVXLcw8K10fPD4',
         page='',
         fields={'amount1': '金额', 'time2': '时间'},
         note='提现成功通知'),
]

# ============================================================
# 可切换配置项（域名等）
# ============================================================
SEED_CONFIG = [
    ('h5_base', 'https://locker.cqdyxl.com', '域名', 'H5 主域名（贴纸二维码写的就是它，被封只能重贴，所以备用域名要提前备案）'),
    ('h5_backup_domains', '', '域名', '备用域名，逗号分隔（待办：先并备案 + 证书 + 加微信业务域名白名单）'),
    ('oauth_path', '/api/wx/oauth', '域名', '网页授权入口路径'),
    ('pay_notify_url', 'https://locker.cqdyxl.com/api/pay/notify', '回调', '支付回调（注意：微信支付回调地址要在商户平台备案，换域名要同步改）'),
    ('refund_notify_url', 'https://locker.cqdyxl.com/api/refund/notify', '回调', '退款回调'),
    ('complaint_notify_url', 'https://locker.cqdyxl.com/api/admin_v2/wechat-complaint/notify', '回调', '投诉通知回调'),
    ('mp_entry_path', 'pages/subscribe/subscribe', '小程序', 'H5 跳小程序时的落地页'),
    ('mp_home_path', 'pages/index/index', '小程序', '小程序首页'),
    ('mp_mine_path', 'pages/mine/mine', '小程序', '小程序"我的"页（订阅消息点击跳这里）'),
    ('entry_mode', 'h5', '入口', '存包入口走哪条：h5 / mp / auto（auto=能跳小程序就跳，跳不动留 H5）'),
    ('oa_subscribe_enabled', 'false', '入口', '公众号订阅通知总开关（与小程序订阅消息各自独立，不互相替代）'),
]


def seed(reset=False):
    """写种子数据。reset=True 会先清空账号池/模板/配置项（危险，只给本地演示用）"""
    if reset:
        raise SystemExit('[中止] 生产环境不允许 reset=True')

    n_acct = n_tpl = n_cfg = 0
    existing = {(a['acct_type'], a['appid']) for a in C.list_accounts()}
    for a in SEED_ACCOUNTS:
        if (a['acct_type'], a['appid']) in existing:
            continue
        C.create_account(**a)
        n_acct += 1

    tpl_have = {(t['biz'], t['channel'], t['account_id']) for t in C.list_templates()}
    for t in SEED_TEMPLATES:
        key = (t['biz'], t['channel'], 0)
        if key in tpl_have:
            continue
        C.set_template(t['biz'], t['channel'], t['template_id'],
                       page=t.get('page', ''), fields=t.get('fields'),
                       note=t.get('note', ''))
        n_tpl += 1

    cfg_have = set()
    with C._conn() as (conn, kind):
        cfg_have = {r['cfg_key'] for r in C._rows(conn, kind, 'SELECT cfg_key FROM wx_config_items')}
    for key, val, grp, note in SEED_CONFIG:
        if key in cfg_have:
            continue
        C.set_config(key, val, group_name=grp, note=note)
        n_cfg += 1

    C.clear_cache()
    return {'accounts': n_acct, 'templates': n_tpl, 'config': n_cfg}


def ensure_seeded():
    """账号池空就自动灌种子"""
    if not C.list_accounts():
        return seed()
    return {'accounts': 0, 'templates': 0, 'config': 0}


if __name__ == '__main__':
    raise SystemExit('[提示] 这个文件是被 wxcfg_3a_seed.py import 用的。'
                     '要灌数据请执行: python3 /home/ubuntu/wxcfg_tools/wxcfg_3a_seed.py')
