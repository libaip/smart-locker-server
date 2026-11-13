#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S906] 自动盯：支付宝"指定收款账号"什么时候配好

每小时跑一次 alipay.trade.create（不扣钱、不下单、自动超时）
只在【状态发生变化】时推微信，避免打扰。
"""
import sys
import os
import json
import time
import logging

logging.disable(logging.CRITICAL)
APP = '/home/ubuntu/smart-locker'
sys.path.insert(0, APP)

from helpers import get_channel_wxpay, send_pushplus   # noqa: E402
from database import get_db                            # noqa: E402

APPA = '2021006199688688'
PIDB = '2088980360979607'
STATE = os.path.join(APP, 'logs', 'alipay_seller_state.json')


def load_state():
    try:
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(d, f)
    except Exception:
        pass


def main():
    push = '--push' in sys.argv
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT * FROM payment_channels WHERE app_id=%s AND channel_type='alipay' LIMIT 1", (APPA,))
    row = cur.fetchone()
    if not row:
        print('找不到渠道'); return 1
    row = dict(row)
    cur.execute("SELECT alipay_uid FROM users WHERE COALESCE(alipay_uid,'')<>'' ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    buyer = dict(r)['alipay_uid'] if r else ''
    cur.close(); db.close()
    if not buyer:
        print('没有可用买家 openid'); return 1

    client, _ = get_channel_wxpay(row)
    oid = 'WTCH%s' % str(int(time.time() * 1000))[-9:]
    try:
        resp = client.trade_create(out_trade_no=oid, total_amount=0.01, subject='收款关系自动检测',
                                   buyer_open_id=buyer, product_code='JSAPI_PAY',
                                   op_app_id=APPA, seller_id=PIDB)
        code = str(resp.get('code') or '')
        sub = str(resp.get('sub_code') or '')
        submsg = str(resp.get('sub_msg') or '')
        ok = (code == '10000')
    except Exception as e:
        code, sub, submsg, ok = 'EXC', '', str(e)[:80], False

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    st = load_state()
    prev = st.get('ok')
    st['ok'] = ok
    st['last_check'] = now
    st['last_code'] = '%s/%s/%s' % (code, sub, submsg)
    save_state(st)

    print('[%s] 指定收款账号 %s -> %s (%s/%s/%s)' % (
        now, PIDB, '✅ 通了' if ok else '❌ 还没通', code, sub, submsg))

    if push and prev is not None and prev != ok:
        if ok:
            title = '✅ 支付宝指定收款已生效'
            body = ('小程序 %s\n指定收款 PID %s\n现在【可以】在渠道113 的 mch_id 里填上这个 PID 了。\n'
                    '填完告诉我，我帮你验证。' % (APPA, PIDB))
        else:
            title = '❌ 支付宝指定收款又失效了'
            body = ('小程序 %s\n指定收款 PID %s\n检测变成失败：%s/%s/%s'
                    % (APPA, PIDB, code, sub, submsg))
        try:
            send_pushplus(title, body)
            print('[push] 已推送')
        except Exception as e:
            print('[push] 异常 %s' % str(e)[:80])
    elif push and prev is None and ok:
        try:
            send_pushplus('✅ 支付宝指定收款已生效',
                          '小程序 %s\n指定收款 PID %s\n已可配置。' % (APPA, PIDB))
            print('[push] 已推送（首次即成功）')
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
