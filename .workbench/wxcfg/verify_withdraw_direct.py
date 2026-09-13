# -*- coding: utf-8 -*-
"""[提现直退] 106 上验证：判定函数 + 余额不足留队重试 + 成功后标记（用打桩，不动真钱）"""
import sys, json
sys.path.insert(0, '/home/ubuntu/smart-locker')
import psycopg2
import wx_config as C
DSN = 'postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker'
C.bind(lambda: psycopg2.connect(DSN)); C.clear_cache()
import helpers
import routes.admin_v2 as A2

conn = psycopg2.connect(DSN); cur = conn.cursor()

print('=' * 68)
print('【1】判定函数 has_mp_menu_entry')
cur.execute("select phone, count(*) from wx_oa_messages where event='view_miniprogram' and coalesce(phone,'')<>'' group by 1 order by 2 desc limit 1")
r = cur.fetchone()
if r:
    print('  有菜单记录的手机号 %s（%d 次点击）-> %s （应 True）' % (r[0], r[1], helpers.has_mp_menu_entry(phone=r[0])))
print('  随便一个号 13900000000 -> %s （应 False）' % helpers.has_mp_menu_entry(phone='13900000000'))
print('  空参数 -> %s （应 False）' % helpers.has_mp_menu_entry())

print()
print('=' * 68)
print('【2】造一条测试提现记录（approver=mp_in_direct），走打桩退款，不动真钱')
pending = cur.execute("select count(*) from withdrawal_records where status=0 and approver in ('whitelist_auto','mp_in_direct')") or cur.fetchone()[0]
print('  当前待处理的必退记录数: %s' % pending)
cur.execute("""select o.id, o.order_no, o.user_phone, o.deposit_amount, coalesce(o.refund_amount,0),
                      o.status, coalesce(o.refund_status,''), coalesce(o.refund_id,'')
               from orders o
               where coalesce(o.refund_status,'') <> 'refunded' and coalesce(o.transaction_id,'') <> ''
                 and o.deposit_amount - coalesce(o.refund_amount,0) > 0
               order by o.id desc limit 1""")
row = cur.fetchone()
if not row:
    print('  ❌ 106 库里没有可用的订单，跳过'); raise SystemExit
oid, ono, phone, dep, ra, o_st, o_rs, o_rid = row
amt = round(float(dep) - float(ra), 2)
cur.execute("""insert into withdrawal_records (order_id, user_phone, amount, status, click_count, approver, auto_approve_time, dedup_key, order_ids)
               values (%s,%s,%s,0,1,'mp_in_direct',NOW(),%s,%s) returning id""",
            (oid, phone, amt, 'TESTMPIN:%s' % oid, json.dumps([oid])))
wid = cur.fetchone()[0]
conn.commit()
print('  测试记录 id=%s（订单 %s 手机 %s 金额 %s）' % (wid, ono, phone, amt))

try:
    print()
    print('【3】模拟"余额不足" -> 应留队重试（status 仍为 0、有错误信息、设了下次重试时间）')
    helpers.do_real_refund = lambda **kw: (False, '', '基本账户余额不足，请充值后重新发起')
    A2._run_withdrawal_batch_auto()
    cur.execute("select status, coalesce(error_msg,''), coalesce(next_attempt_at::text,'') from withdrawal_records where id=%s", (wid,))
    st, em, na = cur.fetchone()
    ok3 = (st == 0 and '余额不足' in em)
    print('  status=%s  error_msg=%r  下次重试=%s  -> %s' % (st, em[:40], na or '(空)', '✅ 留队' if ok3 else '❌'))

    print()
    print('【4】10 分钟没到就再跑一轮（打桩成"能退成功"）-> 应该不被捞起（排队闸门生效）')
    helpers.do_real_refund = lambda **kw: (True, 'TEST_REFUND_ID', 'ok')
    A2._run_withdrawal_batch_auto()
    cur.execute("select status, coalesce(approver,'') from withdrawal_records where id=%s", (wid,))
    st_b, apv_b = cur.fetchone()
    ok4 = (st_b == 0)
    print('  status=%s  approver=%r  -> %s' % (st_b, apv_b, '✅ 没被捞起' if ok4 else '❌ 被提前捞起来退了'))

    print()
    print('【5】把"下次重试时间"拨到过去，再跑一轮 -> 应完成且审批人=公众号入口直退')
    cur.execute("update withdrawal_records set next_attempt_at = NOW() - INTERVAL '1 minute' where id=%s", (wid,))
    conn.commit()
    A2._run_withdrawal_batch_auto()
    cur.execute("select status, coalesce(approver,'') from withdrawal_records where id=%s", (wid,))
    st2, apv2 = cur.fetchone()
    ok5 = (st2 == 2 and apv2 == '公众号入口直退')
    print('  status=%s  approver=%r  -> %s' % (st2, apv2, '✅' if ok5 else '❌'))
finally:
    print()
    print('【6】清理测试痕迹（删测试提现记录 + 还原订单状态）')
    cur.execute("delete from withdrawal_records where id=%s", (wid,))
    cur.execute("update orders set status=%s, refund_status=%s, refund_id=%s where id=%s", (o_st, o_rs, o_rid, oid))
    conn.commit()
    print('  已删除测试记录、订单 %s 已还原为 status=%s refund_status=%s' % (ono, o_st, o_rs or '(空)'))
conn.close()
print()
print('=' * 68)
print('验证结束（第 3、4、5 项都 ✅ 才算通过）')
