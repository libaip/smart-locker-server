# -*- coding: utf-8 -*-
"""[提现直退-20260914] 175 生产【只读】验证脚本。

原则：一个字都不写库。只做
  1) 判定函数 has_mp_menu_entry 真值表（点过菜单的号 = True，随机号 = False）
  2) 表结构 / 现有数据分布（确认没有 mp_in_direct 的历史行）
  3) 新版"必退"分支的取数范围（照抄 SQL，只 SELECT 不更新）——看得到会捞哪些单子
  4) 三个文件的补丁点是否都在
"""
import sys
sys.path.insert(0, '/home/ubuntu/smart-locker')

DSN = 'postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker'
import psycopg2
conn = psycopg2.connect(DSN)
cur = conn.cursor()

print('=' * 68)
print('【1】判定函数 has_mp_menu_entry（真值表）')
try:
    import helpers
    cur.execute("select phone, count(*) from wx_oa_messages where event='view_miniprogram' and coalesce(phone,'')<>'' group by 1 order by 2 desc limit 3")
    hits = cur.fetchall()
    if not hits:
        print('  ⚠️ 生产库没有带手机号的 view_miniprogram 记录，只能验反例')
    for ph, n in hits:
        r = helpers.has_mp_menu_entry(phone=ph)
        print('  点过菜单的号 %s（%d 次）-> %s  %s' % (ph, n, r, '✅' if r else '❌'))
    r1 = helpers.has_mp_menu_entry(phone='13700000000')
    r2 = helpers.has_mp_menu_entry()
    print('  没点过的号 13700000000 -> %s  %s' % (r1, '✅' if not r1 else '❌'))
    print('  空参数 -> %s  %s' % (r2, '✅' if not r2 else '❌'))
    cur.execute("select count(distinct phone) from wx_oa_messages where event='view_miniprogram' and coalesce(phone,'')<>''")
    print('  生产"曾经点过菜单"的号总数（判定口径的池子）: %s 人' % cur.fetchone()[0])
except Exception as e:
    print('  ❌ 判定函数调用失败：%s %s' % (type(e).__name__, e))
    print('     （若只是脚本环境取不到库连接，下面第 3 项仍能说明问题）')

print()
print('=' * 68)
print('【2】数据现状（确认没有历史 mp_in_direct 行）')
cur.execute("select coalesce(approver,'(空)') as ap, count(*) as n, sum(case when status=0 then 1 else 0 end) as pending from withdrawal_records where approver in ('mp_in_direct','whitelist_auto') or status=0 group by 1 order by 2 desc limit 12")
for ap, n, pending in cur.fetchall():
    print('  审批人=%-14s 共 %-7s 其中待处理 %s' % (ap, n, pending))
cur.execute("select count(*) from withdrawal_records where approver='mp_in_direct'")
print('  mp_in_direct 行数(改前应 0，改后才有): %s' % cur.fetchone()[0])

print()
print('=' * 68)
print('【3】新版"必退"分支会捞哪些单子（照抄 SQL，只读）')
cur.execute("""
    SELECT w.id, w.user_phone, w.amount, w.approver
    FROM withdrawal_records w
    WHERE w.status = 0 AND w.approver IN ('whitelist_auto', 'mp_in_direct')
      AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
      AND (w.next_attempt_at IS NULL OR w.next_attempt_at <= NOW())
    LIMIT 200
""")
rows = cur.fetchall()
print('  本轮会被处理的必退单数: %s' % len(rows))
for r in rows[:10]:
    print('    id=%s 手机=%s 金额=%s 审批人=%s' % r)
cur.execute("""
    SELECT count(*) FROM withdrawal_records w
    WHERE w.status = 0 AND w.approver IN ('whitelist_auto', 'mp_in_direct')
      AND w.next_attempt_at IS NOT NULL AND w.next_attempt_at > NOW()
""")
print('  正在"排队等余额"（下次重试时间未到）的单数: %s' % cur.fetchone()[0])

print()
print('=' * 68)
print('【4】文件补丁点检查')
import re
def has(path, pat):
    txt = open(path, encoding='utf-8', errors='ignore').read()
    return len(re.findall(pat, txt))
print('  helpers.py  has_mp_menu_entry 定义: %s 处（应 1）' % has('/home/ubuntu/smart-locker/helpers.py', r'def has_mp_menu_entry'))
print('  user.py     _mp_in_direct 出现: %s 处（应 4~5）' % has('/home/ubuntu/smart-locker/routes/user.py', r'_mp_in_direct'))
print('  admin_v2.py 含 mp_in_direct: %s 处（应 3）' % has('/home/ubuntu/smart-locker/routes/admin_v2.py', r'mp_in_direct'))
print('  admin_v2.py 公众号入口直退标记: %s 处（应 1）' % has('/home/ubuntu/smart-locker/routes/admin_v2.py', r'公众号入口直退'))
print('  admin_v2.py 余额不足排队重试: %s 处（应 1）' % has('/home/ubuntu/smart-locker/routes/admin_v2.py', r'余额不足，排队等待重试'))
conn.close()
print()
print('=' * 68)
print('验证结束')
