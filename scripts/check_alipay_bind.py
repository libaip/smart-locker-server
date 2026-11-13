#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S912] 支付宝"指定收款"绑定检测（可随时重跑，结果可推微信）

用法：
    cd /home/ubuntu/smart-locker
    python3 scripts/check_alipay_bind.py            # 只打印
    python3 scripts/check_alipay_bind.py --push     # 结果推到微信

测试原理（安全、不扣钱）：
    · 调 alipay.trade.create 创建一笔 0.01 元交易
    · 只创建、不付款 → 15 分钟自动关闭，不产生任何扣款
    · 买家 openid 用库里真实用户的（前端小程序下的）
    · 看返回码判断哪个组合能用
"""
import sys
import os
import time
import logging
import json

logging.disable(logging.CRITICAL)
APP = '/home/ubuntu/smart-locker'
sys.path.insert(0, APP)

from alipay import AlipayClient, PROD_GATEWAY       # noqa: E402
from database import get_db                          # noqa: E402

# 前端小程序（用户在用的那个）
FRONT_MP = '2021006199688688'

# 待测的"收款方应用"：别名 -> (app_id, 私钥文件, 支付宝公钥文件, 说明)
TENANTS = [
    ('A 生产(前端自己的应用)', FRONT_MP, 'alipay_mp_private_key.pem', 'alipay_mp_alipay_public_key.pem'),
    ('B 2021005180693170', '2021005180693170', 'alipay_mp_b_private_key.pem', 'alipay_mp_b_alipay_public_key.pem'),
    ('C 2021007109611312', '2021007109611312', 'alipay_mp_c_private_key.pem', 'alipay_mp_c_alipay_public_key.pem'),
]


def load(app_id, prv, pub):
    try:
        return (open(os.path.join(APP, 'cert', prv)).read(),
                open(os.path.join(APP, 'cert', pub)).read())
    except Exception:
        return None


def main():
    push = '--push' in sys.argv
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT alipay_uid FROM users WHERE COALESCE(alipay_uid,'')<>'' ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    buyer = dict(r)['alipay_uid'] if r else ''
    cur.close(); db.close()

    lines = []
    lines.append('测试时间: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('前端小程序: %s' % FRONT_MP)
    lines.append('买家 openid: %s...' % buyer[:16])
    lines.append('')

    def run(label, app_id, prv, pub, op_app):
        keys = load(app_id, prv, pub)
        if not keys:
            return None, '密钥文件缺失', ''
        priv, alipub = keys
        try:
            c = AlipayClient(app_id=app_id, private_key=priv,
                             alipay_public_key=alipub, gateway=PROD_GATEWAY)
            kw = dict(out_trade_no='CHK%d' % (int(time.time() * 1000) % 10 ** 9),
                      total_amount=0.01, subject='收款绑定检测',
                      buyer_open_id=buyer, product_code='JSAPI_PAY')
            if op_app:
                kw['op_app_id'] = op_app
            r = c.trade_create(**kw)
            code = str(r.get('code') or '')
            sub = str(r.get('sub_code') or '-')
            msg = str(r.get('sub_msg') or r.get('msg') or '')[:60]
            ok = (code == '10000')
            return ok, '%s / %s' % (code, sub), msg
        except Exception as e:
            return False, 'EXC', str(e)[:60]

    results = []
    # ① 方案二标准姿势：各收款方应用签名 + op_app_id = 前端小程序
    lines.append('【① 收款方应用签名 + op_app_id=前端小程序（方案二标准姿势）】')
    for label, app_id, prv, pub in TENANTS:
        ok, code, msg = run(label, app_id, prv, pub, FRONT_MP)
        mark = '✅ 通' if ok else '❌'
        lines.append('   %-26s %s  %s' % (label, mark, code))
        if not ok and msg:
            lines.append('        %s' % msg)
        results.append((label + ' + opapp', ok))

    # ② 对照组：前端小程序自己的应用（= 现在的生产配置，必须 ✅）
    lines.append('')
    lines.append('【② 对照组：现在的生产配置（必须 ✅，否则说明我们这边有问题）】')
    ok, code, msg = run('生产配置', FRONT_MP, 'alipay_mp_private_key.pem',
                        'alipay_mp_alipay_public_key.pem', FRONT_MP)
    lines.append('   %-26s %s  %s' % ('2021006199688688 + 自己', '✅ 通' if ok else '❌', code))
    prod_ok = ok

    lines.append('')
    lines.append('【结论】')
    scheme2_ok = [n for n, o in results if o and not n.startswith('A 生产')]
    if not prod_ok:
        lines.append('   ⚠️ 连生产配置都不通 → 是我们这边的问题，请找技术')
    elif scheme2_ok:
        lines.append('   ★★ 方案二可以用了：%s' % ', '.join(scheme2_ok))
        lines.append('   → 告诉技术，1 分钟切配置')
    else:
        lines.append('   ⚠️ 还没有任何一个收款方应用能收')
        lines.append('   → 去诊断页看这两条 ❌ 变 ✅ 没有：')
        lines.append('     ①应用类型符合 ②主体已开通JSAPI支付产品')

    txt = '\n'.join(lines)
    print('=' * 84)
    print('  支付宝收款绑定检测')
    print('=' * 84)
    print(txt)
    print('=' * 84)

    if push:
        try:
            from helpers import send_pushplus
            title = ('✅ 支付宝收款绑定：已通' if scheme2_ok
                     else '❌ 支付宝收款绑定：还没通')
            send_pushplus(title, txt)
            print('[push] 已推送')
        except Exception as e:
            print('[push] 异常 %s' % str(e)[:80])

    return 0 if scheme2_ok else 1


if __name__ == '__main__':
    sys.exit(main())
