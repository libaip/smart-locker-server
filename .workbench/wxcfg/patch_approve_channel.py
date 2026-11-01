#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S226 2026-09-15] 修"手动审批跨商户号合并单只退得动一笔" + 部分失败留痕告警 + 已退金额不再翻倍.

根因(老板定位 + 数据核实):
  routes/admin_v2.py /admin/withdrawal/approve 的打包分支里, 对 order_ids 里每一笔订单
  都传了 wd.get('payment_channel_id')(= 提现单上挂的那一笔订单的商户号), 而 helpers.do_real_refund
  是"传进来的商户号优先", 于是订单自己收款的商户号被覆盖 -> 一张提现单里若混了多个商户号的订单,
  非该商户号的订单一律被微信拒退 -> 表现为"审批通过只退了一笔", 剩下的要靠人工去订单管理补退(RF单号).

本补丁改三处:
  1. 循环里每笔订单用【自己的 payment_channel_id + 自己的 order_no】(为空才回退到单上那个);
  2. refund_amount 用 GREATEST 而不是 "+", do_real_refund 内部已经写过一次, 再累加会记成两倍;
  3. 部分失败时写 error_msg + 插 alarms(type=withdraw_refund_failed), 后台红点能看到.
"""
import io, os, shutil, sys, time

ROOT = '/home/ubuntu/smart-locker'
P = 'routes/admin_v2.py'
STAMP = time.strftime('%Y%m%d_%H%M%S')


def rep(old, new, cnt=1, tag=''):
    path = os.path.join(ROOT, P)
    s = io.open(path, encoding='utf-8').read()
    n = s.count(old)
    if n != cnt:
        print('❌ [%s] 匹配 %d 次(期望 %d), 已中止(未写入)' % (tag, n, cnt))
        sys.exit(1)
    io.open(path, 'w', encoding='utf-8').write(s.replace(old, new))
    print('✅ [%s] 替换 %d 处' % (tag, n))


src = os.path.join(ROOT, P)
shutil.copy2(src, src + '.bak_s226_' + STAMP)
print('已备份 %s.bak_s226_%s' % (P, STAMP))

# --- 1) 增加失败信息收集 ---
rep('''            from helpers import do_real_refund
            all_ok = True
            failed_amount = 0.0
            failed_oids = []''',
    '''            from helpers import do_real_refund
            all_ok = True
            failed_amount = 0.0
            failed_oids = []
            failed_msgs = []''', tag='failed_msgs')

# --- 2) 每笔订单用自己收款的商户号 ---
rep('''                c.execute('SELECT deposit_amount, COALESCE(refund_amount,0) as refund_amount FROM orders WHERE id=%s', (oid,))
                od = c.fetchone()
                if od:
                    refund_this = float(od['deposit_amount']) - float(od['refund_amount'])
                    if refund_this > 0.001:
                        ok, rid, rmsg = do_real_refund(order_id=oid, amount=refund_this, payment_channel_id=wd.get('payment_channel_id'))
                        if ok and '已退款' not in rmsg and '全额退款' not in rmsg:
                            c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount=COALESCE(refund_amount,0)+%s WHERE id=%s', (rid, refund_this, oid))
                            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                        elif ok and ('已退款' in rmsg or '全额退款' in rmsg):
                            # do_real_refund 已按成功同步订单状态，这里只算作成功，不恢复余额
                            pass
                        else:
                            all_ok = False
                            failed_amount += refund_this
                            failed_oids.append(oid)''',
    '''                # [S226 2026-09-15] 原来对每一笔都用 wd['payment_channel_id'](= 单上挂的那笔订单的商户号),
                #   一张提现单里混多个商户号时, 非该商户号的订单一律被微信拒退 -> "审批只退得动一笔".
                #   改成每笔订单用【自己收款的商户号】和自己的 order_no, 只有为空才回退到单上那个.
                c.execute('SELECT deposit_amount, COALESCE(refund_amount,0) as refund_amount, payment_channel_id, order_no FROM orders WHERE id=%s', (oid,))
                od = c.fetchone()
                if od:
                    refund_this = float(od['deposit_amount']) - float(od['refund_amount'])
                    if refund_this > 0.001:
                        ok, rid, rmsg = do_real_refund(order_id=oid, order_no=od['order_no'] or '',
                                                       amount=refund_this,
                                                       payment_channel_id=od['payment_channel_id'] or wd.get('payment_channel_id'))
                        if ok and '已退款' not in rmsg and '全额退款' not in rmsg:
                            # [S226] do_real_refund 内部已把 refund_amount 写成本次退款额, 这里不能再用 "+" 累加(否则记成两倍,
                            #   还会导致以后再退这笔时算出"已退够"直接跳过)
                            c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount=GREATEST(COALESCE(refund_amount,0), %s) WHERE id=%s', (rid, refund_this, oid))
                            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                        elif ok and ('已退款' in rmsg or '全额退款' in rmsg):
                            # do_real_refund 已按成功同步订单状态，这里只算作成功，不恢复余额
                            pass
                        else:
                            all_ok = False
                            failed_amount += refund_this
                            failed_oids.append(oid)
                            failed_msgs.append('订单%s(%s元):%s' % (oid, round(refund_this, 2), str(rmsg)[:60]))''',
    tag='per_order_channel')

# --- 3) 部分失败留痕 + 告警 ---
rep('''            c.execute('UPDATE withdrawal_records SET status=%s, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (2 if all_ok else 4, session.get('admin_username', 'admin'), withdrawal_id))''',
    '''            _approver_now = session.get('admin_username', 'admin')
            c.execute('UPDATE withdrawal_records SET status=%s, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (2 if all_ok else 4, _approver_now, withdrawal_id))
            # [S226 2026-09-15] 部分失败必须留痕+告警: 以前只置 status=4, 不写原因也不报警,
            #   后台只显示"异常", 只能靠人工撞见再去订单管理补退
            if not all_ok:
                _err_msg = ('审批部分退款失败(%d笔): %s' % (len(failed_oids), '; '.join(failed_msgs)))[:400]
                c.execute('UPDATE withdrawal_records SET error_msg=%s WHERE id=%s', (_err_msg, withdrawal_id))
                try:
                    c.execute("INSERT INTO alarms (type, device_id, content, status, created_at) VALUES ('withdraw_refund_failed', NULL, %s, '0', NOW())",
                              (('后台审批部分退款失败 单%s 手机%s %s' % (withdrawal_id, phone, _err_msg))[:500],))
                    logger.warning('[withdrawal_approve] 部分退款失败已告警 单=%s orders=%s', withdrawal_id, failed_oids)
                except Exception as _ae:
                    logger.error('[withdrawal_approve] alarm insert fail: %s', _ae)''',
    tag='alarm')

print('🎉 补丁完成')

