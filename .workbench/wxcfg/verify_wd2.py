# -*- coding: utf-8 -*-
"""[提现直退] 复现/回归：直退的行在"余额不足留队"后，会不会被普通队列分支抢走？

做法（106 上，全程打桩 do_real_refund，不动真钱）：
  1) 挑一个【queue_approve 网点】的真实可退订单
  2) 造一条 approver='mp_in_direct'、status=0、auto_approve_time=现在的提现记录
     （这正是 routes/user.py 里直退命中后写出来的样子）
  3) 把 do_real_refund 打桩成"余额不足"，跑一轮批量任务
  4) 期望：仍留在队列(status=0 + error_msg='余额不足，排队等待重试|...')
     若被第 1/2 段普通队列抢走 -> 会变成 status=3('自动拒绝'/'队列退款失败...') 或 status=2
  5) 清理：删测试行 + 还原订单
"""
import sys, json
sys.path.insert(0, '/home/ubuntu/smart-locker')
import psycopg2
import wx_config as C
DSN = 'postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker'
C.bind(lambda: psycopg2.connect(DSN)); C.clear_cache()
import helpers
import routes.admin_v2 as A2

conn = psycopg2.connect(DSN); cur = conn.cursor()
cur.execute("""
    select o.id, o.order_no, o.user_phone, o.deposit_amount - coalesce(o.refund_amount,0) as can_refund,
           l.withdraw_mode, o.status, coalesce(o.refund_status,''), coalesce(o.refund_id,'')
    from orders o join cabinets cb on o.cabinet_id = cb.id join locations l on cb.location_id = l.id
    where coalesce(o.refund_status,'') <> 'refunded' and coalesce(o.transaction_id,'') <> ''
      and o.deposit_amount - coalesce(o.refund_amount,0) > 0 and l.withdraw_mode = 'queue_approve'
    order by o.id desc limit 1
""")
row = cur.fetchone()
if not row:
    print('  ❌ 106 上没有 queue_approve 网点的可退订单，无法复现'); raise SystemExit
oid, ono, phone, amt, mode, o_st, o_rs, o_rid = row
amt = round(float(amt), 2)
print('  用的订单: id=%s %s 手机=%s 可退=%s 网点模式=%s' % (oid, ono, phone, amt, mode))

cur.execute("""insert into withdrawal_records
    (order_id, user_phone, amount, status, click_count, approver, auto_approve_time, dedup_key, order_ids)
    values (%s,%s,%s,0,1,'mp_in_direct',NOW(),%s,%s) returning id""",
    (oid, phone, amt, 'TESTMPIN2:%s' % oid, json.dumps([oid])))
wid = cur.fetchone()[0]
conn.commit()
print('  测试提现记录 id=%s（approver=mp_in_direct, status=0, auto_approve_time=现在）' % wid)

try:
    helpers.do_real_refund = lambda **kw: (False, '', '基本账户余额不足，请充值后重新发起')
    A2._run_withdrawal_batch_auto()
    cur.execute("select status, coalesce(approver,'(空)'), coalesce(error_msg,''), coalesce(next_attempt_at::text,'') from withdrawal_records where id=%s", (wid,))
    st, ap, em, na = cur.fetchone()
    print()
    print('  跑完一轮批量任务后:')
    print('    status=%s  approver=%s' % (st, ap))
    print('    error_msg=%r' % em[:70])
    print('    下次重试=%s' % (na or '(空)'))
    ok = (st == 0 and '余额不足，排队等待重试' in em and ap == 'mp_in_direct')
    print()
    if ok:
        print('  ✅ 通过：直退的单子老老实实排队等余额，没被普通队列（按通过率）抢走')
    else:
        print('  ❌ 不通过：直退的单子被别的分支处理了（status=%s approver=%s）—— 正是要修的问题' % (st, ap))
finally:
    cur.execute("delete from withdrawal_records where id=%s", (wid,))
    cur.execute("update orders set status=%s, refund_status=%s, refund_id=%s where id=%s", (o_st, o_rs, o_rid, oid))
    conn.commit()
    print('  已清理测试行、订单 %s 已还原(status=%s)' % (ono, o_st))
conn.close()
