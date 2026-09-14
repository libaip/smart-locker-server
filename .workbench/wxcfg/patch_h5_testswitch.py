# -*- coding: utf-8 -*-
"""[测试开关] 给存包 H5 加两个 URL 参数，方便真机验证公众号订阅弹层：

  ?oatest=1  纯测试：进页面就直接弹"订阅通知"层（不创建订单、不跳小程序、跳过"已订阅"去重）
  ?h5only=1  真流程：跳过跳小程序，直接走 H5 支付（走一遍完整路径，会创建订单）

另外修掉一条误报诊断：原来 5 秒后如果 oaSubWrap 没显示就记 tag_timeout，
现在弹层默认就是隐藏的，所以它每次都会误报 -> 改成只在"该弹没弹"时记 tag_not_prompted。

用法：python3 patch_h5_testswitch.py <项目根> [--real|--revert]
"""
import io, os, shutil, re, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
BKDIR = os.path.join(ROOT, 'backups')

FLAG_OLD = "    var params = new URLSearchParams(window.location.search);"
FLAG_NEW = ("    var params = new URLSearchParams(window.location.search);\n"
            "    var _oaForceH5 = params.get('h5only') === '1';    // [测试] 跳过跳小程序, 强制走 H5\n"
            "    var _oaTestOnly = params.get('oatest') === '1';   // [测试] 只弹订阅层, 不创建订单")

BRANCH_OLD = """                if (allowH5ToMp === 1) {
                    mpSavePendingJump();      // FIX-20260912b 跳转前存档，回来才能接着付
                    mpAutoJump();
                } else {
                    goToStep2Direct();
                }"""
BRANCH_NEW = """                if (_oaForceH5) {
                    oaSubPrompt(goToStep2Direct);   // [测试] 强制走 H5: 先问公众号订阅, 再进支付页
                } else if (allowH5ToMp === 1) {
                    mpSavePendingJump();      // FIX-20260912b 跳转前存档，回来才能接着付
                    mpAutoJump();
                } else {
                    goToStep2Direct();
                }"""

DIAG_OLD = """    setTimeout(function() {
        var w = document.getElementById('oaSubWrap');
        if (w && w.style.opacity !== '1') { oaSubDiag('tag_timeout'); }
    }, 5000);"""
DIAG_NEW = """    setTimeout(function() {
        var w = document.getElementById('oaSubWrap');
        // 弹层默认隐藏, 所以这里只在"该弹却没弹"时才记一条(原来无条件记, 会误报)
        if (w && w.style.display !== 'flex' && !_oaSubShown && !OA_SUBSCRIBE_ENABLED) { oaSubDiag('tag_not_prompted'); }
        if (_oaTestOnly && !_oaSubShown) { oaSubDiag('test_mode_no_prompt'); }
    }, 5000);"""

TEST_OLD = """    if (!_oaSubCb) { return; }"""
TEST_MARK = None   # 占位: 本补丁不依赖它

# 测试模式：进页面就弹（去重也跳过）
PROMPT_OLD = """            if (typeof OA_SUBSCRIBE_ENABLED === 'undefined' || !OA_SUBSCRIBE_ENABLED || _oaSubShown) { cb(); return; }
            var w = document.getElementById('oaSubWrap');
            var tag = document.getElementById('oaSubBtn');
            if (!w || !tag) { cb(); return; }
            _oaSubShown = true; _oaSubCb = cb;"""
PROMPT_NEW = """            if (typeof OA_SUBSCRIBE_ENABLED === 'undefined' || !OA_SUBSCRIBE_ENABLED || _oaSubShown) { cb(); return; }
            var w = document.getElementById('oaSubWrap');
            var tag = document.getElementById('oaSubBtn');
            if (!w || !tag) { cb(); return; }
            _oaSubShown = true; _oaSubCb = cb;
            if (typeof _oaTestOnly !== 'undefined' && _oaTestOnly) {   // [测试] 去重也跳过
                var sk0 = document.getElementById('oaSubSkip');
                if (sk0 && !sk0._bound) { sk0._bound = 1; sk0.onclick = function () { oaSubDiag('skip'); oaSubFinish(); }; }
                w.style.display = 'flex';
                oaSubDiag('prompt_show_test');
                return;
            }"""

BOOT_OLD = """    if (!OA_SUBSCRIBE_ENABLED) {
        // 备用状态: 不请求签名、不显示订阅按钮、不隐藏原按钮,
        // 页面行为与功能上线前完全一致。
        console.log('[oa-subscribe] 功能已关闭(备用状态)');
    } else {
        oaSubDiag('boot', (document.getElementById('oaSubBtn') ? 'tag_in_dom' : 'tag_missing') + '|ready=' + document.readyState + '|ua=' + (navigator.userAgent || '').slice(-60));
        try { oaSubscribeInit(); } catch (e) { oaSubDiag('init_exc', (e && e.message) || String(e)); }
    }"""
BOOT_NEW = """    if (!OA_SUBSCRIBE_ENABLED) {
        // 备用状态: 不请求签名、不显示订阅按钮、不隐藏原按钮,
        // 页面行为与功能上线前完全一致。
        console.log('[oa-subscribe] 功能已关闭(备用状态)');
    } else {
        oaSubDiag('boot', (document.getElementById('oaSubBtn') ? 'tag_in_dom' : 'tag_missing') + '|ready=' + document.readyState + '|ua=' + (navigator.userAgent || '').slice(-60));
        try { oaSubscribeInit(); } catch (e) { oaSubDiag('init_exc', (e && e.message) || String(e)); }
        if (_oaTestOnly) {
            // [测试] 进页面就弹订阅层（不创建订单、不跳小程序）
            setTimeout(function () { try { oaSubPrompt(function () { console.log('[oa-subscribe] 测试模式结束'); }); } catch (e) {} }, 1200);
        }
    }"""


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'deposit.html'))
         for d in os.listdir(BKDIR) if d.startswith('h5test_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    shutil.copy2(src, H5)
    print('  已还原自 %s' % src)
    raise SystemExit(0)

t = io.open(H5, encoding='utf-8').read()
checks = [('h5only/oatest 参数解析', FLAG_OLD, 1),
          ('下单回调分支', BRANCH_OLD, 1),
          ('诊断误报修正', DIAG_OLD, 1),
          ('测试模式跳过去重', PROMPT_OLD, 1),
          ('测试模式进页面就弹', BOOT_OLD, 1)]
ok = True
for label, a, want in checks:
    n = t.count(a)
    print('  %s %-24s 命中 %d/%d' % ('✓' if n == want else '✗', label, n, want))
    if n != want:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，不写')

new = t
for label, a, want in checks:
    pair = {'h5only/oatest 参数解析': (FLAG_OLD, FLAG_NEW),
            '下单回调分支': (BRANCH_OLD, BRANCH_NEW),
            '诊断误报修正': (DIAG_OLD, DIAG_NEW),
            '测试模式跳过去重': (PROMPT_OLD, PROMPT_NEW),
            '测试模式进页面就弹': (BOOT_OLD, BOOT_NEW)}[label]
    new = new.replace(pair[0], pair[1], 1)
blocks = re.findall(r'<script>(.*?)</script>', new, re.S)
io.open('/tmp/_h5_test_check.js', 'w', encoding='utf-8').write(blocks[-1] if blocks else '')
print('  内联 JS 已抽出待检查')

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'h5test_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
io.open(H5, 'w', encoding='utf-8').write(new)
print('  已写入 static/deposit.html; 备份 %s' % os.path.join(d, 'deposit.html'))
