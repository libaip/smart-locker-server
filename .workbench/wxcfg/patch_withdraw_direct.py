# -*- coding: utf-8 -*-
"""
[提现直退-20260914] 从公众号点菜单进过小程序的用户 -> 提现免审直退

老板定的规则（2026-09-14 01:20）：
  - 判定：该用户有过"从公众号点菜单进小程序"的记录（view_miniprogram 事件）
    （因为账单/搜索/分享等入口微信不给标识，唯一能识别的入口动作就是点菜单）
  - 放行：免审、由批量任务原路立即退款
  - 不限单笔金额、不限每日次数（老板明确说不限制）
  - 余额不足：像审批队列那样"排队等"，下次批量再试（不拒绝、不转人工）
  - 不生成投诉工单（加直退旁路）

实测影响面：近 30 天提现用户里 2,916 人（8.4%）、4,218 笔、89,153 元。

改动 3 个文件：
  1) helpers.py        新增 has_mp_menu_entry() 判定函数
  2) routes/user.py    用户提现时：命中判定 -> auto_approve_time=now + approver='mp_in_direct'
  3) routes/admin_v2.py 批量任务：把 mp_in_direct 并入"必退"分支；余额不足则留队重试
"""
import os
import sys
import time
import shutil
import py_compile

APP = '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
BK = os.path.join(APP, 'backups', 'mplin_' + time.strftime('%Y%m%d_%H%M%S'))
HLP = APP + '/helpers.py'
USR = APP + '/routes/user.py'
ADM = APP + '/routes/admin_v2.py'

HELPER = '''def has_mp_menu_entry(phone='', openid='', unionid=''):
    """[提现直退-20260914] 这个用户是不是"从公众号点菜单进过小程序"。

    老板定的提现免审直退条件。为什么用这个条件：从微信账单/搜索/分享/支付后提示
    进公众号的用户，微信报文里【没有任何来源标识】，无法区分；唯一能识别的入口动作
    就是"点公众号菜单跳小程序"(view_miniprogram 事件 + MenuId)。
    所以规则=有点过菜单记录 -> 提现免审直退（分不出是哪条路来的，就一律按"从公众号来的"放行）。

    数据来源 wx_oa_messages（公众号收到的全部记录；phone 列是按 unionid 反查出来的手机号）。
    查不到/报错一律返回 False（钱的事，判不出来就不放行）。
    """
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cond, params = [], []
        if phone:
            cond.append('phone = %s')
            params.append(str(phone))
        if openid:
            cond.append('openid = %s')
            params.append(openid)
        if not cond:
            conn.close()
            return False
        cur.execute("SELECT 1 FROM wx_oa_messages WHERE event = 'view_miniprogram' AND (%s) LIMIT 1"
                    % ' OR '.join(cond), tuple(params))
        r = cur.fetchone()
        conn.close()
        return bool(r)
    except Exception as e:
        logger.warning('[mp_menu_entry] 查询失败(按不放行处理): %s' % e)
        return False


def check_withdraw_auto_approve(openid=None, phone=None, user_id=0):'''

E = []
E.append((HLP, '新增判定函数', 'def check_withdraw_auto_approve(openid=None, phone=None, user_id=0):', HELPER, 1))

E.append((USR, '提现时打直退标记',
          """            if wl_record:
                _auto_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            plan_ids = []""",
          """            if wl_record:
                _auto_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # [提现直退-20260914] 从公众号点菜单进过小程序的用户 -> 免审直退（老板定；不限金额不限次数；
            #   余额不足则由批量任务留队重试，不拒绝）。套路和上面白名单一致：把 auto_approve_time 置为
            #   现在 + 打 approver 标记，由 run_withdrawal_batch.py 的"必退"分支原路退款。
            _mp_in_direct = False
            try:
                from helpers import has_mp_menu_entry
                _mp_in_direct = bool(has_mp_menu_entry(phone=phone, openid=openid,
                                                       unionid=ident.get('unionid') or ''))
            except Exception as _e_mi:
                logger.warning(f'[提现直退] 判定失败，按普通流程: {_e_mi}')
                _mp_in_direct = False
            if _mp_in_direct and not wl_record:
                _auto_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            plan_ids = []""", 1))

E.append((USR, '提现记录写入标记',
          '''            cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, openid, auto_approve_time, dedup_key, order_ids) VALUES (%s, %s, %s, 0, 1, %s, %s, %s, %s) RETURNING id",
                           (int(plan_ids[0]), phone, actual_amount, order_openid, _auto_time, dedup_key, _json_auto.dumps(plan_ids)))''',
          '''            cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, click_count, openid, auto_approve_time, dedup_key, order_ids, approver) VALUES (%s, %s, %s, 0, 1, %s, %s, %s, %s, %s) RETURNING id",
                           (int(plan_ids[0]), phone, actual_amount, order_openid, _auto_time, dedup_key, _json_auto.dumps(plan_ids),
                            ('mp_in_direct' if _mp_in_direct else None)))''', 1))

