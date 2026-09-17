#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S230-20260917] routes/user.py: 把 S229 那条 mp_enter_log 记录从"新开连接"改成"复用已有连接"

原因：S229 用 get_db() 另开一个连接写（每天约 1,900 次），重载后曾出现一批"连接池取到坏连接"的抖动
（14:17-15:45 共 24 行，落在投诉调度器/提现批处理上，15:45 后归零）。虽然不像是主因，但去掉这条多余连接更干净。

技术依据（已实测）：database.py:178 把底层连接设为 autocommit=True，所以
  - 每一条 execute 自成事务，写失败不会污染后续语句（不需要 SAVEPOINT；实测 SAVEPOINT 在此也不可用：
    "SAVEPOINT can only be used in transaction blocks"）；
  - get_db() 在 Flask 请求内是复用同一个连接(flask.g)，直接用它即可。
"""
import io
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'routes/user.py'

OLD = """        # 用独立连接写，避免污染本接口主事务；失败只告警，不影响绑定主流程。
        try:
            if order_id or phone:
                _mep_conn = get_db()
                _mep_cur = _mep_conn.cursor()
                _mep_cur.execute(
                    "INSERT INTO mp_enter_log (order_id, phone, phase) VALUES (%s, %s, %s)",
                    (str(order_id or '')[:40], str(phone or '')[:20], 'mp_page'))
                _mep_conn.commit()
                _mep_conn.close()
"""

NEW = """        # [S230] 改为复用本接口已有的连接：database.py 的连接是 autocommit=True（每条语句独立提交），
        #        写失败只打一条告警、不会污染后面的语句；原来每次新开一个连接（每天约 1900 次）。
        try:
            if order_id or phone:
                cursor.execute(
                    "INSERT INTO mp_enter_log (order_id, phone, phase) VALUES (%s, %s, %s)",
                    (str(order_id or '')[:40], str(phone or '')[:20], 'mp_page'))
"""


def main():
    s = io.open(P, encoding='utf-8').read()
    n = s.count(OLD)
    if n != 1:
        print('[FAIL] 原文出现 %d 次（要求恰好 1 次），未写文件' % n)
        sys.exit(2)
    s2 = s.replace(OLD, NEW, 1)
    if '_mep_conn' in s2 or '_mep_cur' in s2:
        print('[FAIL] 仍有 _mep_conn/_mep_cur 残留')
        sys.exit(3)
    if "'mp_page'" not in s2:
        print('[FAIL] mp_page 标记丢了')
        sys.exit(4)
    io.open(P, 'w', encoding='utf-8', newline='').write(s2)
    print('[DONE] 已写入 %s：%d -> %d 字符 (%+d)' % (P, len(s), len(s2), len(s2) - len(s)))


if __name__ == '__main__':
    main()
