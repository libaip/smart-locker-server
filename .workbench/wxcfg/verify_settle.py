#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S188 2026-09-15] settle_withdrawal_for_order 回滚级验证(不落库).

在 autocommit=False 的连接里造两条假的合并提现单 -> 调 helper -> 打印结果 -> 整体 ROLLBACK.
"""
import sys, os
sys.path.insert(0, '/home/ubuntu/smart-locker')
os.chdir('/home/ubuntu/smart-locker')
import psycopg2, psycopg2.extras
import helpers

dsn = "postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker"
conn = psycopg2.connect(dsn)
conn.autocommit = False
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

A, B, C = 88888001, 88888002, 88888003
cur.execute("""INSERT INTO withdrawal_records (order_id, user_phone, amount, status, order_ids, dedup_key, created_at)
               VALUES (%s, %s, 40.0, 0, %s, %s, NOW()) RETURNING id""",
            (A, 'TEST_S188', '["%d", "%d"]' % (A, B), 'TEST:S188:1'))
r1 = cur.fetchone()['id']
cur.execute("""INSERT INTO withdrawal_records (order_id, user_phone, amount, status, order_ids, dedup_key, created_at)
               VALUES (%s, %s, 20.0, 0, %s, %s, NOW()) RETURNING id""",
            (C, 'TEST_S188', '["%d"]' % C, 'TEST:S188:2'))
r2 = cur.fetchone()['id']
print("造数: 合并单 id=%s (order_id=%d, order_ids=[%d,%d], 40元) / 单单 id=%s (order_id=%d, 20元)" % (r1, A, A, B, r2, C))

st1 = helpers.settle_withdrawal_for_order(cur, A, 20.0, '测试-投诉自动退款', 'TEST_S188')
print("\n① 对合并单里的订单 %d 结算 20 元 -> %s" % (A, st1))
print("   期望: matched=1 deducted=1 approved=0 inserted=True")
cur.execute("SELECT id, order_id, amount, order_ids, status, approver FROM withdrawal_records WHERE id=%s OR user_phone='TEST_S188' ORDER BY id", (r1,))
for r in cur.fetchall():
    print("   |", dict(r))

st2 = helpers.settle_withdrawal_for_order(cur, C, 20.0, '测试-投诉自动退款', 'TEST_S188')
print("\n② 对单单里的订单 %d 结算 20 元 -> %s" % (C, st2))
print("   期望: matched=1 deducted=0 approved=1 inserted=False")
cur.execute("SELECT id, order_id, amount, order_ids, status, approver FROM withdrawal_records WHERE id=%s OR user_phone='TEST_S188' ORDER BY id", (r2,))
for r in cur.fetchall():
    print("   |", dict(r))

st3 = helpers.settle_withdrawal_for_order(cur, A, 20.0, '测试-投诉自动退款', 'TEST_S188')
print("\n③ 对已结算过的订单 %d 再调一次(幂等验证) -> %s" % (A, st3))
print("   期望: matched=0 deducted=0 approved=0 inserted=False")

conn.rollback()
conn.close()
print("\n✅ 已整体 ROLLBACK, 生产库无残留")

