#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S853] 支付宝「指定收款账号(seller_id)」配置检测 —— 随时可跑，不改任何数据。

用法：
    python3 check_alipay_seller.py                # 用默认 PID(B)
    python3 check_alipay_seller.py 2088xxxxxxxxxxxx

原理：alipay.trade.create 只【创建交易】、不扣钱；不唤起支付就自动超时关闭。
      两个对照组：
        · 不带 seller_id -> 应成功（证明渠道本身正常）
        · 带 seller_id   -> 成功=收款关系已配好 / INVALID_RECEIVE_ACCOUNT=还没配
"""
import sys, time, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')
import app as _a                        # noqa
from helpers import get_channel_wxpay
from database import get_db

APPA = '2021006199688688'
PIDB = sys.argv[1] if len(sys.argv) > 1 else '2088980360979607'

db = get_db(); cur = db.cursor()
cur.execute("SELECT * FROM payment_channels WHERE app_id=%s AND channel_type='alipay' LIMIT 1", (APPA,))
row = cur.fetchone()
cur.execute("SELECT alipay_uid FROM users WHERE COALESCE(alipay_uid,'')<>'' ORDER BY id DESC LIMIT 1")
buyer = (dict(cur.fetchone()).get('alipay_uid') if True else '') or ''
cur.close(); db.close()
if not row:
    print('❌ 找不到 app_id=%s 的支付宝渠道' % APPA); sys.exit(2)
ch = dict(row)
client, _ = get_channel_wxpay(ch)

print('小程序 app_id : %s' % ch.get('app_id'))
print('渠道 mch_id   : %s' % (ch.get('mch_id') or '(空)'))
print('待测收款 PID  : %s' % PIDB)
print('买家 openid   : %s' % (buyer[:18] + '...' if buyer else '(无)'))
print('-' * 58)

def probe(label, **kw):
    oid = 'CHKA%s' % str(int(time.time() * 1000))[-9:]
    try:
        r = client.trade_create(out_trade_no=oid, total_amount=0.01, subject='配置检测',
                                buyer_open_id=buyer, product_code='JSAPI_PAY', op_app_id=APPA, **kw)
    except Exception as e:
        print('%-16s -> 异常 %s' % (label, e)); return None
    code = str(r.get('code')); sc = str(r.get('sub_code') or ''); sm = str(r.get('sub_msg') or '')
    if code == '10000':
        print('%-16s -> ✅ 成功' % label); return 'ok'
    print('%-16s -> ❌ %s / %s / %s' % (label, code, sc, sm)); return sc or code

a = probe('无 seller_id')
b = probe('带 seller_id', seller_id=PIDB)
print('-' * 58)
if a == 'ok' and b == 'ok':
    print('结论：✅✅ 收款关系【已配好】')
    print('      下一步：把渠道 mch_id 填成 %s 即可正式生效。' % PIDB)
    print('      SQL: UPDATE payment_channels SET mch_id=\'%s\' WHERE id=%s;' % (PIDB, ch.get('id')))
elif b == 'ACQ.INVALID_RECEIVE_ACCOUNT':
    print('结论：⚠️ 还没配好 —— 「卖家不在设置的收款账户列表之中」')
    print('      去找支付宝客服/小二配置收款关系（seller_id 与 pid 不一致）。')
    print('      ★ 现在【不要】把 mch_id 填上，否则支付宝订单会全部失败。')
else:
    print('结论：结果不明确（对照组成立=%s，带seller_id=%s），把输出发给开发看。' % (a, b))
