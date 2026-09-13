# -*- coding: utf-8 -*-
"""
[T-1789258766 / 3A] 在 175 生产库建配置中心 4 张表 + 灌种子数据

安全性：
  - 先断言连的是 PostgreSQL（走 pgbouncer 6432），一旦发现是 sqlite 立刻中止，
    绝不误建到本地 locker.db
  - init_db() 只用 CREATE TABLE IF NOT EXISTS，不改、不删任何已有表
  - seed() 是幂等的：已存在的账号/模板/配置项一律跳过，不会覆盖人工改过的值
  - 灌完打印"配置中心取值 vs config.py 现值"逐项对比，证明取值完全一致

用法: python3 wxcfg_3a_seed.py
"""
import sys
import json

sys.path.insert(0, '/home/ubuntu/smart-locker')
sys.path.insert(0, '/home/ubuntu/wxcfg_tools')

import wx_config as C
from database import get_db

print('=' * 70)
print('[3A] 175 生产库：建表 + 灌种子')
print('=' * 70)

C.bind(get_db)

# ---------- 0) 安全检查：必须走 PostgreSQL ----------
kind = C._conn_kind(get_db())
print('连接类型: %s' % kind)
if kind != 'pg':
    raise SystemExit('[中止] 检测到不是 PostgreSQL（%s）。如果继续会把表建到本地 sqlite，已停止。' % kind)

import config
dsn = getattr(config, 'DATABASE_URL', '') or ''
print('数据库: %s' % (dsn.split('@')[-1] if '@' in dsn else dsn))

# ---------- 1) 建表 ----------
before = C.check_schema()
print('\n建表前:')
for t, v in before['tables'].items():
    print('   %-18s 存在=%s' % (t, v['exists']))
if before['warnings']:
    print('   提示: %s' % '; '.join(before['warnings']))

C.init_db()
after = C.check_schema()
print('\n建表后:')
for t, v in after['tables'].items():
    print('   %-18s 存在=%s  自增主键=%s' % (t, v['exists'], v['auto_id']))
print('   整体 ok=%s' % after['ok'])
if not after['ok']:
    raise SystemExit('[中止] 建表后校验没通过: %s' % after['warnings'])

# ---------- 2) 灌种子 ----------
import seed_prod

r = seed_prod.seed()
print('\n本次写入: 账号 %d 条 / 模板 %d 条 / 配置项 %d 条（已存在的会跳过）' % (r['accounts'], r['templates'], r['config']))

accts = C.list_accounts()
tpls = C.list_templates()
cfgs = C.list_config_items()
print('库里现有: 账号 %d 条 / 模板 %d 条 / 配置项 %d 条' % (len(accts), len(tpls), len(cfgs)))

print('\n账号池:')
for a in accts:
    print('   [%s] %-28s %-24s 生效=%s 优先级=%s' % (a['acct_type'], a['name'], a['appid'], a['is_active'], a['priority']))

print('\n模板:')
for t in tpls:
    print('   [%s] %-20s %s' % (t['channel'], t['biz'], t['template_id']))

print('\n配置项:')
for c in cfgs:
    print('   %-22s = %s' % (c['cfg_key'], c['cfg_value']))

# ---------- 3) 取值一致性：配置中心 vs config.py 现值 ----------
print('\n' + '=' * 70)
print('取值一致性核对（配置中心读出来的 vs config.py 里写死的）')
print('=' * 70)


def g(name):
    return getattr(config, name, None)


pairs = [
    ('小程序 appid', C.mp_appid(), g('WX_MP_APP_ID')),
    ('小程序 secret', C.mp_secret(), g('WX_MP_APP_SECRET')),
    ('公众号 appid', C.oa_appid(), g('WX_APP_ID')),
    ('公众号 secret', C.oa_secret(), g('WX_APP_SECRET')),
    ('支付回调', C.pay_notify_url(), g('WX_PAY_NOTIFY_URL')),
    ('退款回调', C.refund_notify_url(), g('WX_REFUND_NOTIFY_URL')),
    ('H5 主域名', C.h5_base(), C.DEFAULTS['config']['h5_base']),
    ('H5 下单页', C.h5_store(), C.DEFAULTS['config']['h5_base'] + '/store'),
    ('网页授权回调', C.oauth_callback(), C.DEFAULTS['config']['h5_base'] + C.DEFAULTS['config']['oauth_path']),
    ('WS 地址', C.ws_base(), 'ws://locker.cqdyxl.com'),
]
ok = bad = 0
for name, got, want in pairs:
    if want is None:
        print('   %-14s %s   （config.py 里没有对应常量，跳过比对）' % (name, C.mask(got, 40)))
        continue
    flag = 'PASS' if got == want else '❌ 不一致'
    if got == want:
        ok += 1
    else:
        bad += 1
    print('   %-14s %-6s 配置中心=%-46s config.py=%s' % (name, flag, C.mask(got, 46), C.mask(want, 46)))
print('\n一致 %d 项，不一致 %d 项' % (ok, bad))

# ---------- 4) 三个业务码的模板ID ----------
print('\n模板ID取值（第1步会读这三个）:')
for biz in ('subscribe_deposit', 'subscribe_refund', 'subscribe_general'):
    tid = C.template_id(biz)
    print('   %-20s %s' % (biz, tid))

print('\n' + '=' * 70)
print('3A 建表灌种子完成')
print('=' * 70)
