# -*- coding: utf-8 -*-
"""在 175 上验证 openid 前缀改造：取值对不对 + 影子比对（老判断 vs 新判断）"""
import sys

sys.path.insert(0, '/home/ubuntu/smart-locker')
import psycopg2                      # noqa: E402
import wx_config as C                # noqa: E402

DSN = 'postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker'
C.bind(lambda: psycopg2.connect(DSN))
C.clear_cache()

print('=' * 68)
print('【1】前缀取值 + 业务判断')
print('=' * 68)
print('  配置中心: 小程序=%s  公众号=%s' % (C.mp_openid_prefix(), C.oa_openid_prefix()))
import helpers                       # noqa: E402
print('  业务接口 helpers: 小程序=%s  公众号=%s' % (helpers.mp_openid_prefix(), helpers.oa_openid_prefix()))
print('  helpers.is_mp_openid(ooTcRx 开头) = %s （应 True）' % helpers.is_mp_openid('ooTcRxABCDEF'))
print('  helpers.is_mp_openid(oLhbm2 开头) = %s （应 False）' % helpers.is_mp_openid('oLhbm2ABCDEF'))
print('  排除公众号判断 startswith(oa前缀) 对 oLhbm2 开头 = %s （应 True）'
      % str('oLhbm2ABCDEF').startswith(helpers.oa_openid_prefix()))

conn = psycopg2.connect(DSN)
cur = conn.cursor()
cur.execute("select id, acct_type, appid, openid_prefix, is_active from wx_accounts order by id")
print('\n  账号表里的前缀:')
for r in cur.fetchall():
    print('    id=%-2s [%s] %-24s 前缀=%-8s 生效=%s' % (r[0], r[1], r[2], r[3] or '(空)', r[4]))

# 拿一条真实订单的 openid 验一下业务判断
cur.execute("select mp_openid from orders where mp_openid like 'ooTcRx%' order by id desc limit 1")
row = cur.fetchone()
if row:
    real = row[0]
    print('\n  真实订单里的 openid（前10位=%s...）: is_mp_openid = %s （应 True）'
          % (real[:10], helpers.is_mp_openid(real)))
cur.execute("select mp_openid from orders where coalesce(mp_openid,'')<>'' and mp_openid not like 'ooTcRx%' order by id desc limit 1")
row = cur.fetchone()
if row:
    real_old = row[0]
    print('  真实订单里的旧号 openid（前10位=%s...）: is_mp_openid = %s （应 False，这类就是被跳过的）'
          % (real_old[:10], helpers.is_mp_openid(real_old)))

print()
print('=' * 68)
print('【2】影子比对：生产真实数据，逐行比"老判断 vs 新判断"')
print('=' * 68)
MP_OLD = lambda v: bool(v) and str(v).startswith('ooTcRx')       # noqa: E731
OA_OLD = lambda v: bool(v) and str(v).startswith('oLhbm2')       # noqa: E731
MP_NEW = lambda v: bool(v) and str(v).startswith(C.mp_openid_prefix())   # noqa: E731
OA_NEW = lambda v: bool(v) and str(v).startswith(C.oa_openid_prefix())   # noqa: E731

SRC = [('orders', 'mp_openid'), ('orders', 'openid'), ('users', 'mp_openid'),
       ('users', 'openid'), ('user_balances', 'mp_openid'), ('phone_openids', 'mp_openid')]
tot = bad_m = bad_o = 0
samples = []
for t, c in SRC:
    try:
        cur.execute('select distinct "%s" from %s where coalesce("%s",\'\')<>\'\'' % (c, t, c))
    except Exception as e:
        print('  跳过 %s.%s: %s' % (t, c, e))
        conn.rollback()
        continue
    rows = cur.fetchall()
    for (v,) in rows:
        tot += 1
        if MP_OLD(v) != MP_NEW(v):
            bad_m += 1
            if len(samples) < 5:
                samples.append((t, c, v))
        if OA_OLD(v) != OA_NEW(v):
            bad_o += 1
            if len(samples) < 5:
                samples.append((t, c, v))
    print('  %-30s %d 个' % (t + '.' + c, len(rows)))

print('  ---- 合计 %d 个不同的 openid 值 ----' % tot)
print('  小程序判断不一致: %d 个' % bad_m)
print('  公众号判断不一致: %d 个' % bad_o)
print('  %s' % ('✅ 逐个一致：这次改造对现网判断零影响'
                if bad_m == 0 and bad_o == 0 else '❌ 有不一致，必须查！'))
for s in samples:
    print('    ! %s' % (s,))
conn.close()
