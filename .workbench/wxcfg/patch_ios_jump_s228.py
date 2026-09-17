#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S228-20260917] 修 iPhone 扫码存包跳小程序被误判"跳失败"、被弹回设置页 + 挂"请跳转小程序"提示。

背景（2026-09-17 当日 1300 单实测）：
  iPhone 上 H5 跳走小程序后自己仍报"前台可见"，而"订单创建 → 小程序点返回"实测中位数 11 秒
  （没有一单能在 2 秒内回来），于是原有那句 "2 秒还可见 = 跳失败" 对 iPhone 几乎必然误报，
  把正在小程序里订阅的用户弹回设置页；当天 iPhone 86.5% 被迫多跳一次，安卓仅 8%。

改法（只动 H5，不碰小程序/后端/数据库）：
  1) mpAutoJump / launchMp：删掉"2 秒还可见就判失败"的兜底；
  2) 新增 mpStartEnterPoll()：跳出去后每 2 秒问一次 /api/user/mp-entered，
     服务端说"进过小程序"就直接进支付页；60 秒仍无进店记录才兜底弹一次提示；3 分钟自动停；
  3) goToStep2Direct / reserveDoorAndProceed：停掉轮询（到支付页、或开始新订单）。

安全：每处都是"原文精确匹配 + 断言只出现一次"，任一处对不上就报错退出、不写文件。
用法：cd /home/ubuntu/smart-locker && python3 .workbench/wxcfg/patch_ios_jump_s228.py
"""
import io
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'static/deposit.html'

OLD_A = """            mpOpenedTime = Date.now();
            mpStartReturnWatch();
            window.location.href = mpScheme;
            if (mpRescueTimer) { clearTimeout(mpRescueTimer); }
            mpRescueTimer = setTimeout(function() {
                mpRescueTimer = null;
                if (mpOpenedTime === 0) { return; }
                if (document.hidden === false) { showMpTipModal(); }
            }, 2000);
"""
NEW_A = """            mpOpenedTime = Date.now();
            mpStartReturnWatch();
            mpStartEnterPoll();   // [S228] 不再用"2 秒还可见"猜跳失败(iPhone 必误判)，改成盯服务端
            window.location.href = mpScheme;
"""

OLD_B = """        mpOpenedTime = Date.now();
        mpStartReturnWatch();
        if (mpRescueTimer) { clearTimeout(mpRescueTimer); }
        mpRescueTimer = setTimeout(function() {
            mpRescueTimer = null;
            if (mpOpenedTime === 0) { return; }
            if (document.hidden === false) { mpGateOrEscape(); }   // [A1-b] 又没跳过去 -> 点够了就放行，否则弹层再出来
        }, 2000);
"""
NEW_B = """        mpOpenedTime = Date.now();
        mpStartReturnWatch();
        mpStartEnterPoll();   // [S228] 同上：盯着服务端，不再靠"2 秒还可见"猜
