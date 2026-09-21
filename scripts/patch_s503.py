# -*- coding: utf-8 -*-
"""
S503-20260921 「直退商户号」通道：
在自动退款批处理 _process_auto_withdrawal_batch 的取数 SQL 里增加一条规则——
  订单通道属于【直退商户号名单】且提现创建时间 >= 生效时间  → 与 auto_approve 网点同等待遇，
  不看网点审批模式、不走通过率，直接原路退款。
名单/生效时间放 system_settings（withdraw_direct_mch_ids / withdraw_direct_since），改完不用重启即可生效
（批量由 cron 每 3 分钟起独立进程执行；进程内调度器需 HUP 重载才拿到新代码）。
只改这一条查询，其它逻辑一字不动。
"""
import hashlib
import py_compile
import shutil
import sys

P = 'routes/admin_v2.py'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()

old = """            WHERE w.status = 0 AND l.withdraw_mode = 'auto_approve'
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
              AND (w.auto_approve_time IS NULL OR w.auto_approve_time::timestamp <= NOW())
              AND (w.next_attempt_at IS NULL OR w.next_attempt_at <= NOW())
            ORDER BY w.id
            LIMIT %s"""

new = """            WHERE w.status = 0
              AND (
                    (l.withdraw_mode = 'auto_approve'
                     AND (w.auto_approve_time IS NULL OR w.auto_approve_time::timestamp <= NOW()))
                 OR (o.payment_channel_id IN (
                        SELECT pc.id FROM payment_channels pc
                        WHERE pc.mch_id = ANY (string_to_array(
                                REPLACE(COALESCE((SELECT setting_value FROM system_settings
                                                  WHERE setting_key = 'withdraw_direct_mch_ids'), ''), ' ', ''), ','))
                     )
                     AND w.created_at >= COALESCE(
                           (SELECT NULLIF(setting_value, '')::timestamp FROM system_settings
                            WHERE setting_key = 'withdraw_direct_since'), NOW()))
              )
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
              AND (w.next_attempt_at IS NULL OR w.next_attempt_at <= NOW())
            ORDER BY w.id
            LIMIT %s"""

n = src.count(old)
assert n == 1, '锚点命中数=%d（应为1，否则中止）' % n
src2 = src.replace(old, new, 1)

shutil.copy2(P, P + '.bak_s503')
open(P, 'w', encoding='utf-8').write(src2)
after = hashlib.md5(src2.encode('utf-8')).hexdigest()

py_compile.compile(P, doraise=True)
print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('withdraw_direct_mch_ids 出现次数 =', src2.count('withdraw_direct_mch_ids'))
print('原 auto_approve 条件仍在 =', "l.withdraw_mode = 'auto_approve'" in src2)
