# -*- coding: utf-8 -*-
"""[A1-b 修bug] 第 3 次放行到支付页后，又被弹回设置页。

根因：放行(mpGateEscape)时把计数清零了，但 2 秒兜底定时器 / 从跳转返回的检查还会再跑一次，
      它们看到"计数=0 < 上限"就当成次数不够 -> 又叫 showMpTipModal() -> 而它会 goToStoreStep1()
      -> 用户被从支付页弹回第一步 ✗。

修法：
  1) 加一个"已放行"标记 _mpEscaped：一旦放行过，后续所有"拦或放"的判断都直接放行、绝不再弹拦截层；
  2) 放行时清掉 2 秒兜底/其它定时器；
  3) 放行时**不再清零计数**（保留证据，也避免和兜底逻辑打架）。

用法：python3 patch_a1b_fix.py <项目根> [--real|--revert]
"""
import io, os, re, shutil, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
BKDIR = os.path.join(ROOT, 'backups')

V_OLD = """    // ===== [A1-b 2026-09-14] 点够次数还跳不过去 -> 放行进网页支付（防卡死） ====="""
V_NEW = """    // ===== [A1-b 2026-09-14] 点够次数还跳不过去 -> 放行进网页支付（防卡死） =====
    var _mpEscaped = false;   // ★一旦放行过，就不再拦（否则 2 秒兜底/再次返回会把用户从支付页弹回第一步）"""

E_OLD = """    function mpGateEscape() {
        var _n = mpTryCount();
        mpTryClear();
        mpGateEscapeReport(_n);
        try { showToast('多次未能跳转，已为你切换到网页支付'); } catch (e) {}
        try { goToStep2Direct(); } catch (e) {}
    }"""
E_NEW = """    function mpGateEscape() {
        _mpEscaped = true;                                              // ★放行后不再拦
        if (mpRescueTimer) { clearTimeout(mpRescueTimer); mpRescueTimer = null; }
        if (mpGiveupTimer) { clearTimeout(mpGiveupTimer); mpGiveupTimer = null; }
        var _n = mpTryCount();
        mpGateEscapeReport(_n);                                         // 计数不清零：留证据、也避免和兜底逻辑打架
        try { showToast('多次未能跳转，已为你切换到网页支付'); } catch (e) {}
        try { goToStep2Direct(); } catch (e) {}
    }"""

G_OLD = """    function mpGateOrEscape() {
        try { if (mpTryCount() >= MP_RETRY_LIMIT) { mpGateEscape(); return; } } catch (e) {}
        showMpTipModal();
    }"""
G_NEW = """    function mpGateOrEscape() {
        if (_mpEscaped) { try { goToStep2Direct(); } catch (e) {} return; }   // ★已放行过 -> 直接进支付页，绝不再弹
        try { if (mpTryCount() >= MP_RETRY_LIMIT) { mpGateEscape(); return; } } catch (e) {}
        showMpTipModal();
    }"""

P_OLD = """    function mpProceedOrGate() {
        var _done = false;"""
P_NEW = """    function mpProceedOrGate() {
        if (_mpEscaped) { try { goToStep2Direct(); } catch (e) {} return; }   // ★已放行过 -> 不查、不拦
        var _done = false;"""

EDITS = [('加"已放行"标记', V_OLD, V_NEW),
         ('mpGateEscape 加固', E_OLD, E_NEW),
         ('mpGateOrEscape 加固', G_OLD, G_NEW),
         ('mpProceedOrGate 加固', P_OLD, P_NEW)]


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'deposit.html'))
         for d in os.listdir(BKDIR) if d.startswith('a1bfix_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
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
    print('  %s %-24s 命中 %d/1' % ('✓' if n == 1 else '✗', label, n))
    if n != 1:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，不写')
new = t
for label, old, nw in EDITS:
    new = new.replace(old, nw, 1)
blocks = re.findall(r'<script>(.*?)</script>', new, re.S)
io.open('/tmp/_a1bfix.js', 'w', encoding='utf-8').write(blocks[-1] if blocks else '')
print('  _mpEscaped 出现次数:', new.count('_mpEscaped'))

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'a1bfix_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
io.open(H5, 'w', encoding='utf-8').write(new)
print('  已写入 static/deposit.html; 备份 %s' % os.path.join(d, 'deposit.html'))
