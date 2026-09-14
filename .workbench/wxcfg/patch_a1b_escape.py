# -*- coding: utf-8 -*-
"""[A1-b] 点够次数还跳不进小程序 -> 自动放行进网页支付（防止用户被卡死）。

老板要求：加一个设置，用户点几次过后还是不进入小程序，就直接跳转 H5 支付页面，这样不会卡死。

设计：
  - 计数按"这一次存包流程"算：localStorage key = locker_mp_try_<order_id>（换单/成功了就清掉）
  - 每点一次【去小程序】+1；达到上限（后台设置 mp_jump_max_retry，默认 3）后：
      · 若这次又没跳过去（2 秒后页面还在 / 或者"回来时查不到真进过小程序"）-> 自动进网页支付页，并提示一句
      · 弹层上从第 2 次起显示一行「一直跳不过去？点这里用网页支付」，给用户自己选（不用干等到上限）
  - 每次"逃逸"都上报一条（stage=mp_gate_escape），明天能数出有多少人真的跳不过去
  - 真进去了 / 正常走到支付成功 -> 计数清零

改动：app.py（注入后台设置）+ static/deposit.html（计数与放行逻辑）
用法：python3 patch_a1b_escape.py <项目根> [--real|--revert]
"""
import io, os, re, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
APP = os.path.join(ROOT, 'app.py')
BKDIR = os.path.join(ROOT, 'backups')

# ---------- app.py：注入后台设置 ----------
APP_OLD = """        html = html.replace("{oa_sub_on}", ("true" if _oa_sub_on else "false"))"""
APP_NEW = """        html = html.replace("{oa_sub_on}", ("true" if _oa_sub_on else "false"))
        # [A1-b 2026-09-14] 跳小程序最多点几次（点够了还进不去就放行网页支付），后台设置 mp_jump_max_retry
        try:
            _mp_try_limit = int(str(_get_setting('mp_jump_max_retry', '3') or '3').strip() or '3')
        except Exception:
            _mp_try_limit = 3
        if _mp_try_limit < 1:
            _mp_try_limit = 1
        if _mp_try_limit > 10:
            _mp_try_limit = 10
        html = html.replace("{mp_retry_limit}", str(_mp_try_limit))"""

# ---------- H5：变量 ----------
VAR_OLD = """    var _oaTestOnly = params.get('oatest') === '1';   // [测试] 只弹订阅层, 不创建订单"""
VAR_NEW = """    var _oaTestOnly = params.get('oatest') === '1';   // [测试] 只弹订阅层, 不创建订单
    // [A1-b] 跳小程序最多点几次（后台设置 mp_jump_max_retry；没注入时按 3）
    var MP_RETRY_LIMIT = parseInt("{mp_retry_limit}", 10) || 3;"""

# ---------- H5：计数与放行函数（插在 mpProceedOrGate 前面） ----------
FN_ANCHOR = """    // ===== [A1-20260914] 回来时先问服务端"到底进没进小程序" ====="""
FN_NEW = """    // ===== [A1-b 2026-09-14] 点够次数还跳不过去 -> 放行进网页支付（防卡死） =====
    function mpTryKey() {
        var oid = currentOrderId || '';
        if (!oid) {
            try { var _o = mpReadPendingJump(15 * 60 * 1000); if (_o) { oid = _o.order_id; } } catch (e) {}
        }
        return 'locker_mp_try_' + (oid || 'x');
    }
    function mpTryCount() { try { return parseInt(localStorage.getItem(mpTryKey()) || '0', 10) || 0; } catch (e) { return 0; } }
    function mpTryInc() { try { var n = mpTryCount() + 1; localStorage.setItem(mpTryKey(), String(n)); return n; } catch (e) { return 1; } }
    function mpTryClear() { try { localStorage.removeItem(mpTryKey()); } catch (e) {} }
    function mpGateEscapeReport(tries) {
        try {
            fetch('/api/user/oa-subscribe-log', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    phone: (currentPhone || (document.getElementById('userPhone') || {}).value || ''),
                    detail: JSON.stringify({stage: 'mp_gate_escape', tries: tries})
                })
            }).catch(function () {});
        } catch (e) {}
    }
    function mpGateEscape() {
        var _n = mpTryCount();
        mpTryClear();
        mpGateEscapeReport(_n);
        try { showToast('多次未能跳转，已为你切换到网页支付'); } catch (e) {}
        try { goToStep2Direct(); } catch (e) {}
    }
    // 该拦还是该放：点够次数就放行，否则弹回"请跳转小程序"
    function mpGateOrEscape() {
        try { if (mpTryCount() >= MP_RETRY_LIMIT) { mpGateEscape(); return; } } catch (e) {}
        showMpTipModal();
    }

    // ===== [A1-20260914] 回来时先问服务端"到底进没进小程序" ====="""

# ---------- H5：mpProceedOrGate 里的 _yes/_no ----------
YN_OLD = """        function _yes() { if (_done) { return; } _done = true; try { goToStep2Direct(); } catch (e) {} }
        function _no() { if (_done) { return; } _done = true; try { showMpTipModal(); } catch (e) {} }"""
YN_NEW = """        function _yes() { if (_done) { return; } _done = true; try { mpTryClear(); } catch (e) {} try { goToStep2Direct(); } catch (e) {} }
        function _no() { if (_done) { return; } _done = true; try { mpGateOrEscape(); } catch (e) {} }"""

