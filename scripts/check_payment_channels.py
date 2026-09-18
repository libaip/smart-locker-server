#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S246] 全通道自检（只读，不改任何生产配置）
对每条通道用它的 mch_id + api_key + app_id 调一次微信统一下单(JSAPI)，把微信的原始报错打出来。
作用：一眼看出每个商户号缺什么 —— 密钥错 / 没绑当前公众号 / 产品没开通 / 可用。
注意：只生成 1 分钱的未支付预支付单，不扣款、不影响用户，2 小时自动失效。
"""
import sys, os, time
os.chdir('/home/ubuntu/smart-locker')
sys.path.insert(0, '/home/ubuntu/smart-locker')
from wxpay import WxPay
from database import get_db

OLD = 'wxd85204d0ec930d46'
conn = get_db(); cur = conn.cursor()
cur.execute("""SELECT id,name,mch_id,COALESCE(api_key,'') k,COALESCE(app_id,'') aid,COALESCE(cert_name,'') cert,is_active
               FROM payment_channels WHERE id>=100 ORDER BY id""")
chs = cur.fetchall()
# 取两种 openid 各一个（老号/新号）
def pick(prefix):
    cur.execute("SELECT openid FROM orders WHERE openid LIKE %s ORDER BY id DESC LIMIT 1", (prefix + '%',))
    r = cur.fetchone()
    return (r['openid'] if isinstance(r, dict) else r[0]) if r else None
oa_new = pick('ov47M3')
oa_old = pick('oLhbm2')
conn.close()
print('新公众号openid:', (oa_new or '无')[:10] + '...   老公众号openid:', (oa_old or '无')[:10] + '...')
print()
print('%-4s %-14s %-20s %-4s %s' % ('id', 'mch_id', 'app_id', '启用', '微信返回'))
print('-' * 110)
for c in chs:
    aid = c['aid'] or '(空→取当前公众号)'
    openid = oa_old if aid == OLD else oa_new
    if not openid:
        print('%-4s %-14s %-20s %-4s 没有对应 openid，跳过' % (c['id'], c['mch_id'], aid[-18:], c['is_active']))
        continue
    try:
        wp = WxPay(c['mch_id'], c['k'], aid or oa_new)
        out = 'S246CHK%d' % int(time.time() * 1000 % 10 ** 9)
        r = wp.unifiedorder(trade_type='JSAPI', body='通道自检-不支付', total_fee=1, out_trade_no=out,
                            notify_url='https://locker.cqdyxl.com/api/pay/notify', openid=openid)
        if r.get('return_code') == 'SUCCESS' and r.get('prepay_id'):
            msg = '✅ 可用 (prepay_id=%s...)' % str(r.get('prepay_id'))[:14]
        else:
            msg = '❌ %s | %s' % (r.get('err_code') or r.get('return_code'), r.get('err_code_des') or r.get('return_msg'))
    except Exception as e:
        msg = 'ERR %s' % e
    print('%-4s %-14s %-20s %-4s %s' % (c['id'], c['mch_id'], aid[-18:], c['is_active'], msg))
    time.sleep(1.2)
