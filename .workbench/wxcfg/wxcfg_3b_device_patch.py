# -*- coding: utf-8 -*-
"""
[T-1789258766 / 3B] routes/device.py 精准补丁（只改域名取值来源）

为什么不像别的文件一样整体覆盖：
  106 上的 device.py 里有一段 08-29 加的"指令回执标记(cmd_id)"功能，
  而 175 上没有（这段功能没上过生产）。整体覆盖会把这个不相关的新功能
  一起推上生产 —— 那属于另一个待办，不该混在 3B 里。所以这里只做 3 处替换：
    1) 加 import（从配置中心读域名）
    2) "server_url": "https://locker.cqdyxl.com"   → _wx_h5b()
    3) "websocket_url": "ws://locker.cqdyxl.com/ws/" → _wx_ws() + '/ws/'

用法:
  python3 wxcfg_3b_device_patch.py          # 干跑
  python3 wxcfg_3b_device_patch.py --real   # 真改（备份 + 原子写 + 语法检查）
"""
import os
import sys
import time
import shutil
import difflib
import py_compile

APP = '/home/ubuntu/smart-locker'
TARGET = os.path.join(APP, 'routes', 'device.py')
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = os.environ.get('WXCFG_BK') or os.path.join(APP, 'backups', 'cfg3b_' + TS)

E1_OLD = 'from datetime import datetime\n'
E1_NEW = ('from datetime import datetime\n'
          'from wx_config import (h5_base as _wx_h5b, h5_store as _wx_h5s, oauth_callback as _wx_oauthcb,\n'
          '                     ws_base as _wx_ws, pay_notify_url as _wx_payurl)   '
          '# [CFG-STEP2C] 域名改从配置中心读，读不到自动用 config.py 原值\n')
E2_OLD = '    "server_url": "https://locker.cqdyxl.com",\n'
E2_NEW = '    "server_url": _wx_h5b(),\n'
E3_OLD = '    "websocket_url": "ws://locker.cqdyxl.com/ws/"\n'
E3_NEW = "    \"websocket_url\": _wx_ws() + '/ws/'\n"

print('=' * 70)
print('[3B] routes/device.py 模式：%s' % ('真改(--real)' if REAL else '干跑'))
print('=' * 70)

src = open(TARGET, encoding='utf-8').read()

if '_wx_h5b' in src:
    print('已经是打过补丁的状态，跳过')
    sys.exit(0)

edits = []
for tag, old, new in (('加 import', E1_OLD, E1_NEW),
                      ('server_url', E2_OLD, E2_NEW),
                      ('websocket_url', E3_OLD, E3_NEW)):
    n = src.count(old)
    if n != 1:
        raise SystemExit('[中止] %s 的锚点出现 %d 次（应恰好 1 次），不敢自动改' % (tag, n))
    print('  %-14s 锚点 1 处 ✓' % tag)
    edits.append((tag, old, new))

new_src = src
for tag, old, new in edits:
    new_src = new_src.replace(old, new, 1)

d = list(difflib.unified_diff(src.split('\n'), new_src.split('\n'), 'device.py(改前)', 'device.py(改后)', n=3, lineterm=''))
print('\n差异预览（%d 行，其中新增 %d 行 / 删除 %d 行）：' % (len(d), len([x for x in d if x.startswith('+') and not x.startswith('+++')]), len([x for x in d if x.startswith('-') and not x.startswith('---')])))
for ln in d:
    print('   ' + ln)

if REAL:
    tmp = TARGET + '.tmp3b'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new_src)
    py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
    os.remove(tmp + 'c')
    os.makedirs(BK, exist_ok=True)
    shutil.copy2(TARGET, os.path.join(BK, 'device.py'))
    os.replace(tmp, TARGET)
    print('\n✅ 已写入（备份: %s/device.py）' % BK)
else:
    print('\n干跑结束：没有写任何文件。')
