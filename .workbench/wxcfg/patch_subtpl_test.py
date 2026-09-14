# -*- coding: utf-8 -*-
"""[实验] 临时把 /api/user/subscribe-templates 下发的"寄存成功"模板换掉，验证小程序弹窗是否真用服务端下发的列表。

原理：把 deposit 那一项指向"押金退还"的 ID（不与其它项冲突、仍是合法模板 ID）。
  - 若小程序弹窗是"用服务端下发的值" -> 弹窗里"寄存成功通知"会消失（微信对重复模板会自动去重）
  - 若小程序自己写死了 ID        -> 弹窗里三条照旧不变
所以老板只要看弹窗里还剩几条、分别叫什么，就能判定。

用法：
  python3 patch_subtpl_test.py            # 干跑（只看锚点）
  python3 patch_subtpl_test.py --real     # 真改（先备份）
  python3 patch_subtpl_test.py --revert   # 从最近一次备份还原
"""
import io, os, shutil, subprocess, sys, time

ROOT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
P = os.path.join(ROOT, 'routes', 'user.py')
BKDIR = os.path.join(ROOT, 'backups')

OLD = "    _deposit = _wx_tpl('subscribe_deposit', 'mp', 'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk')     # 寄存成功"
NEW = "    _deposit = _general  # [实验] 临时指向押金退还, 用于验证小程序弹窗是否用服务端下发值"


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    cands = []
    for d in os.listdir(BKDIR):
        if d.startswith('subtpl_'):
            f = os.path.join(BKDIR, d, 'user.py')
            if os.path.isfile(f):
                cands.append((os.path.getmtime(f), f))
    return sorted(cands)[-1][1] if cands else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到实验备份')
    shutil.copy2(src, P)
    print('  已从备份还原: %s -> %s' % (src, P))
    subprocess.run(['python3', '-m', 'py_compile', P], check=True)
    print('  语法通过; md5=%s' % subprocess.run(['md5sum', P], capture_output=True, text=True).stdout.strip())
    raise SystemExit(0)

txt = io.open(P, encoding='utf-8').read()
n = txt.count(OLD)
print('  锚点命中: %d/1' % n)
if n != 1:
    raise SystemExit('[中止] 锚点不唯一, 不写')
new = txt.replace(OLD, NEW)
try:
    compile(new, P, 'exec')
except SyntaxError as e:
    raise SystemExit('[中止] 语法不过: %s' % e)
print('  语法检查通过')
if not REAL:
    print('  干跑结束, 什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'subtpl_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(P, os.path.join(d, 'user.py'))
io.open(P, 'w', encoding='utf-8').write(new)
compile(io.open(P, encoding='utf-8').read(), P, 'exec')
print('  已写入; 备份 %s' % os.path.join(d, 'user.py'))
print('  md5=%s' % subprocess.run(['md5sum', P], capture_output=True, text=True).stdout.strip())
print('  改完记得重载: sudo kill -HUP <master pid>; 还原用 --revert')
