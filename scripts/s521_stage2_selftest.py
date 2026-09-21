# -*- coding: utf-8 -*-
"""[S521 阶段②] 用真实 openid 验证"分类 + 按 id 认人"，全程事务回滚不留数据"""
import os
import sys

sys.path.insert(0, '/home/ubuntu/smart-locker')
os.chdir('/home/ubuntu/smart-locker')
from database import get_db  # noqa
import helpers  # noqa


def dic(r):
    return dict(r) if r is not None and hasattr(r, 'keys') else r


conn = get_db()
cur = conn.cursor()
cur.execute('BEGIN')

cur.execute('SELECT count(*) AS c FROM users')
before = dic(cur.fetchone())['c']
print('users 行数(前) = %s' % before)

# 取真实样本：最新一条带公众号 openid 的行、最新一条带小程序 openid 的行
cur.execute("SELECT id, openid, mp_openid FROM users WHERE COALESCE(openid,'') <> '' ORDER BY id DESC LIMIT 1")
oa = dic(cur.fetchone())
cur.execute("SELECT id, openid, mp_openid FROM users WHERE COALESCE(mp_openid,'') <> '' ORDER BY id DESC LIMIT 1")
mp = dic(cur.fetchone())
print('样本·公众号行: id=%s openid=%s...' % (oa['id'], (oa['openid'] or '')[:10]))
print('样本·小程序行: id=%s mp_openid=%s...' % (mp['id'], (mp['mp_openid'] or '')[:10]))

for tag, oid, expect_uid in (
        ('公众号 openid 放 openid 字段', oa['openid'], oa['id']),
        ('小程序 openid 放 openid 字段(旧客户端)', mp['mp_openid'], mp['id']),
        ('小程序 openid 放 mp_openid 字段', None, mp['id'])):
    if tag.startswith('小程序 openid 放 mp_openid'):
        k, i = helpers._s521_ident_kind(openid='', mp_openid=mp['mp_openid'], alipay_uid='')
    else:
        k, i = helpers._s521_ident_kind(openid=oid, mp_openid='', alipay_uid='')
    uid = helpers.resolve_user_by_ident(cur, k, i) if k else 0
    print('  %-34s -> kind=%-10s 解析user_id=%-7s 期望=%-7s %s'
          % (tag, k, uid, expect_uid, 'OK' if str(uid) == str(expect_uid) else '**不一致**'))

# 支付宝 uid 也过一遍（拿真实存在的一行）
cur.execute("SELECT id, alipay_uid FROM users WHERE COALESCE(alipay_uid,'') <> '' ORDER BY id DESC LIMIT 1")
al = dic(cur.fetchone())
if al:
    k, i = helpers._s521_ident_kind(openid='', mp_openid='', alipay_uid=al['alipay_uid'])
    uid = helpers.resolve_user_by_ident(cur, k, i) if k else 0
    print('  %-34s -> kind=%-10s 解析user_id=%-7s 期望=%-7s %s'
          % ('支付宝 uid', k, uid, al['id'], 'OK' if str(uid) == str(al['id']) else '**不一致**'))
else:
    print('  （库里还没有支付宝 uid 行，跳过）')

cur.execute('SELECT count(*) AS c FROM users')
after = dic(cur.fetchone())['c']
print('users 行数(解析过程中) = %s（应与前一致，说明没新建行）' % after)

cur.execute('ROLLBACK')
cur.execute('SELECT count(*) AS c FROM users')
print('回滚后 users 行数 = %s' % dic(cur.fetchone())['c'])
conn.close()
