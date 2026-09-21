# -*- coding: utf-8 -*-
"""[S521-20260921] 身份对账脚本（只读，不写任何业务数据）

用途：改了"按 id 认人"之后，每 30 分钟跑一次，确认没有串数据。
      全部是 SELECT；不改库、不改文件。异常时退出码 2（方便 cron 告警）。

跑法：python3 scripts/reconcile_identity.py [--minutes 30]
"""
import argparse
import os
import sys

sys.path.insert(0, '/home/ubuntu/smart-locker')
os.chdir('/home/ubuntu/smart-locker')
from database import get_db  # noqa


def q(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall() or []


def one(cur, sql, args=()):
    cur.execute(sql, args)
    r = cur.fetchone()
    if not r:
        return None
    return r[0] if not hasattr(r, 'keys') else list(dict(r).values())[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=int, default=30)
    a = ap.parse_args()
    m = max(1, int(a.minutes))
    conn = get_db()
    cur = conn.cursor()
    bad = []

    print('=== [S521] 身份对账（近 %d 分钟）===' % m)

    # 1) 新订单：订单上的 openid/mp_openid 是否出现在它 user_id 指向的 users 行上
    n1 = one(cur, """
        SELECT count(*) FROM orders o JOIN users u ON u.id = o.user_id
        WHERE o.created_at >= now() - (%s || ' minutes')::interval
          AND o.user_id > 0
          AND COALESCE(o.openid,'') <> '' AND COALESCE(u.openid,'') <> '' AND o.openid <> u.openid
    """, (str(m),))
    n2 = one(cur, """
        SELECT count(*) FROM orders o JOIN users u ON u.id = o.user_id
        WHERE o.created_at >= now() - (%s || ' minutes')::interval
          AND o.user_id > 0
          AND COALESCE(o.mp_openid,'') <> '' AND COALESCE(u.mp_openid,'') <> '' AND o.mp_openid <> u.mp_openid
    """, (str(m),))
    print('  ① 订单身份对不上: 公众号 %s 单 / 小程序 %s 单（应为 0）' % (n1, n2))
    if n1 or n2:
        bad.append('订单身份对不上')
        for r in q(cur, """
            SELECT o.id, o.order_no, o.user_id, o.openid, u.openid AS u_openid,
                   o.mp_openid, u.mp_openid AS u_mp
            FROM orders o JOIN users u ON u.id = o.user_id
            WHERE o.created_at >= now() - (%s || ' minutes')::interval AND o.user_id > 0
              AND ((COALESCE(o.openid,'') <> '' AND COALESCE(u.openid,'') <> '' AND o.openid <> u.openid)
                OR (COALESCE(o.mp_openid,'') <> '' AND COALESCE(u.mp_openid,'') <> '' AND o.mp_openid <> u.mp_openid))
            LIMIT 5
        """, (str(m),)):
            print('     订单%s user_id=%s 订单openid=%s users.openid=%s' % (r[0], r[2], r[3], r[4]))

    # 2) 新增重复身份（同一 mp_openid / 公众号 openid 多行）
    d1 = one(cur, """
        SELECT count(*) FROM (SELECT mp_openid FROM users
            WHERE COALESCE(mp_openid,'') <> '' AND created_at >= now() - (%s || ' minutes')::interval
            GROUP BY mp_openid HAVING count(*) > 1) t
    """, (str(m),))
    d2 = one(cur, """
        SELECT count(*) FROM (SELECT openid FROM users
            WHERE COALESCE(openid,'') <> '' AND created_at >= now() - (%s || ' minutes')::interval
            GROUP BY openid HAVING count(*) > 1) t
    """, (str(m),))
    print('  ② 近 %d 分钟"同一身份多行": 小程序 %s 个 / 公众号 %s 个（应为 0）' % (m, d1, d2))
    if d1 or d2:
        bad.append('新增重复身份')

    # 3) 新建 users 行里三种 id 全空（应为 0）
    n3 = one(cur, """
        SELECT count(*) FROM users
        WHERE created_at >= now() - (%s || ' minutes')::interval
          AND COALESCE(mp_openid,'') = '' AND COALESCE(openid,'') = '' AND COALESCE(alipay_uid,'') = ''
    """, (str(m),))
    print('  ③ 新建"没有任何 id"的用户行: %s（应为 0；存量 941 行不算）' % n3)
    if n3:
        bad.append('新建无 id 用户行')

    # 4) 同一 openid 是否出现在多行钱包（余额被拆散）
    n4 = one(cur, """
        SELECT count(*) FROM (SELECT mp_openid FROM user_balances
            WHERE COALESCE(mp_openid,'') <> ''
            GROUP BY mp_openid HAVING count(*) > 1) t
    """)
    n5 = one(cur, """
        SELECT count(*) FROM (SELECT openid FROM user_balances
            WHERE COALESCE(openid,'') <> ''
            GROUP BY openid HAVING count(*) > 1) t
    """)
    print('  ④ 钱包同一身份多行（存量口径，供趋势观察）: 小程序 %s / 公众号 %s' % (n4, n5))

    # 5) 当前开关
    try:
        sw = one(cur, "SELECT setting_value FROM system_settings WHERE setting_key='identity_strict_mode'")
    except Exception:
        sw = None
    print('  ⑤ identity_strict_mode = %s' % (sw if sw is not None else '(未设置=按 off 处理)'))

    conn.close()
    if bad:
        print('  >>> 异常：%s' % '、'.join(bad))
        return 2
    print('  >>> 正常')
    return 0


if __name__ == '__main__':
    sys.exit(main())
