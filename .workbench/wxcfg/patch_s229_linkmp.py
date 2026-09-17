#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S229-20260917] routes/user.py: 把"用户真的进了小程序"记进 mp_enter_log

背景（2026-09-17 实测）：本接口(/user/link-mp-openid)由小程序 subscribe 页 onLoad 调用并带订单号，
即"小程序页面一打开"就会到服务器（当天 1,943 次）。但 H5 判"他进没进小程序"只看 mp_enter_log，
而那张表原先只有用户在小程序里点「返回网页」时才由 /user/mp-exit-log 写入 —— 于是
"进去了、但用 ✕/返回键关掉小程序"的用户在 H5 眼里等于"没进过"→ 被弹回设置页 + 挂提示，
反复几次后只能点"用网页支付"逃生（当天 8 个逃生用户里 7 个都有本接口记录，最典型
19812040456：12:39:00 进小程序、12:39:37 被判"没进过"→ 逃生）。

改法：在本接口落库阶段补一条 phase='mp_page' 记录（用独立连接，失败不影响主流程），
H5 的 /api/user/mp-entered 只查"有无记录"，于是立刻认账。
不影响既有统计口径（统计脚本按 phase='attempt' 计数）。
"""
import io
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'routes/user.py'

OLD = """            else:
                cursor.execute("UPDATE users SET nickname = %s WHERE phone = %s AND (nickname IS NULL OR nickname = chr(39)||chr(39))", (nickname, phone))

        
        conn.commit()
"""

NEW = """            else:
                cursor.execute("UPDATE users SET nickname = %s WHERE phone = %s AND (nickname IS NULL OR nickname = chr(39)||chr(39))", (nickname, phone))

        # ===== [S229-20260917] 把"用户真的进了小程序"记进 mp_enter_log =====
        # 本接口是小程序 subscribe 页 onLoad 调的(带 order_id)，等于"小程序页面一打开"就到这。
        # H5 判"进没进小程序"只看 mp_enter_log，而那张表原先只有点「返回网页」时才有记录 ->
        # "进去了却用 叉/返回键关掉小程序"的人被判成"没进过"而弹回设置页(当天 8 个逃生用户里 7 个如此)。
        # 补一条 phase='mp_page'：H5 的 /api/user/mp-entered 只查有无记录，立刻认账。
        # 用独立连接写，避免污染本接口主事务；失败只告警，不影响绑定主流程。
        try:
            if order_id or phone:
                _mep_conn = get_db()
                _mep_cur = _mep_conn.cursor()
                _mep_cur.execute(
                    "INSERT INTO mp_enter_log (order_id, phone, phase) VALUES (%s, %s, %s)",
                    (str(order_id or '')[:40], str(phone or '')[:20], 'mp_page'))
                _mep_conn.commit()
                _mep_conn.close()
        except Exception as _mep_e:
            logger.warning('[link_mp_openid] mp_enter_log 落库失败(不影响主流程): %s' % _mep_e)

        conn.commit()
"""


def main():
    s = io.open(P, encoding='utf-8').read()
    n = s.count(OLD)
    if n != 1:
        print('[FAIL] 原文出现 %d 次（要求恰好 1 次），未写文件' % n)
        sys.exit(2)
    s2 = s.replace(OLD, NEW, 1)
    for must in ["'mp_page'", 'INSERT INTO mp_enter_log']:
        if must not in s2:
            print('[FAIL] 缺少标记: %s' % must)
            sys.exit(3)
    io.open(P, 'w', encoding='utf-8', newline='').write(s2)
    print('[DONE] 已写入 %s：%d -> %d 字符 (+%d)' % (P, len(s), len(s2), len(s2) - len(s)))


if __name__ == '__main__':
    main()
