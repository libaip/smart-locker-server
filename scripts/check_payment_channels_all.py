#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S250] 全通道自检（含老通道）：看"我们自己主体的商户号"还有哪些能用
只读：对每条通道调一次微信统一下单(JSAPI，1分未支付单，不扣款)。
"""
import sys, os, time
os.chdir('/home/ubuntu/smart-locker')
sys.path.insert(0, '/home/ubuntu/smart-locker')
from wxpay import WxPay
from database import get_db

conn = get_db(); cur = conn.cursor()
cur.execute("""SELECT id,name,mch_id,COALESCE(api_key,'') k,COALESCE(app_id,'') aid,is_active
               FROM payment_channels WHERE COALESCE(mch_id,'')<>'' ORDER BY id""")
chs = cur.fetchall()
cur.execute("SELECT openid FROM orders WHERE openid LIKE 'ov47M3%' ORDER BY id DESC LIMIT 1")
r = cur.fetchone(); new_oa = (r['openid'] if isinstance(r, dict) else r[0]) if r else None
cur.execute("SELECT openid FROM orders WHERE openid LIKE 'oLhbm2%' ORDER BY id DESC LIMIT 1")
r2 = cur.fetchone(); old_oa = (r2['openid'] if isinstance(r2, dict) else r2[0]) if r2 else None
conn.close()

print('%-4s %-14s %-20s %-4s %s' % ('id', 'mch_id', 'app_id', '启用', '微信返回'))
print('-' * 104)
ok = []
for c in chs:
    aid = c['aid'] or '(空)'
    openid = old_oa if 'wxd85204' in aid else new_oa
    if not openid or not c['k']:
        print('%-4s %-14s %-20s %-4s 跳过(无openid/无密钥)' % (c['id'], c['mch_id'], aid[-18:], c['is_active']))
        continue
    try:
        wp = WxPay(c['mch_id'], c['k'], aid if aid != '(空)' else new_oa)
        out = 'S250CHK%d' % int(time.time() * 1000 % 10 ** 9)
        rr = wp.unifiedorder(trade_type='JSAPI', body='使用储物柜预付款', total_fee=1, out_trade_no=out,
                             notify_url='https://locker.cqdyxl.com/api/pay/notify', openid=openid)
        if rr.get('return_code') == 'SUCCESS' and rr.get('prepay_id'):
            msg = 'YES 可用'; ok.append((c['id'], c['mch_id'], c['name']))
        else:
            msg = 'NO  %s %s' % (rr.get('err_code') or rr.get('return_code'), (rr.get('err_code_des') or rr.get('return_msg') or '')[:60])
    except Exception as e:
        msg = 'ERR %s' % str(e)[:60]
    print('%-4s %-14s %-20s %-4s %s' % (c['id'], c['mch_id'], aid[-18:], c['is_active'], msg))
    time.sleep(1.0)
print()
print('可用通道：', ok if ok else '（无）')
