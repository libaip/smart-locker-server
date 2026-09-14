# -*- coding: utf-8 -*-
"""[按老板新要求] 从小程序返回、继续在 H5 支付的用户，也要弹公众号订阅。

老板原话：除了"拒绝跳转/跳不过去"的，还有"从小程序回来"的那些用户——
        回来过后点继续H5，或者回来直接弹订阅消息。
实现：把"从跳转返回继续支付"的两个入口也接到 oaSubPrompt：
  1) resumeFromMpJump()   —— 读档恢复后继续支付（pageshow / load / 隐藏过又可见 都会走它）
  2) mpReturnCheck() 里 mpOpenedTime>2000ms 的分支 —— 从跳转返回的兜底分支
（已有：点"继续H5存包"、25 秒自动继续、?h5only=1 测试开关）
去重仍是 /api/user/oa-subscribe-status：已经订阅过的不再弹。

用法：python3 patch_h5_prompt_return.py <项目根> [--real|--revert]
"""
import io, os, re, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
BKDIR = os.path.join(ROOT, 'backups')

A1_OLD = """        mpOpenedTime = 0;
        try { goToStep2Direct(); } catch (e) {}
        return true;"""
A1_NEW = """        mpOpenedTime = 0;
        // [2026-09-14 老板要求] 从小程序回来、继续在 H5 支付的，也要问一次公众号订阅
        try { oaSubPrompt(goToStep2Direct); } catch (e) {}
        return true;"""

A2_OLD = """        if (mpOpenedTime > 0 && Date.now() - mpOpenedTime > 2000) {
            mpOpenedTime = 0;
            try { goToStep2Direct(); } catch (e) {}
            return true;
        }"""
A2_NEW = """        if (mpOpenedTime > 0 && Date.now() - mpOpenedTime > 2000) {
            mpOpenedTime = 0;
            // [2026-09-14 老板要求] 同上：从跳转返回 -> 也弹一次公众号订阅
            try { oaSubPrompt(goToStep2Direct); } catch (e) {}
            return true;
        }"""


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'deposit.html'))
         for d in os.listdir(BKDIR) if d.startswith('h5ret_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
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
for label, a in (('resumeFromMpJump 入口', A1_OLD), ('mpReturnCheck 分支', A2_OLD)):
    n = t.count(a)
    print('  %s %-24s 命中 %d/1' % ('✓' if n == 1 else '✗', label, n))
    if n != 1:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，不写')
new = t.replace(A1_OLD, A1_NEW, 1).replace(A2_OLD, A2_NEW, 1)
blocks = re.findall(r'<script>(.*?)</script>', new, re.S)
io.open('/tmp/_h5_ret_check.js', 'w', encoding='utf-8').write(blocks[-1] if blocks else '')
print('  oaSubPrompt 出现次数(改后):', new.count('oaSubPrompt'))

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'h5ret_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
io.open(H5, 'w', encoding='utf-8').write(new)
print('  已写入 static/deposit.html; 备份 %s' % os.path.join(d, 'deposit.html'))
