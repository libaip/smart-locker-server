#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S229-20260917] static/deposit.html: 轮询不再被"返回信号"掐死 + 问服务端从 2 次加到 4 次

背景：S228 把"2 秒猜跳失败"删了、改成每 2 秒问服务端，但实测（当天 13:45-14:00）iPhone 仍 5/8 多跳一次：
  resumeFromMpJump() 会把 mpOpenedTime 清零 -> 新轮询的 `mpOpenedTime === 0 就自杀` 那条立刻命中被掐死，
  只剩 mpProceedOrGate 里"问两次、间隔 1.5 秒"，而 iPhone 上小程序上报有时晚一两秒才落库 -> 两次都问空 -> 误拦。
  铁证：老板测试号 18888889999 的订单 131116 跳了 2 次，且埋点报 sec=3（说明第一次的轮询已被掐死、第二次是新起的）。

改法两处：
  1) 轮询去掉 `mpOpenedTime === 0 就停`（改为只在 已放行/已到支付页/新单/3 分钟超时 时停）；
  2) mpProceedOrGate 的询问从 2 次加到 4 次（间隔 1.5 秒，约 4.5 秒），兜底放行从 6 秒挪到 9 秒。
"""
import io
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'static/deposit.html'

OLD1 = """            if (_mpEscaped) { clearInterval(mpEnterPoll); mpEnterPoll = null; return; }
            if (mpOpenedTime === 0) { clearInterval(mpEnterPoll); mpEnterPoll = null; return; }
            var el = Date.now() - t0;
"""
NEW1 = """            if (_mpEscaped) { clearInterval(mpEnterPoll); mpEnterPoll = null; return; }
            // [S229] 这里原来还有一句 `if (mpOpenedTime === 0) 停表`：用户带回 H5 时
            // resumeFromMpJump() 会把 mpOpenedTime 清零，轮询当场被掐死 —— iPhone 误拦就是这么来的。
            // 现在只认"已放行/已到支付页/新单/3 分钟超时"这几种停法(都在别处显式 clear)。
            var el = Date.now() - t0;
"""

OLD2 = """        _ask(function (ok1) {
            if (ok1) { _yes(); return; }
            setTimeout(function () {                        // 小程序上报可能慢半拍，再问一次
                _ask(function (ok2) { if (ok2) { _yes(); } else { _no(); } });
            }, 1500);
        });
        setTimeout(function () { if (!_done) { _done = true; try { goToStep2Direct(); } catch (e) {} } }, 6000);
"""
NEW2 = """        // [S229] 原来只问 2 次(间隔 1.5 秒)：iPhone 上小程序上报晚一两秒就两次全空 -> 误拦回设置页。
        //        改成最多问 4 次(约 4.5 秒)，兜底放行从 6 秒挪到 9 秒，把这一两秒的时差吃掉。
        var _tries = 0;
        function _askLoop() {
            if (_done) { return; }
            _tries++;
            _ask(function (ok) {
                if (_done) { return; }
                if (ok) { _yes(); return; }
                if (_tries >= 4) { _no(); return; }
                setTimeout(_askLoop, 1500);
            });
        }
        _askLoop();
        setTimeout(function () { if (!_done) { _done = true; try { goToStep2Direct(); } catch (e) {} } }, 9000);
"""


def main():
    s = io.open(P, encoding='utf-8').read()
    before = len(s)
    for tag, old, new in [('1) 轮询不再自杀', OLD1, NEW1), ('2) 询问 2 次->4 次', OLD2, NEW2)]:
        n = s.count(old)
        if n != 1:
            print('[FAIL] %s: 原文出现 %d 次（要求 1 次），未写文件' % (tag, n))
            sys.exit(2)
        s = s.replace(old, new, 1)
        print('[ok] %s' % tag)
    if s.count('_askLoop') != 3:            # 定义 1 + 调用 1 + 递归 1
        print('[FAIL] _askLoop 出现 %d 次(期望 3)' % s.count('_askLoop'))
        sys.exit(3)
    if 'if (mpOpenedTime === 0) { clearInterval(mpEnterPoll)' in s:
        print('[FAIL] 轮询自杀那行仍在')
        sys.exit(4)
    io.open(P, 'w', encoding='utf-8', newline='').write(s)
    print('[DONE] 已写入 %s：%d -> %d 字符 (+%d)' % (P, before, len(s), len(s) - before))


if __name__ == '__main__':
    main()
