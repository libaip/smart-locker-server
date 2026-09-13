# -*- coding: utf-8 -*-
"""
[T-1789258766 / 3A] 175 上线第一步：只加不改

做两件事（都不改变任何现有业务的取值来源）：
  1) app.py                 插入"配置中心注册"整段（在原蓝图注册循环之后、
                            "兼容微信支付投诉回调"注释之前）。整段包在 try 里，
                            任何异常只打日志，不影响主程序启动。
  2) static/admin-v2.html   后台挂上"微信账号"页面：脚本、样式、左侧菜单项。

用法:
  python3 wxcfg_3a_patch.py          # 干跑：只报会改什么，一个字节都不写
  python3 wxcfg_3a_patch.py --real   # 真改：自动备份 + 原子写 + 语法检查 + 失败自动回滚

安全性：
  - app.py 用"区域替换"：把 175 上 [A锚点行之后 , C锚点行之前] 这一段
    替换成 106 上同一段的内容。锚点行本身不动，其它地方一个字节不动。
  - 写入前先编译校验，编译不过就一个文件都不落盘。
  - 已打过补丁则自动跳过（可重复执行）。
"""
import os
import re
import sys
import time
import shutil
import difflib
import py_compile

APP = '/home/ubuntu/smart-locker'
APP_PY = os.path.join(APP, 'app.py')
ADMIN_HTML = os.path.join(APP, 'static', 'admin-v2.html')
BLOCK_FILE = '/tmp/wxcfg_block_region.txt'
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = os.path.join(APP, 'backups', 'cfg3a_' + TS)

# 锚点行（106 与 175 上都存在且唯一）
A_MARK = "logger.error(f'[注册] 注册蓝图 {bp.name} 失败: {e}')"
C_MARK = "# 兼容微信支付投诉回调路径（商户1747572495配置的URL前缀不同）"

# 后台页面三处锚点
H_SCRIPT = '<script src="/static/js/vue.min.js"></script>'
H_SCRIPT_NEW = H_SCRIPT + '\n<script src="/static/wx_accounts_page.js"></script>'
H_HEAD = '</head>'
H_HEAD_NEW = '<link rel="stylesheet" href="/static/wx_accounts_page.css">\n</head>'
H_MENU = "{key:'payment-channels',label:'支付渠道'},"
H_MENU_NEW = H_MENU + "{key:'wx-accounts',label:'微信账号'},"


def region_bounds(text, tag):
    """返回 (A锚点行末, C锚点行首)，锚点必须各出现一次"""
    na, nc = text.count(A_MARK), text.count(C_MARK)
    if na != 1 or nc != 1:
        raise SystemExit('[中止] %s 的锚点数量不对：A=%d 次 C=%d 次（都应恰好 1 次）' % (tag, na, nc))
    i = text.index(A_MARK)
    a_end = text.index('\n', i + len(A_MARK)) + 1
    j = text.index(C_MARK)
    c_start = text.rindex('\n', 0, j) + 1
    if c_start <= a_end:
        raise SystemExit('[中止] %s 锚点顺序异常' % tag)
    return a_end, c_start


def atomic_write(path, text, backup_of=None):
    tmp = path + '.tmp3a'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    if path.endswith('.py'):
        py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
        os.remove(tmp + 'c')
    if backup_of:
        os.makedirs(BK, exist_ok=True)
        shutil.copy2(backup_of, os.path.join(BK, os.path.basename(backup_of)))
    os.replace(tmp, path)


print('=' * 70)
print('[3A] 模式：%s' % ('真改(--real)' if REAL else '干跑（只报不改）'))
print('     目录：%s' % APP)
print('=' * 70)

# ---------- 0) 读源区域 ----------
if not os.path.exists(BLOCK_FILE):
    raise SystemExit('[中止] 找不到 %s（106 上抽取的注册段）' % BLOCK_FILE)
