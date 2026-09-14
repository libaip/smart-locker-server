# -*- coding: utf-8 -*-
"""[A1-a] 拒绝/无法跳转小程序 -> 退回第一步 + 弹「请跳转小程序进行下一步」，只有【去小程序】一个出口（可反复点），没有任何绕过。

老板定的 A1-a：
  - 用户跳不过去（或点"继续H5存包"）-> **不进支付页**，退回第 1 步（设置手机号页，手机号自动回填，订单保留复用）
  - 弹层：「请跳转小程序进行下一步」+ 只有一个主按钮【去小程序】（真实点击，成功率最高，可点无数次）
  - **取消 25 秒自动放行**（原来是 25 秒后自动进支付页 = 绕过口）
  - **取消「继续H5存包」按钮**（不再有"绕过"选项）
  - 点【去小程序】后若还是没跳过去 -> 2 秒后弹层自动再出来，让他再点（闭环，不卡死也不放行）
  - 从小程序成功返回 -> 直接进支付页（不弹任何订阅窗口）

用法：python3 patch_a1_gate.py <项目根> [--real|--revert]
"""
import io, os, re, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
BKDIR = os.path.join(ROOT, 'backups')

HTML_OLD = """    <div id="mpTipModal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-title">存包提示</div>
            <p id="mpTipDesc" style="text-align:center;font-size:14px;color:#666;margin-bottom:10px">建议使用小程序完成存包，可收到微信通知；打不开请点“继续H5存包”</p>
            <p id="mpRetryHint" style="display:none;text-align:center;font-size:13px;color:#e6a23c;margin-bottom:10px">没跳过去？点下面“打开小程序”再试一次</p>
            <div class="modal-btns">
                <button class="modal-btn modal-btn-cancel" onclick="closeMpTipModal()">继续H5存包</button>
                <button id="mpOpenBtn" class="modal-btn modal-btn-confirm" onclick="launchMp()">打开小程序</button>
            </div>
        </div>
    </div>"""
HTML_NEW = """    <div id="mpTipModal" class="modal-overlay" onclick="if(event.target===this){closeMpTipModal()}">
        <div class="modal-box">
            <div class="modal-title">请跳转小程序进行下一步</div>
            <p id="mpTipDesc" style="text-align:center;font-size:14px;color:#666;margin-bottom:10px">跳转后请允许“订阅通知”，这样才能收到押金退还 / 退款到账的微信提醒</p>
            <p id="mpRetryHint" style="display:none;text-align:center;font-size:13px;color:#e6a23c;margin-bottom:10px">没跳过去？再点一次【去小程序】就行（点几次都可以）</p>
            <div class="modal-btns">
                <button id="mpOpenBtn" class="modal-btn modal-btn-confirm" onclick="launchMp()">去小程序</button>
            </div>
        </div>
    </div>"""

CLOSE_OLD = """    function closeMpTipModal() {
        document.getElementById('mpTipModal').classList.remove('show');
        if (mpGiveupTimer) { clearTimeout(mpGiveupTimer); mpGiveupTimer = null; }
        if (mpRescueTimer) { clearTimeout(mpRescueTimer); mpRescueTimer = null; }
        mpOpenedTime = 0;
        oaSubPrompt(goToStep2Direct);   // [2026-09-14] 用户选了"继续H5存包" -> 先问一次公众号订阅
    }"""
CLOSE_NEW = """    function closeMpTipModal() {
        // [A1-20260914] 只是关掉提示层，人留在第 1 步；**没有"跳过"出口**：不跳小程序就不进支付页
        document.getElementById('mpTipModal').classList.remove('show');
        if (mpGiveupTimer) { clearTimeout(mpGiveupTimer); mpGiveupTimer = null; }
        if (mpRescueTimer) { clearTimeout(mpRescueTimer); mpRescueTimer = null; }
        mpOpenedTime = 0;
    }"""

SHOW_OLD = """    function showMpTipModal() {
        var el = document.getElementById('mpTipModal');
        var hint = document.getElementById('mpRetryHint');
        if (hint) { hint.style.display = 'block'; }
        if (el) { el.classList.add('show'); }
        mpPrefetchScheme();
        if (mpGiveupTimer) { clearTimeout(mpGiveupTimer); }
        mpGiveupTimer = setTimeout(function() { mpGiveupTimer = null; oaSubPrompt(goToStep2Direct); }, 25000);
    }"""
SHOW_NEW = """    function showMpTipModal() {
        var el = document.getElementById('mpTipModal');
        var hint = document.getElementById('mpRetryHint');
        if (hint) { hint.style.display = 'block'; }
        if (el) { el.classList.add('show'); }
        mpPrefetchScheme();
        if (mpGiveupTimer) { clearTimeout(mpGiveupTimer); mpGiveupTimer = null; }   // [A1] 取消"25 秒自动放行"
        // [A1] 跳不过去就退回第 1 步（订单保留复用，不会多占柜门），不放行进支付页
        try { goToStoreStep1(); } catch (e) {}
    }"""

LAUNCH_OLD = """        mpOpenedTime = Date.now();
        if (mpScheme) { window.location.href = mpScheme; return; }"""
LAUNCH_NEW = """        mpOpenedTime = Date.now();
        mpStartReturnWatch();
        if (mpRescueTimer) { clearTimeout(mpRescueTimer); }
        mpRescueTimer = setTimeout(function() {
            mpRescueTimer = null;
            if (mpOpenedTime === 0) { return; }
            if (document.hidden === false) { showMpTipModal(); }   // [A1] 又没跳过去 -> 弹层再出来，让他再点
        }, 2000);
        if (mpScheme) { window.location.href = mpScheme; return; }"""

H5ONLY_OLD = """                if (_oaForceH5) {
                    oaSubPrompt(goToStep2Direct);   // [测试] 强制走 H5: 先问公众号订阅, 再进支付页
                } else if (allowH5ToMp === 1) {"""
H5ONLY_NEW = """                if (_oaForceH5) {
                    showMpTipModal();   // [测试] 强制走 H5: 直接看到 A1 的拦截弹层(请跳转小程序)
                } else if (allowH5ToMp === 1) {"""

EDITS = [('弹层文案/按钮', HTML_OLD, HTML_NEW),
         ('closeMpTipModal 去掉绕过', CLOSE_OLD, CLOSE_NEW),
         ('showMpTipModal 退回第一步+取消25秒', SHOW_OLD, SHOW_NEW),
         ('launchMp 失败再弹', LAUNCH_OLD, LAUNCH_NEW),
         ('h5only 测试分支', H5ONLY_OLD, H5ONLY_NEW)]


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'deposit.html'))
         for d in os.listdir(BKDIR) if d.startswith('a1gate_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
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
    print('  %s %-30s 命中 %d/1' % ('✓' if n == 1 else '✗', label, n))
    if n != 1:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，不写')
new = t
for label, old, nw in EDITS:
    new = new.replace(old, nw, 1)
blocks = re.findall(r'<script>(.*?)</script>', new, re.S)
io.open('/tmp/_a1_check.js', 'w', encoding='utf-8').write(blocks[-1] if blocks else '')
print('  25秒放行是否已清除:', 'mpGiveupTimer = setTimeout' not in new)
print('  "继续H5存包"按钮是否已清除:', '继续H5存包' not in new)

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'a1gate_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
io.open(H5, 'w', encoding='utf-8').write(new)
print('  已写入 static/deposit.html; 备份 %s' % os.path.join(d, 'deposit.html'))
