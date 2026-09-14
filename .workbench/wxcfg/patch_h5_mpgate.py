# -*- coding: utf-8 -*-
"""[A1 修bug] H5：从"跳转返回"时先问服务端"这个订单到底进没进小程序"。

修的问题（老板真机实测）：微信跳小程序时会弹它自己的确认框，用户点"取消"时页面会
"隐藏一下又回来"，原来的逻辑把"回来"当成跳成功 -> 直接放行到支付页 ✗。
现在：回来时先查 /api/user/mp-entered?order_id=xx
  - entered=true  -> 进支付页（真的进去了）
  - entered=false -> 弹回「请跳转小程序进行下一步」（取消/没跳成功）
  查不到/接口异常 -> 放行（别把真进去的人卡住）；重试一次（小程序上报可能慢半拍）；
  6 秒还没结果也放行（兜底，避免把人卡死）。

用法：python3 patch_h5_mpgate.py <项目根> [--real|--revert]
"""
import io, os, re, shutil, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
BKDIR = os.path.join(ROOT, 'backups')

FUNC_ANCHOR = """    // 读档恢复到支付页；成功返回 true
    function resumeFromMpJump() {"""
FUNC_NEW = """    // ===== [A1-20260914] 回来时先问服务端"到底进没进小程序" =====
    // 背景：微信跳小程序会弹它自己的确认框，用户点"取消"时页面也会"隐藏一下又回来"，
    //       光看"页面可见了"会把取消误判成成功 -> 直接进支付页（老板真机抓到的 bug）。
    // 做法：小程序进去后会 POST /api/user/mp-exit-log（已落库）-> 问 /api/user/mp-entered。
    function mpProceedOrGate() {
        var _done = false;
        function _yes() { if (_done) { return; } _done = true; try { goToStep2Direct(); } catch (e) {} }
        function _no() { if (_done) { return; } _done = true; try { showMpTipModal(); } catch (e) {} }
        function _oid() {
            var oid = currentOrderId || '';
            if (!oid) {
                try { var _o = mpReadPendingJump(15 * 60 * 1000); if (_o) { oid = _o.order_id; } } catch (e) {}
            }
            return oid;
        }
        function _ask(cb) {
            try {
                fetch('/api/user/mp-entered?order_id=' + encodeURIComponent(_oid()))
                  .then(function (r) { return r.json(); })
                  .then(function (d) { cb(!!(d && d.data && d.data.entered)); })
                  .catch(function () { cb(true); });      // 接口异常 -> 放行，别把真进去的人卡住
            } catch (e) { cb(true); }
        }
        _ask(function (ok1) {
            if (ok1) { _yes(); return; }
            setTimeout(function () {                        // 小程序上报可能慢半拍，再问一次
                _ask(function (ok2) { if (ok2) { _yes(); } else { _no(); } });
            }, 1500);
        });
        setTimeout(function () { if (!_done) { _done = true; try { goToStep2Direct(); } catch (e) {} } }, 6000);
    }

    // 读档恢复到支付页；成功返回 true
    function resumeFromMpJump() {"""

A1_OLD = """        mpOpenedTime = 0;
        try { goToStep2Direct(); } catch (e) {}
        return true;
    }"""
A1_NEW = """        mpOpenedTime = 0;
        // [A1] 不直接进支付页：先确认"真的进过小程序"（取消跳转的会被拦回"请跳转小程序"）
        try { mpProceedOrGate(); } catch (e) {}
        return true;
    }"""

A2_OLD = """        if (mpOpenedTime > 0 && Date.now() - mpOpenedTime > 2000) {
            mpOpenedTime = 0;
            try { goToStep2Direct(); } catch (e) {}
            return true;
        }"""
A2_NEW = """        if (mpOpenedTime > 0 && Date.now() - mpOpenedTime > 2000) {
            mpOpenedTime = 0;
            // [A1] 同上：先确认真的进过小程序，否则弹回拦截层
            try { mpProceedOrGate(); } catch (e) {}
            return true;
        }"""

EDITS = [('新增 mpProceedOrGate', FUNC_ANCHOR, FUNC_NEW),
         ('resumeFromMpJump 改判定', A1_OLD, A1_NEW),
         ('mpReturnCheck 改判定', A2_OLD, A2_NEW)]


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'deposit.html'))
         for d in os.listdir(BKDIR) if d.startswith('mpgate_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    shutil.copy2(src, H5)
    print('  已还原自 %s' % src)
    raise SystemExit(0)

t = io.open(H5, encoding='utf-8').read()
ok = True
for label, old, new in EDITS:
    n = t.count(old)
    print('  %s %-26s 命中 %d/1' % ('✓' if n == 1 else '✗', label, n))
    if n != 1:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，不写')
new = t
for label, old, nw in EDITS:
    new = new.replace(old, nw, 1)
blocks = re.findall(r'<script>(.*?)</script>', new, re.S)
io.open('/tmp/_mpgate.js', 'w', encoding='utf-8').write(blocks[-1] if blocks else '')
print('  mpProceedOrGate 出现次数:', new.count('mpProceedOrGate'))

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'mpgate_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
io.open(H5, 'w', encoding='utf-8').write(new)
print('  已写入 static/deposit.html; 备份 %s' % os.path.join(d, 'deposit.html'))