"""

NEW_FUNCS = """    // ===== [S228-20260917] iPhone 跳转加固：不再用"2 秒还可见 = 跳失败"猜，改成盯服务端 =====
    // 背景(2026-09-17 实测当日 1300 单)：iPhone 上 H5 跳走小程序后自己仍报"前台可见"，
    //   而"订单创建 → 小程序点返回"实测中位数 11 秒(没有一单能在 2 秒内回来)，
    //   于是那句 2 秒判断对 iPhone 订单几乎必然误报"跳失败" -> 把正在小程序里订阅的用户
    //   弹回设置页并挂上"请跳转小程序"提示（当天 iPhone 86.5% 被迫多跳一次，安卓仅 8%）。
    // 现在：跳出去后每 2 秒问一次 /api/user/mp-entered，服务端说"进过"就直接进支付页；
    //   仍然只有"人回到 H5、服务端说没进过"才拦（取消跳转的照旧拦住，A1 规矩不变）；
    //   60 秒还没进过也没返回信号，才兜底弹一次提示；3 分钟自动停。
    var mpEnterPoll = null;
    function mpStartEnterPoll() {
        if (mpEnterPoll) { clearInterval(mpEnterPoll); mpEnterPoll = null; }
        var t0 = Date.now();
        var gated = 0;
        mpEnterPoll = setInterval(function () {
            if (_mpEscaped) { clearInterval(mpEnterPoll); mpEnterPoll = null; return; }
            if (mpOpenedTime === 0) { clearInterval(mpEnterPoll); mpEnterPoll = null; return; }
            var el = Date.now() - t0;
            if (el > 180000) { clearInterval(mpEnterPoll); mpEnterPoll = null; return; }
            var oid = currentOrderId || '';
            if (!oid) { try { var _p = mpReadPendingJump(15 * 60 * 1000); if (_p) { oid = _p.order_id; } } catch (e) {} }
            if (!oid) { return; }
            fetch('/api/user/mp-entered?order_id=' + encodeURIComponent(oid))
              .then(function (r) { return r.json(); })
              .then(function (d) {
                  if (d && d.data && d.data.entered) {
                      if (mpEnterPoll) { clearInterval(mpEnterPoll); mpEnterPoll = null; }
                      try { mpEnterPollReport('mp_enter_poll_ok', Math.round(el / 1000), 0); } catch (e) {}
                      try { mpTryClear(); } catch (e) {}
                      mpOpenedTime = 0;
                      try { goToStep2Direct(); } catch (e) {}
                      return;
                  }
                  // 60 秒还没"进过"、也没有"回到 H5"的信号 -> 兜底弹一次提示
                  // (人还在小程序里也不影响：一旦有了进店记录，上面那条照样放行)
                  if (el > 60000 && !gated) {
                      gated = 1;
                      try { mpEnterPollReport('mp_failsafe_gate', 0, 60); } catch (e) {}
                      try { mpGateOrEscape(); } catch (e) {}
                  }
              })
              .catch(function () {});
        }, 2000);
    }
    function mpEnterPollReport(stage, sec, limit) {
        try {
            fetch('/api/user/oa-subscribe-log', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    phone: (currentPhone || (document.getElementById('userPhone') || {}).value || ''),
                    detail: JSON.stringify({ stage: stage, sec: sec, tries: mpTryCount(), limit: limit })
                })
            }).catch(function () {});
        } catch (e) {}
    }

"""

OLD_D = "    // 读档恢复到支付页；成功返回 true\n"

OLD_F = "        try { var _gt = document.getElementById('mpTipModal'); if (_gt) { _gt.classList.remove('show'); } } catch (e) {}\n"
NEW_F = OLD_F + "        try { if (mpEnterPoll) { clearInterval(mpEnterPoll); mpEnterPoll = null; } } catch (e) {}   // [S228] 到支付页就停轮询\n"

OLD_G = "        mpClearPendingJump();             // FIX-20260912b 新下单，先清掉上一次的存档\n"
NEW_G = OLD_G + "        try { if (mpEnterPoll) { clearInterval(mpEnterPoll); mpEnterPoll = null; } } catch (e) {}   // [S228] 新单开始，旧轮询作废\n"


def apply_one(s, tag, old, new):
    n = s.count(old)
    if n != 1:
        print('[FAIL] %s: 原文出现 %d 次（要求恰好 1 次），未写文件' % (tag, n))
        sys.exit(2)
    print('[ok] %s' % tag)
    return s.replace(old, new, 1)


def main():
    s = io.open(P, encoding='utf-8').read()
    before = len(s)

    s = apply_one(s, '1) mpAutoJump 去掉 2 秒误判', OLD_A, NEW_A)
    s = apply_one(s, '2) launchMp 去掉 2 秒误判', OLD_B, NEW_B)
    s = apply_one(s, '3) 插入 mpStartEnterPoll/mpEnterPollReport', OLD_D, NEW_FUNCS + OLD_D)
    s = apply_one(s, '4) goToStep2Direct 停轮询', OLD_F, NEW_F)
    s = apply_one(s, '5) reserveDoorAndProceed 停轮询', OLD_G, NEW_G)

    # 收尾自检
    left = s.count('document.hidden === false')
    if left != 0:
        print('[FAIL] 仍有 %d 处 "document.hidden === false" 残留，未写文件' % left)
        sys.exit(3)
    for must in ['function mpStartEnterPoll()', 'mpStartEnterPoll();', 'mpEnterPollReport(', "'mp_enter_poll_ok'", "'mp_failsafe_gate'"]:
        if must not in s:
            print('[FAIL] 缺少标记: %s，未写文件' % must)
            sys.exit(4)
    if s.count('mpStartEnterPoll();') != 2:
        print('[FAIL] mpStartEnterPoll() 调用点应为 2 处，实际 %d' % s.count('mpStartEnterPoll();'))
        sys.exit(5)

    io.open(P, 'w', encoding='utf-8', newline='').write(s)
    print('[DONE] 已写入 %s：%d -> %d 字符 (+%d)' % (P, before, len(s), len(s) - before))


if __name__ == '__main__':
    main()
