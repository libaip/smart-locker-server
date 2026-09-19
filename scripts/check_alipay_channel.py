#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S257] 支付宝通道一键自检（只读，不改配置、不产生真实交易）
用法（服务器上）：python3 scripts/check_alipay_channel.py [通道id]
  不带参数 = 检查所有 channel_type='alipay' 的通道
判定：
  ACQ.TRADE_NOT_EXIST   → ✅ 通道可用（签名/网关/appid 全对，应用已上线）
  isv.not-online-app    → ⏳ 配置对，但应用还在审核中（上线后再测）
  isv.invalid-signature → ❌ 应用公钥与本地私钥不配对（去控制台换成配对的公钥）
  其他                   → 见微信/支付宝返回原文
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from database import get_db          # noqa: E402
from helpers import get_channel_wxpay  # noqa: E402


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    conn = get_db()
    cur = conn.cursor()
    if only:
        cur.execute("SELECT * FROM payment_channels WHERE id=%s", (only,))
    else:
        cur.execute("SELECT * FROM payment_channels WHERE channel_type='alipay' ORDER BY id")
    rows = cur.fetchall() or []
    if not rows:
        print('没有 channel_type=alipay 的通道（先在后台新增一条支付宝通道）')
        return
    print('%-5s %-20s %-18s %-6s %s' % ('id', 'name', 'app_id', '启用', '自检结果'))
    print('-' * 100)
    for ch in rows:
        ch = dict(ch)
        client, ch_type = get_channel_wxpay(ch)
        if client is None or ch_type != 'alipay':
            print('%-5s %-20s %-18s %-6s ❌ 实例创建失败（检查 cert/<cert_name>_private_key.pem 是否存在）'
                  % (ch.get('id'), ch.get('name'), ch.get('app_id'), ch.get('is_active')))
            continue
        out = 'CHECK%d' % (int(time.time()) % 10 ** 9)
        try:
            r = client.query(out_trade_no=out)
            code, sub = str(r.get('code')), str(r.get('sub_code'))
            if sub == 'ACQ.TRADE_NOT_EXIST' or code == '10000':
                msg = '✅ 可用（签名/网关/APPID 全对）'
            elif sub == 'isv.not-online-app':
                msg = '⏳ 配置正确，但应用仍在审核中'
            elif 'sign' in sub.lower():
                msg = '❌ 签名不匹配：控制台"应用公钥"与本地私钥不是一对'
            else:
                msg = '⚠️ code=%s sub_code=%s sub_msg=%s' % (code, sub, r.get('sub_msg'))
            # 顺带验证"下单"能否生成跳转
            try:
                pay = client.wap_pay(out_trade_no=out, total_amount=0.01, subject='通道自检')
                has = bool(pay.get('url')) and bool(pay.get('form'))
                msg += ' | 下单跳转=%s' % ('OK' if has else 'FAIL')
            except Exception as e:
                msg += ' | 下单异常=%s' % e
        except Exception as e:
            msg = '❌ 调用异常: %s' % e
        print('%-5s %-20s %-18s %-6s %s' % (ch.get('id'), ch.get('name'), ch.get('app_id'), ch.get('is_active'), msg))
    conn.close()


if __name__ == '__main__':
    main()