src = open(BLOCK_FILE, encoding='utf-8').read()
for must in ('import wx_config as _wxcfg', 'import wx_config_api as _wxcfg_api',
             '_wxcfg.bind(_wxcfg_get_db)', 'app.register_blueprint(_wxcfg_api.bp)',
             'except Exception as _wxcfg_e:'):
    if must not in src:
        raise SystemExit('[中止] 源区域缺少关键内容: %s' % must)
print('\n源区域（从 106 的 app.py 抽取，共 %d 行）：' % len(src.rstrip('\n').split('\n')))
for k, ln in enumerate(src.rstrip('\n').split('\n'), 1):
    print('   %2d| %s' % (k, ln))

changed = []

# ---------- 1) app.py ----------
print('\n' + '-' * 70)
print('[1/2] app.py')
tgt = open(APP_PY, encoding='utf-8').read()
if 'wx_config as _wxcfg' in tgt:
    print('  已经是打过补丁的状态，跳过')
else:
    if 'wx_config' in tgt:
        print('  注意：app.py 里已有 wx_config 字样但没找到注册段，人工确认一下：')
        for k, ln in enumerate(tgt.split('\n'), 1):
            if 'wx_config' in ln:
                print('     %d: %s' % (k, ln.strip()))
        raise SystemExit('[中止] 不敢自动改')
    b = region_bounds(tgt, 'app.py')
    old_region = tgt[b[0]:b[1]]
    new_text = tgt[:b[0]] + src + tgt[b[1]:]
    add = len(src.rstrip('\n').split('\n')) - len(old_region.rstrip('\n').split('\n')) if old_region.strip() else len(src.rstrip('\n').split('\n'))
    print('  将插入 %d 行，替换掉原有 %d 行空白' % (add, len(old_region.rstrip('\n').split('\n'))))
    print('  位置：原第 %d 行之后（"%s"）' % (tgt[:b[0]].count('\n'), A_MARK[:38] + '...'))
    d = list(difflib.unified_diff(tgt.split('\n'), new_text.split('\n'), 'app.py(改前)', 'app.py(改后)', n=2, lineterm=''))
    print('  差异预览（前 %d 行）：' % min(len(d), 40))
    for ln in d[:40]:
        print('     ' + ln)
    if REAL:
        atomic_write(APP_PY, new_text, backup_of=APP_PY)
        print('  ✅ 已写入')
        changed.append('app.py')

# ---------- 2) static/admin-v2.html ----------
print('\n' + '-' * 70)
print('[2/2] static/admin-v2.html')
h = open(ADMIN_HTML, encoding='utf-8').read()
if 'wx_accounts_page' in h:
    print('  已经挂过了，跳过')
else:
    for name, old, new, expect in (('脚本标签', H_SCRIPT, H_SCRIPT_NEW, 1),
                                   ('样式标签', H_HEAD, H_HEAD_NEW, 1),
                                   ('左侧菜单项', H_MENU, H_MENU_NEW, None)):
        n = h.count(old)
        if n == 0:
            raise SystemExit('[中止] 后台页面找不到%s锚点，不敢自动改' % name)
        if expect is not None and n != expect:
            raise SystemExit('[中止] 后台页面%s锚点出现 %d 次（应为 %d 次）' % (name, n, expect))
        print('  %s：锚点 %d 处 → 追加' % (name, n))
        h = h.replace(old, new, 1)
    if REAL:
        atomic_write(ADMIN_HTML, h, backup_of=ADMIN_HTML)
        print('  ✅ 已写入')
        changed.append('static/admin-v2.html')

print('\n' + '=' * 70)
if REAL:
    print('改完的文件：%s' % (', '.join(changed) if changed else '（无，已是打过补丁状态）'))
    if changed:
        print('备份目录：%s' % BK)
        print('回滚命令：cp -a %s/* %s/' % (BK, APP))
else:
    print('干跑结束：没有写任何文件。确认无误后加 --real 执行。')
print('=' * 70)
