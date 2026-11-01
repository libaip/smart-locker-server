#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S188 2026-09-15] 修"投诉/客服自动退款只退了一部分、却把整张合并提现单标记为已通过"的问题.

根因: 这两条路径都是 `UPDATE withdrawal_records SET status=2 ... WHERE order_id=%s`,
      而合并提现单的 order_id 只是批次里的第一个订单 => 退了一笔就把整张单标成已通过,
      剩余押金既没退给用户、余额明细又被隐藏(pending), 用户看不到也提不出来.

改法: 与 routes/admin_v2.py 订单退款(S102/S111)一致, 改成"按单逐笔扣减":
      helpers.py 新增 settle_withdrawal_for_order(), 4 处调用点改为调它.
"""
import io, os, shutil, sys, time

ROOT = '/home/ubuntu/smart-locker'
STAMP = time.strftime('%Y%m%d_%H%M%S')
FILES = ('helpers.py', 'routes/user.py', 'routes/admin_v2.py')


def load(p):
    return io.open(os.path.join(ROOT, p), encoding='utf-8').read()


def save(p, s):
    io.open(os.path.join(ROOT, p), 'w', encoding='utf-8').write(s)


def rep(p, old, new, cnt=1, tag=''):
    s = load(p)
    n = s.count(old)
    if n != cnt:
        print('❌ [%s] %s 匹配 %d 次(期望 %d), 已中止未改动该文件' % (tag, p, n, cnt))
        sys.exit(1)
    save(p, s.replace(old, new))
    print('✅ [%s] %s 替换 %d 处' % (tag, p, n))


for f in FILES:
    src = os.path.join(ROOT, f)
    shutil.copy2(src, src + '.bak_s188_' + STAMP)
print('已备份 %d 个文件 (.bak_s188_%s)' % (len(FILES), STAMP))

# ---------------------------------------------------------------- 1) helpers.py
HELPER = '''def settle_withdrawal_for_order(cur, order_id, amount, approver='投诉自动退款', phone=''):
    """[S188 2026-09-15] 投诉/客服自动退款后, 按"逐单扣减"结算包含该订单的提现单.

    背景: 原来这条路径是 `UPDATE withdrawal_records SET status=2 WHERE order_id=%s`,
    只按单个 order_id 匹配, 而"合并提现单"的 order_id 只是批次里的第一个订单;
    于是"只退了一部分却整张单标记已通过", 剩余押金既没退给用户、余额明细又被隐藏(pending),
    用户看不到也提不出来. 现在改成与 routes/admin_v2.py 订单退款(S102/S111)一致的做法:
      - 从所有待处理(status 0/1)且包含该订单的单里移除该订单并扣减金额;
      - 扣减后还有别的订单 -> 保持待处理, 只更新金额/订单列表(若移除的正好是单里的 order_id, 换成剩余第一个);
      - 全部订单都退完 -> 置为已通过;
      - 并补一条该订单自己的已通过记录, 保证用户提现记录里有这笔退款流水.
    幂等: 重复调用不会重复扣减、也不会重复补记录. 不提交事务, 由调用方 commit.
    返回统计 dict 供日志使用.
    """
    import json as _json_s
    stat = {'matched': 0, 'deducted': 0, 'approved': 0, 'inserted': False}
    try:
        oid = int(order_id)
    except Exception:
        return stat
    amt = float(amount or 0)

    def _g(row, key, idx):
        try:
            if hasattr(row, 'get'):
                return row.get(key)
            return row[idx]
        except Exception:
            return None

    if not phone:
        try:
            cur.execute('SELECT user_phone FROM orders WHERE id=%s', (oid,))
            _r = cur.fetchone()
            if _r:
                phone = _g(_r, 'user_phone', 0) or ''
        except Exception:
            phone = ''

    # 1) 包含该订单的待处理提现单
    cur.execute("""SELECT id, amount, order_ids, order_id FROM withdrawal_records
                   WHERE status IN (0,1)
                     AND (order_id=%s OR order_ids::jsonb @> %s::jsonb OR order_ids::jsonb @> %s::jsonb)
                   ORDER BY id""",
                (oid, _json_s.dumps([oid]), _json_s.dumps([str(oid)])))
    for row in cur.fetchall():
        _id = _g(row, 'id', 0)
        _amt = float(_g(row, 'amount', 1) or 0)
        _oids_raw = _g(row, 'order_ids', 2) or '[]'
        _cur_oid = _g(row, 'order_id', 3)
        try:
            _oids = _json_s.loads(_oids_raw)
        except Exception:
            _oids = []
        stat['matched'] += 1
        _before = len(_oids)
        _oids = [x for x in _oids if str(x) != str(oid)]
        _removed = len(_oids) < _before
        _new_amt = max(0.0, _amt - amt) if _removed else _amt
        if _oids:
            _next_oid = _cur_oid
            if str(_cur_oid) == str(oid):
                try:
                    _next_oid = int(_oids[0])
                except Exception:
                    _next_oid = _oids[0]
            cur.execute("""UPDATE withdrawal_records SET amount=%s, order_ids=%s, order_id=%s,
                           error_msg=NULL, retry_count=0 WHERE id=%s""",
                        (round(_new_amt, 2), _json_s.dumps(_oids), _next_oid, _id))
            stat['deducted'] += 1
        else:
            cur.execute("""UPDATE withdrawal_records SET status=2, amount=%s, order_ids=%s,
                           approver=%s, approve_time=CURRENT_TIMESTAMP, error_msg=NULL WHERE id=%s""",
                        (round(_new_amt, 2), _json_s.dumps([]), approver, _id))
            stat['approved'] += 1

    # 2) 补一条该订单自己的已通过记录(保证用户提现记录里能看到这笔退款)
    cur.execute('SELECT 1 FROM withdrawal_records WHERE order_id=%s LIMIT 1', (oid,))
    if not cur.fetchone():
        try:
            cur.execute("""INSERT INTO withdrawal_records
                           (order_id, user_phone, amount, status, approver, order_ids, approve_time, error_msg, dedup_key, created_at)
                           VALUES (%s, %s, %s, 2, %s, %s, CURRENT_TIMESTAMP, %s, %s, CURRENT_TIMESTAMP)
                           ON CONFLICT DO NOTHING""",
                        (oid, phone, round(amt, 2), approver, _json_s.dumps([oid]),
                         '%s(原提现单已联动扣减)' % approver, 'C:%s:%s' % (phone, oid)))
            stat['inserted'] = True
        except Exception as _ie:
            logger.warning('[settle_withdrawal] 补建提现记录失败 order_id=%s err=%s', oid, _ie)
    return stat


'''
rep('helpers.py', 'def do_balance_transfer(phone, amount, openid=None, user_id=0):',
    HELPER + 'def do_balance_transfer(phone, amount, openid=None, user_id=0):', tag='helpers')

# ---------------------------------------------------------------- 2) routes/user.py
rep('routes/user.py',
    "            cur.execute(\"UPDATE withdrawal_records SET status=2, approver='投诉自动退款', approve_time=CURRENT_TIMESTAMP WHERE order_id=%s AND status='0'\", (order[0],))",
    """            # [S188 2026-09-15] 原来是按单个 order_id 把整张提现单置为已通过 -> 合并提现单只退了一部分却整张标通过,
            #   剩余押金被隐藏(用户看不到也提不出). 改为逐单扣减(与 admin_v2 订单退款 S102/S111 一致)
            try:
                from helpers import settle_withdrawal_for_order
                logger.info('[self_complaint] 提现单结算 %s order_id=%s',
                            settle_withdrawal_for_order(cur, order[0], float(order[2] or 0), '投诉自动退款'), order[0])
            except Exception as _st_e:
                logger.warning('[self_complaint] 提现单结算失败 order_id=%s err=%s', order[0], _st_e)""",
    tag='user.py')

# ---------------------------------------------------------------- 3) admin_v2.py 三处
# 3a. _auto_refund_complaint_order 里的"直接置为已通过"
rep('routes/admin_v2.py',
    '''                # 已有待处理/处理中的提现记录 -> 直接置为已通过(投诉退款已完成)
                c.execute("UPDATE withdrawal_records SET status=2, approver='投诉自动退款', approve_time=CURRENT_TIMESTAMP WHERE order_id=%s AND status IN (0,1)", (order_id,))''',
    '''                # [S188 2026-09-15] 原来按单个 order_id 把整张提现单置为已通过 -> 合并提现单只退了一部分却整张标通过,
                #   剩余押金被隐藏(用户看不到也提不出). 改为逐单扣减, 全退完才置通过(与订单退款 S102/S111 一致)
                from helpers import settle_withdrawal_for_order
                logger.info('[auto_refund_complaint] 提现单结算 %s order_id=%s',
                            settle_withdrawal_for_order(c, order_id, refund_amount, '投诉自动退款', _wr_phone), order_id)''',
    tag='admin_v2/7674')

# 3b. 调度器: 订单已退款分支
rep('routes/admin_v2.py',
    '''                                _wa2_cur.execute("""UPDATE withdrawal_records SET status=2, approver='投诉自动退款', approve_time=CURRENT_TIMESTAMP WHERE order_id=(SELECT id FROM orders WHERE order_no=%s LIMIT 1) AND status='0'""", (ono2,))''',
    '''                                # [S188 2026-09-15] 改为逐单扣减(原来整张标已通过, 会吞掉同单其它订单的押金)
                                from helpers import settle_withdrawal_for_order
                                _wa2_cur.execute("SELECT id, deposit_amount, user_phone FROM orders WHERE order_no=%s ORDER BY id DESC LIMIT 1", (ono2,))
                                _sr2 = _wa2_cur.fetchone()
                                if _sr2:
                                    _sid2 = _sr2[0] if not hasattr(_sr2, 'get') else _sr2.get('id')
                                    _samt2 = float((_sr2[1] if not hasattr(_sr2, 'get') else _sr2.get('deposit_amount')) or 0)
                                    _sph2 = (_sr2[2] if not hasattr(_sr2, 'get') else _sr2.get('user_phone')) or ''
                                    logger.info("[complaint_scheduler] 提现单结算(订单已退) %s order=%s",
                                                settle_withdrawal_for_order(_wa2_cur, _sid2, _samt2, '投诉自动退款', _sph2), ono2)''',
    tag='admin_v2/8749')

# 3c. 调度器: 退款成功后置通过
rep('routes/admin_v2.py',
    '''                                _wa_cur.execute("""UPDATE withdrawal_records SET status=2, approver='投诉自动退款', approve_time=CURRENT_TIMESTAMP WHERE order_id=(SELECT id FROM orders WHERE order_no=%s LIMIT 1) AND status='0'""", (ono2,))
                                _wa_conn.commit()
                                _wa_conn.close()
                                logger.info("[complaint_scheduler] pending withdrawal auto-approved order=%s id=%s", ono2, cid2)''',
    '''                                # [S188 2026-09-15] 改为逐单扣减(幂等, _auto_refund_complaint_order 已结算过则空转)
                                from helpers import settle_withdrawal_for_order
                                _wa_cur.execute("SELECT id, deposit_amount, user_phone FROM orders WHERE order_no=%s ORDER BY id DESC LIMIT 1", (ono2,))
                                _sr3 = _wa_cur.fetchone()
                                if _sr3:
                                    _sid3 = _sr3[0] if not hasattr(_sr3, 'get') else _sr3.get('id')
                                    _samt3 = float((_sr3[1] if not hasattr(_sr3, 'get') else _sr3.get('deposit_amount')) or 0)
                                    _sph3 = (_sr3[2] if not hasattr(_sr3, 'get') else _sr3.get('user_phone')) or ''
                                    logger.info("[complaint_scheduler] 提现单结算 %s order=%s id=%s",
                                                settle_withdrawal_for_order(_wa_cur, _sid3, _samt3, '投诉自动退款', _sph3), ono2, cid2)
                                _wa_conn.commit()
                                _wa_conn.close()''',
    tag='admin_v2/8807')

print('🎉 补丁 1 全部应用完成')