E.append((ADM, '批量任务把直退并进必退分支',
          """            SELECT w.id, w.user_phone, w.amount, w.order_id, w.order_ids
            FROM withdrawal_records w
            WHERE w.status = 0 AND w.approver = 'whitelist_auto'
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
            LIMIT 200""",
          """            SELECT w.id, w.user_phone, w.amount, w.order_id, w.order_ids, w.approver
            FROM withdrawal_records w
            WHERE w.status = 0 AND w.approver IN ('whitelist_auto', 'mp_in_direct')
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
              AND (w.next_attempt_at IS NULL OR w.next_attempt_at <= NOW())
            LIMIT 200""", 1))

E.append((ADM, '每行取审批标记',
          """        for rw in rows_wl:
            _wid = rw['id']
            _w_phone = rw['user_phone'] or ''""",
          """        for rw in rows_wl:
            _wid = rw['id']
            _w_phone = rw['user_phone'] or ''
            # [提现直退-20260914] 公众号入口直退 / 白名单，审计上要分得清
            _ap_label = '公众号入口直退' if (rw.get('approver') == 'mp_in_direct') else '白名单'""", 1))

E.append((ADM, '成功时写对应审批人',
          """                c.execute("UPDATE withdrawal_records SET status=2, approve_time=NOW(), approver='白名单' WHERE id=%s", (_wid,))""",
          """                c.execute("UPDATE withdrawal_records SET status=2, approve_time=NOW(), approver=%s WHERE id=%s", (_ap_label, _wid))""", 1))

E.append((ADM, '余额不足则排队重试',
          """                _reject_msg = '白名单退款失败，余额已隐藏待人工处理'
                c.execute("UPDATE withdrawal_records SET status=3, error_msg=%s, dedup_key=NULL, next_attempt_at=NULL, approve_time=NOW(), approver='白名单' WHERE id=%s", ((_reject_msg + '|' + str(_first_msg or ''))[:500], _wid))""",
          """                # [提现直退-20260914] 老板指定：公众号入口直退的单子，余额不足就"排队等"
                #   （像审批队列那样，不拒绝、不转人工），10 分钟后由下一轮批量自动重试
                _low_bal = ('余额不足' in str(_first_msg or '')) or ('NOTENOUGH' in str(_first_msg or '').upper())
                if rw.get('approver') == 'mp_in_direct' and _low_bal:
                    c.execute("UPDATE withdrawal_records SET error_msg=%s, next_attempt_at=NOW() + INTERVAL '10 minutes' WHERE id=%s",
                              (('余额不足，排队等待重试|' + str(_first_msg or ''))[:500], _wid))
                    continue
                _reject_msg = '白名单退款失败，余额已隐藏待人工处理'
                c.execute("UPDATE withdrawal_records SET status=3, error_msg=%s, dedup_key=NULL, next_attempt_at=NULL, approve_time=NOW(), approver=%s WHERE id=%s", ((_reject_msg + '|' + str(_first_msg or ''))[:500], _ap_label, _wid))""", 1))


def main():
    print('=' * 72)
    print('[提现直退] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    text = {p: open(p, encoding='utf-8').read() for p in (HLP, USR, ADM)}
    news = dict(text)
    for path, tag, old, new, cnt in E:
        n = news[path].count(old)
        print('  %s %-28s %-18s 命中 %d/%d' % ('✓' if n == cnt else '✗', tag, os.path.basename(path), n, cnt))
        if n != cnt:
            raise SystemExit('[中止] %s 锚点数量不对(%d/%d)，一个文件都不写' % (tag, n, cnt))
        news[path] = news[path].replace(old, new, 1)
    if not REAL:
        print('\n干跑结束：没有写文件。')
        return 0
    os.makedirs(BK, exist_ok=True)
    tmps = []
    try:
        for path in (HLP, USR, ADM):
            tmp = path + '.mplin_tmp.py'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(news[path])
            py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
            os.remove(tmp + 'c')
            tmps.append((path, tmp))
        print('\n三个文件语法检查通过')
    except Exception as e:
        for _, tmp in tmps:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise SystemExit('[中止] 语法检查失败，一个文件都没写：%s' % e)
    for path, tmp in tmps:
        shutil.copy2(path, os.path.join(BK, os.path.basename(path)))
        os.replace(tmp, path)
        print('  已写入 %s' % os.path.basename(path))
    print('备份: %s' % BK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
