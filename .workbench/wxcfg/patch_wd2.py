# -*- coding: utf-8 -*-
"""[提现直退-补丁v2] 让"普通队列"三段取数都别碰直退的单子。

背景（106 上已复现）: 直退的行在"余额不足->留队重试"时 status 仍是 0、auto_approve_time 是过去时间，
于是同一个批量任务后面的"普通队列"(queue_approve 按通过率 / manual_approve 人工审批段 / auto_approve 定时线程)
会把它当普通提现处理，实测被写成 status=3 approver='自动' —— 与老板定的"排队等、不拒绝"相反。

做法: 在这三段取数的条件里各加一行 `AND COALESCE(w.approver, '') <> 'mp_in_direct'`。
只排除我们自己的标记, **白名单(whitelist_auto)行为一个字不改**。

说明: 一个文件多处补丁必须按"文件 -> 补丁列表"累积后一次写入（v1 用路径做字典 key，前两处被覆盖，
只落了 1 处 —— 教训：同一文件多补丁要显式累加，并复核最终出现次数）。
"""
import io, os, shutil, sys, time

REAL = '--real' in sys.argv
ROOT = '/home/ubuntu/smart-locker'
EXCL = "COALESCE(w.approver, '') <> 'mp_in_direct'"

PATCHES = [
    ('routes/admin_v2.py', '普通队列(queue_approve)别捞直退单',
     "            WHERE w.status = 0 AND l.withdraw_mode = 'queue_approve'\n"
     "            AND w.auto_approve_time IS NOT NULL\n"
     "            AND w.auto_approve_time::timestamp <= NOW()\n"
     "            AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')\n",
     "            AND COALESCE(w.approver, '') <> 'mp_in_direct'\n"),

    ('routes/admin_v2.py', '人工审批段(manual_approve)别捞直退单',
     "            WHERE w.status = 0 AND l.withdraw_mode = 'manual_approve'\n"
     "              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')\n",
     "              AND COALESCE(w.approver, '') <> 'mp_in_direct'\n"),

    ('routes/admin_v2.py', 'auto_approve 定时线程别捞直退单',
     "            WHERE w.status = 0 AND l.withdraw_mode = 'auto_approve'\n"
     "              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')\n",
     "              AND COALESCE(w.approver, '') <> 'mp_in_direct'\n"),
]

print('=' * 68)
print('[提现直退-补丁v2] 模式：%s' % ('真改(--real)' if REAL else '干跑'))

# 1) 锚点检查（按文件累积）
by_file = {}
ok_all = True
for rel, tag, anchor, addon in PATCHES:
    p = os.path.join(ROOT, rel)
    txt = io.open(p, encoding='utf-8').read()
    cnt = txt.count(anchor)
    print('  %s %-34s %-18s 命中 %d/1' % ('✓' if cnt == 1 else '✗', tag, rel, cnt))
    if cnt != 1:
        ok_all = False
        continue
    by_file.setdefault(p, []).append((tag, anchor, addon))

if not ok_all:
    raise SystemExit('\n[中止] 锚点数量不对，一个文件都不写')

if len(by_file.get(os.path.join(ROOT, 'routes/admin_v2.py'), [])) != 3:
    raise SystemExit('[中止] admin_v2.py 应累积 3 处补丁，实得 %d 处（不许漏）'
                     % len(by_file.get(os.path.join(ROOT, 'routes/admin_v2.py'), [])))

# 2) 逐个文件应用全部补丁 + 语法检查
new_texts = {}
for p, items in by_file.items():
    txt = io.open(p, encoding='utf-8').read()
    for tag, anchor, addon in items:
        assert txt.count(anchor) == 1, (p, tag)
        txt = txt.replace(anchor, anchor + addon)
    new_texts[p] = txt
    try:
        compile(txt, p, 'exec')
    except SyntaxError as e:
        raise SystemExit('[中止] 语法检查失败，一个文件都没写：%s' % e)
    got = txt.count(EXCL)
    print('  %s 共 %d 处补丁，语法通过，最终排除条件出现 %d 次' % (os.path.relpath(p, ROOT), len(items), got))
    if got != 3:
        raise SystemExit('[中止] 排除条件应出现 3 次，实得 %d 次，不写' % got)

if not REAL:
    print()
    print('干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

# 3) 备份 + 写入
ts = time.strftime('%Y%m%d_%H%M%S')
bkdir = os.path.join(ROOT, 'backups', 'mpind2_%s' % ts)
os.makedirs(bkdir, exist_ok=True)
for p, txt in new_texts.items():
    rel = os.path.relpath(p, ROOT)
    dst = os.path.join(bkdir, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(p, dst)
    with io.open(p, 'w', encoding='utf-8') as f:
        f.write(txt)
    chk = io.open(p, encoding='utf-8').read()
    compile(chk, p, 'exec')
    print('  已写入 %s，复核排除条件 %d 处，语法通过' % (rel, chk.count(EXCL)))
print('  备份: %s' % bkdir)
