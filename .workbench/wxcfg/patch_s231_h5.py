#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S231-20260917] static/deposit.html: 跳小程序前用 sendBeacon 给服务端打个招呼（存包落地页标记）"""
import io, sys

P = sys.argv[1] if len(sys.argv) > 1 else 'static/deposit.html'

OLD_FN = "    function launchMp() {\n"

NEW_FN = '''    // ===== [S231-20260917] 跳小程序前"打招呼"：让服务端知道接下来几秒的模板请求来自存包落地页 =====
    // 用途：存包落地页只请求"账户余额"一个订阅模板；提现页/小程序存包页仍请求两个。
    // 为什么用 sendBeacon：它由浏览器在页面卸载时也保证发出，跳走不会丢。
    // 失败无所谓：服务端收不到就当"非落地页"处理（退回两个模板，与改动前一致）。
    function mpJumpIntent() {
        try {
            if (!navigator.sendBeacon) { return; }
            var _body = JSON.stringify({ order_id: currentOrderId || '', phone: currentPhone || '' });
            navigator.sendBeacon('/api/user/mp-jump-intent', new Blob([_body], { type: 'application/json' }));
        } catch (e) {}
    }

'''

OLD_CALL1 = """        try { mpSavePendingJump(); } catch (e) {}   // [A1] 点"去小程序"也存档：回来/页面被重载都能接上订单
"""
NEW_CALL1 = OLD_CALL1 + """        try { mpJumpIntent(); } catch (e) {}        // [S231] 打招呼：这一次是"存包落地页"
"""

OLD_CALL2 = """            mpStartEnterPoll();   // [S228] 不再用"2 秒还可见"猜跳失败(iPhone 必误判)，改成盯服务端
            window.location.href = mpScheme;
"""
NEW_CALL2 = """            mpStartEnterPoll();   // [S228] 不再用"2 秒还可见"猜跳失败(iPhone 必误判)，改成盯服务端
            try { mpJumpIntent(); } catch (e) {}       // [S231] 打招呼：这一次是"存包落地页"
            window.location.href = mpScheme;
"""


def main():
    s = io.open(P, encoding='utf-8').read()
    before = len(s)
    for tag, old, new in [('1) 插入 mpJumpIntent()', OLD_FN, NEW_FN + OLD_FN),
                          ('2) launchMp 里调用', OLD_CALL1, NEW_CALL1),
                          ('3) mpAutoJump 里调用', OLD_CALL2, NEW_CALL2)]:
        n = s.count(old)
        if n != 1:
            print('[FAIL] %s: 原文出现 %d 次（要求 1 次），未写文件' % (tag, n))
            sys.exit(2)
        s = s.replace(old, new, 1)
        print('[ok] %s' % tag)
    if s.count('mpJumpIntent') != 3:
        print('[FAIL] mpJumpIntent 出现 %d 次（期望 3 = 定义1 + 调用2）' % s.count('mpJumpIntent'))
        sys.exit(3)
    if '/api/user/mp-jump-intent' not in s:
        print('[FAIL] 缺少接口路径')
        sys.exit(4)
    io.open(P, 'w', encoding='utf-8', newline='').write(s)
    print('[DONE] deposit.html: %d -> %d 字符 (%+d)' % (before, len(s), len(s) - before))


if __name__ == '__main__':
    main()
