#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[S524b-20260921] 修 `/alipay/login` 读不到 uid 的 bug（只改 user.py 里这一行）

背景
------------------------------------------------------------------
· 现象：`users.alipay_uid` 有 69909 行、其中 0 行有值 → 支付宝小程序身份从来没落库，
  小程序侧 uid 一直填不上。
· 原因：支付宝已切 **openid 模式**，`alipay.system.oauth.token` 返回的是 `open_id`，
  而 `routes/user.py` 的 `alipay_login()` 只读 `user_id`（恒为空）→ 直接
  `return 400 未取到支付宝用户标识`，永远走不到写库那一步。
· 参考：同一批次 /pay/notify/alipay 的付款人 uid 也已切 `buyer_open_id`（见 S524）。

本补丁做什么（1 行，加法、兼容两种模式）
------------------------------------------------------------------
  锚点：  alipay_uid = str(r.get('user_id') or '').strip()
  改成：  alipay_uid = str(r.get('open_id') or r.get('user_id') or '').strip()   # [S524b] ...

  ——优先 open_id，老的 user_id 保留兼容；不改其它任何逻辑（不碰微信、不碰身份开关）。

用法
------------------------------------------------------------------
  # 1) 干跑（默认，不写任何文件，只打印锚点命中数与 md5）
  python3 patch_s524b_alipay_openid.py routes/user.py

  # 2) 真打（先 shutil.copy2 备份，再写盘）
  python3 patch_s524b_alipay_openid.py routes/user.py --apply \
      --backup-dir backups/s524_buyer_openid_20260921_2159/routes

退出码：0=成功；2=锚点不唯一/已打过/语法校验失败（**不写任何文件**）。
"""
from __future__ import print_function

import hashlib
import os
import py_compile
import shutil
import sys
import time

MARK = '[S524b]'

OLD = """        alipay_uid = str(r.get('user_id') or '').strip()
"""

NEW = """        alipay_uid = str(r.get('open_id') or r.get('user_id') or '').strip()   # [S524b] 支付宝已切 openid 模式
"""

GUARD = "def alipay_login():"


def md5_of(path):
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def md5_text(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    apply = '--apply' in argv[1:]
    backup_dir = None
    if '--backup-dir' in argv:
        backup_dir = argv[argv.index('--backup-dir') + 1]

    if not os.path.isfile(path):
        print('[S524b][FAIL] 文件不存在: %s' % path)
        return 2

    # 按要求用 newline='' 读取，保证原样读入、原样写回（不擅自改行尾）
    with open(path, encoding='utf-8', newline='') as f:
        src = f.read()

    before_md5 = md5_text(src)
    print('[S524b] 目标文件        : %s' % path)
    print('[S524b] 改前 md5        : %s' % before_md5)
    print('[S524b] 改前 md5(文件)  : %s' % md5_of(path))
    if before_md5 != md5_of(path):
        print('[S524b][WARN] 文本 md5 与文件 md5 不一致（编码/换行差异），继续但请留意')

    if GUARD not in src:
        print('[S524b][FAIL] 文件里找不到 %s —— 目标文件不对' % GUARD)
        return 2
    if MARK in src:
        print('[S524b][FAIL] 文件里已经出现 %s —— 本补丁可能已打过，拒绝重复执行' % MARK)
        return 2

    # ---- 锚点唯一性校验：不唯一 => 一个字节都不写 ----
    print('[S524b] ---- 锚点校验 ----')
    n = src.count(OLD)
    print('[S524b]   A /alipay/login 读 open_id  count=%d (必须=1)' % n)
    if n != 1:
        print('[S524b][FAIL] 锚点命中 %d 次（应为 1），未写任何文件' % n)
        return 2
    out = src.replace(OLD, NEW, 1)

    after_md5 = md5_text(out)
    print('[S524b] ---- 结果 ----')
    print('[S524b] 改后 md5        : %s' % after_md5)
    print('[S524b] 新增 %s 注释处数: %d' % (MARK, out.count(MARK)))

    if not apply:
        print('[S524b] 干跑模式（未加 --apply）：不写任何文件。确认无误后加 --apply 真打。')
        return 0

    # ---- 先写临时文件 + 语法校验，通过后才覆盖，并留 copy2 备份 ----
    tmp = path + '.s524btmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(out)
    if md5_of(tmp) != after_md5:
        print('[S524b][FAIL] 临时文件 md5 与预期不符，放弃（未覆盖原文件）')
        os.remove(tmp)
        return 2
    try:
        py_compile.compile(tmp, cfile=tmp + '.pyc', doraise=True)
        print('[S524b] py_compile 校验通过 ✅')
    except Exception as e:
        print('[S524b][FAIL] 语法校验失败，未覆盖原文件: %s' % e)
        os.remove(tmp)
        return 2

    if not backup_dir:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(path)),
                                  'backups', 's524b_%s' % time.strftime('%Y%m%d_%H%M%S'))
    if not os.path.isdir(backup_dir):
        os.makedirs(backup_dir)
    backup = os.path.join(backup_dir, os.path.basename(path))
    shutil.copy2(path, backup)
    print('[S524b] 已备份(copy2)  : %s' % backup)

    shutil.move(tmp, path)
    try:
        os.remove(tmp + '.pyc')
    except Exception:
        pass

    print('[S524b] 写入完成        : %s' % path)
    print('[S524b] 回读 md5        : %s' % md5_of(path))
    print('[S524b] 期望 md5        : %s' % after_md5)
    print('[S524b] 回滚命令        : cp -a %s %s' % (backup, path))
    return 0 if md5_of(path) == after_md5 else 2


if __name__ == '__main__':
    sys.exit(main())
