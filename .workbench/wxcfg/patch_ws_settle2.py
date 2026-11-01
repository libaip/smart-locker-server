#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S188 2026-09-15] 补丁 1b: settle_withdrawal_for_order 里把 order_ids::jsonb 强转换掉.

原因: get_db() 是 autocommit=True, 单条语句报错不会污染事务, 但 order_ids 若存在非 JSON 脏数据,
      ::jsonb 强转会抛异常到调用方(投诉退款流程里). 改成文本 LIKE 预筛 + Python 精确判断, 零风险.
"""
import io, os, shutil, sys, time

ROOT = '/home/ubuntu/smart-locker'
P = 'helpers.py'
STAMP = time.strftime('%Y%m%d_%H%M%S')


def rep(old, new, cnt=1, tag=''):
    p = os.path.join(ROOT, P)
    s = io.open(p, encoding='utf-8').read()
    n = s.count(old)
    if n != cnt:
        print('❌ [%s] 匹配 %d 次(期望 %d), 中止' % (tag, n, cnt))
        sys.exit(1)
    io.open(p, 'w', encoding='utf-8').write(s.replace(old, new))
    print('✅ [%s] 替换 %d 处' % (tag, n))


src = os.path.join(ROOT, P)
shutil.copy2(src, src + '.bak_s188b_' + STAMP)
print('已备份 %s.bak_s188b_%s' % (P, STAMP))

rep('''    cur.execute("""SELECT id, amount, order_ids, order_id FROM withdrawal_records
                   WHERE status IN (0,1)
                     AND (order_id=%s OR order_ids::jsonb @> %s::jsonb OR order_ids::jsonb @> %s::jsonb)
                   ORDER BY id""",
                (oid, _json_s.dumps([oid]), _json_s.dumps([str(oid)])))''',
    '''    # 用文本 LIKE 预筛(避免 order_ids 里若有非 JSON 脏数据时 ::jsonb 强转报错), 命中后再由 Python 精确判断
    cur.execute("""SELECT id, amount, order_ids, order_id FROM withdrawal_records
                   WHERE status IN (0,1) AND (order_id=%s OR order_ids LIKE %s)
                   ORDER BY id""",
                (oid, '%' + str(oid) + '%'))''',
    tag='select')

rep('''        stat['matched'] += 1
        _before = len(_oids)
        _oids = [x for x in _oids if str(x) != str(oid)]
        _removed = len(_oids) < _before
        _new_amt = max(0.0, _amt - amt) if _removed else _amt''',
    '''        _contains = str(oid) in [str(x) for x in _oids]
        _is_own = (str(_cur_oid) == str(oid))
        if not (_contains or _is_own):
            continue          # LIKE 预筛的误命中, 跳过
        stat['matched'] += 1
        if _contains:
            _oids = [x for x in _oids if str(x) != str(oid)]
            _new_amt = max(0.0, _amt - amt)
        else:
            _new_amt = _amt''',
    tag='loop')

print('🎉 补丁 1b 完成')