# ---------- H5：launchMp 每次点击 +1；失败时按次数决定拦还是放 ----------
LAUNCH_OLD = """        try { mpSavePendingJump(); } catch (e) {}   // [A1] 点"去小程序"也存档：回来/页面被重载都能接上订单"""
LAUNCH_NEW = """        try { mpSavePendingJump(); } catch (e) {}   // [A1] 点"去小程序"也存档：回来/页面被重载都能接上订单
        var _mpTryN = mpTryInc();                    // [A1-b] 记一次点击
        console.log('[mp-gate] 第 ' + _mpTryN + ' 次尝试跳小程序 (上限 ' + MP_RETRY_LIMIT + ')');"""
RESCUE_OLD = """            if (document.hidden === false) { showMpTipModal(); }   // [A1] 又没跳过去 -> 弹层再出来，让他再点"""
RESCUE_NEW = """            if (document.hidden === false) { mpGateOrEscape(); }   // [A1-b] 又没跳过去 -> 点够了就放行，否则弹层再出来"""

# ---------- H5：弹层上加一行"用网页支付"（第 2 次起显示） ----------
MODAL_OLD = """            <div class="modal-btns">
                <button id="mpOpenBtn" class="modal-btn modal-btn-confirm" onclick="launchMp()">去小程序</button>
            </div>"""
MODAL_NEW = """            <div class="modal-btns">
                <button id="mpOpenBtn" class="modal-btn modal-btn-confirm" onclick="launchMp()">去小程序</button>
            </div>
            <div id="mpEscLine" style="display:none;text-align:center;margin-top:14px">
                <span id="mpEscBtn" style="font-size:13px;color:#576b95;text-decoration:underline">一直跳不过去？点这里用网页支付</span>
            </div>"""
SHOW_OLD = """    function showMpTipModal() {
        var el = document.getElementById('mpTipModal');
        var hint = document.getElementById('mpRetryHint');
        if (hint) { hint.style.display = 'block'; }
        if (el) { el.classList.add('show'); }"""
SHOW_NEW = """    function showMpTipModal() {
        var el = document.getElementById('mpTipModal');
        var hint = document.getElementById('mpRetryHint');
        if (hint) { hint.style.display = 'block'; }
        // [A1-b] 第 2 次起给用户一个"用网页支付"的出口；点够次数会自动放行
        try {
            var _esc = document.getElementById('mpEscLine');
            var _escBtn = document.getElementById('mpEscBtn');
            if (_esc && _escBtn) {
                if (mpTryCount() >= 2) {
                    _esc.style.display = 'block';
                    _escBtn.textContent = '一直跳不过去？点这里用网页支付（已试 ' + mpTryCount() + ' 次）';
                    if (!_escBtn._bound) { _escBtn._bound = 1; _escBtn.onclick = function () { mpGateEscape(); }; }
                } else { _esc.style.display = 'none'; }
            }
        } catch (e) {}
        if (el) { el.classList.add('show'); }"""

EDITS = [('app.py 注入设置', APP, APP_OLD, APP_NEW),
         ('H5 变量', H5, VAR_OLD, VAR_NEW),
         ('H5 计数/放行函数', H5, FN_ANCHOR, FN_NEW),
         ('H5 _yes/_no', H5, YN_OLD, YN_NEW),
         ('H5 launchMp 计数', H5, LAUNCH_OLD, LAUNCH_NEW),
         ('H5 失败时拦或放', H5, RESCUE_OLD, RESCUE_NEW),
         ('H5 弹层加出口', H5, MODAL_OLD, MODAL_NEW),
         ('H5 显示出口', H5, SHOW_OLD, SHOW_NEW)]


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d))
         for d in os.listdir(BKDIR) if d.startswith('a1b_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    shutil.copy2(os.path.join(src, 'deposit.html'), H5)
    shutil.copy2(os.path.join(src, 'app.py'), APP)
    subprocess.run(['python3', '-m', 'py_compile', APP], check=True)
    print('  已还原自 %s' % src)
    raise SystemExit(0)

texts = {APP: io.open(APP, encoding='utf-8').read(), H5: io.open(H5, encoding='utf-8').read()}
ok = True
for label, path, old, new in EDITS:
    n = texts[path].count(old)
    print('  %s %-22s 命中 %d/1' % ('✓' if n == 1 else '✗', label, n))
    if n != 1:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，一个文件都不写')
for label, path, old, new in EDITS:
    texts[path] = texts[path].replace(old, new, 1)
try:
    compile(texts[APP], APP, 'exec')
except SyntaxError as e:
    raise SystemExit('[中止] app.py 语法不过: %s' % e)
blocks = re.findall(r'<script>(.*?)</script>', texts[H5], re.S)
io.open('/tmp/_a1b_check.js', 'w', encoding='utf-8').write(blocks[-1] if blocks else '')
print('  MP_RETRY_LIMIT 出现次数:', texts[H5].count('MP_RETRY_LIMIT'))
print('  {mp_retry_limit} 占位符在:', '{mp_retry_limit}' in texts[H5])

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'a1b_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
shutil.copy2(APP, os.path.join(d, 'app.py'))
for p, t in texts.items():
    io.open(p, 'w', encoding='utf-8').write(t)
subprocess.run(['python3', '-m', 'py_compile', APP], check=True)
print('  已写入 static/deposit.html 与 app.py; 备份 %s' % d)
