#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S232-20260917] routes/user.py: "其它位置"（提现页等）也只请求「退款成功」一个模板

老板实测：一次弹窗放两个模板，用户往往只勾一个 -> 另一个通知永远没额度（今天小程序通道 43101 达 1,270 次）。
拆成两条各自的触点：存包落地页(已上线)只问「账户余额」；提现页/其它只问「退款成功」。
注意：小程序存包页 deposit.js 读的是具名字段(deposit_notify/general_notify/withdraw_notify，仍是 2 条)，
      不受这一行影响 —— 万一真有人在小程序内直接存包，他照样能拿到「账户余额」授权(安全网)。
"""
import io, sys

P = sys.argv[1] if len(sys.argv) > 1 else 'routes/user.py'

OLD = "    _tpls = [_general] if _landing else [_withdraw, _general]\n"

NEW = ("    # [S232-20260917] 其它位置（提现页等）也只要一个：「退款成功」\n"
       "    # 老板实测：一次弹窗两个模板，用户往往只勾一个 -> 另一个永远没额度(今天 43101 达 1270 次)。\n"
       "    # 两条模板各自的触点：存包落地页给「账户余额」、提现页给「退款成功」。\n"
       "    # 注意：小程序存包页(deposit.js)读的是下面三个具名字段(仍是 2 条)，不受这行影响 ——\n"
       "    #       万一真有人在小程序内直接存包，他照样能拿到「账户余额」授权。\n"
       "    _tpls = [_general] if _landing else [_withdraw]\n")


def main():
    s = io.open(P, encoding='utf-8').read()
    n = s.count(OLD)
    if n != 1:
        print('[FAIL] 原文出现 %d 次（要求 1 次），未写文件' % n)
        sys.exit(2)
    s2 = s.replace(OLD, NEW, 1)
    if "_tpls = [_general] if _landing else [_withdraw]" not in s2:
        print('[FAIL] 改后内容不对')
        sys.exit(3)
    if "_tpls = [_general] if _landing else [_withdraw, _general]" in s2:
        print('[FAIL] 旧内容仍在')
        sys.exit(4)
    io.open(P, 'w', encoding='utf-8', newline='').write(s2)
    print('[DONE] user.py: %d -> %d 字符 (%+d)' % (len(s), len(s2), len(s2) - len(s)))


if __name__ == '__main__':
    main()
