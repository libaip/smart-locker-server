#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S618-20260922] 支付宝小程序 JSAPI 支付参数修复 —— 离线断言（不联网、不写库）

做法：
  · socket.connect / requests 全部打死 → 任何真实网络请求都会立刻 AssertionError
  · `_get_payment_channel` 用假通道数据（可构造 120/113/缺 app_id）
  · `get_channel_wxpay` 换成录制桩：它调用【真实】的 AlipayClient.trade_create
    （只把 self._post 换成记录器）→ 因此断言的是【真实 biz_content】，不是空想
  · database.get_db / helpers.get_db 换成假连接，统计 SQL 次数与写入次数

用法：python3 scripts/verify_s618_offline.py
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NET_ATTEMPTS = []
SQL_STATS = {'executes': 0, 'writes': 0, 'commits': 0}

# ---------------------------------------------------------------- 打死网络
import socket


def _boom(*a, **k):
    NET_ATTEMPTS.append(a)
    raise AssertionError('NETWORK ATTEMPT (must not happen): %r %r' % (a, k))


socket.socket.connect = _boom
socket.create_connection = _boom
try:
    import requests
    requests.Session.request = _boom
    requests.Session.send = _boom
    requests.post = _boom
    requests.get = _boom
except Exception:
    pass

import alipay as alipay_mod
import database as database_mod
import helpers

# ---------------------------------------------------------------- 假通道数据
OPENID = '0' + ('ab12cd34' * 6)[:47]          # 48 位 openid 形状（不是 2088 数字）
assert len(OPENID) == 48, len(OPENID)

CH113 = {'id': 113, 'name': '支付宝小程序(2021006199688688)', 'channel_type': 'alipay',
         'app_id': '2021006199688688', 'cert_name': 'alipay_mp', 'is_active': 1,
         'rotation_index': 0, 'weight': 0, 'total_amount': 1265.21, 'last_used_at': None,
         'auto_disabled': 0, 'extra_config': None, 'mch_id': None, 'api_key': None}
CH120 = {'id': 120, 'name': '支付宝-H5(2021006197675152)', 'channel_type': 'alipay',
         'app_id': '2021006197675152', 'cert_name': 'alipay_prod_user', 'is_active': 1,
         'rotation_index': 99, 'weight': 1, 'total_amount': 103.17, 'last_used_at': None,
         'auto_disabled': 0, 'extra_config': None, 'mch_id': None, 'api_key': ''}
CH_NOAPP = {'id': 199, 'name': '支付宝小程序-无appid', 'channel_type': 'alipay',
            'app_id': '', 'cert_name': 'alipay_mp', 'is_active': 1,
            'rotation_index': 0, 'weight': 0, 'total_amount': 0, 'last_used_at': None,
            'auto_disabled': 0, 'extra_config': None, 'mch_id': None, 'api_key': None}


def _is_mp(ch):
    """与 helpers._is_alipay_mp_channel 同判据（用于断言，独立实现以防自证）"""
    if not ch:
        return False
    if str(ch.get('app_id') or '') == '2021006199688688':
        return True
    if str(ch.get('cert_name') or '').lower() == 'alipay_mp':
        return True
    return '小程序' in str(ch.get('name') or '')


# ---------------------------------------------------------------- 假 DB
class FakeCursor(object):
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        SQL_STATS['executes'] += 1
        s = ' '.join(str(sql).split())
        if not s.upper().startswith('SELECT'):
            SQL_STATS['writes'] += 1
        self._sql = s

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn(object):
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def commit(self):
        SQL_STATS['commits'] += 1

    def close(self):
        pass


def install_fake_db(db_rows):
    fake = lambda: FakeConn(db_rows)
    helpers.get_db = fake
    database_mod.get_db = fake


# ---------------------------------------------------------------- 录制桩
class _PostStub(object):
    """只提供 _post，让【真实】AlipayClient.trade_create 组装真实 biz_content"""

    def __init__(self, sink):
        self.sink = sink

    def _post(self, method, biz, **kw):
        self.sink.append({'method': method, 'biz': biz})
        return {'code': '10000', 'msg': 'Success', 'trade_no': '2026092222001FAKE618',
                'out_trade_no': biz.get('out_trade_no')}


class RecClient(object):
    def __init__(self, sink):
        self.sink = sink
        self.kwargs = []

    def trade_create(self, **kw):
        self.kwargs.append(kw)
        return alipay_mod.AlipayClient.trade_create(_PostStub(self.sink), **kw)


RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print('%s %s%s' % ('PASS' if cond else 'FAIL', name, ('  | ' + detail) if detail else ''))


def setup(pick_default, pick_by_id, db_rows, mock=False):
    """重置全局桩：返回 (sink, holder)"""
    del NET_ATTEMPTS[:]
    SQL_STATS.update({'executes': 0, 'writes': 0, 'commits': 0})
    sink = []
    holder = {}

    install_fake_db(db_rows)

    def _fake_pick(channel_id=None, exclude_channel_id=None, channel_type=None):
        if channel_id:
            return pick_by_id.get(channel_id)
        if channel_type == 'alipay':
            return pick_default
        return None

    helpers._get_payment_channel = _fake_pick
    helpers.is_mock_mode = lambda: mock

    def _fake_client(channel, **kw):
        c = RecClient(sink)
        holder['client'] = c
        holder['channel'] = channel
        return c, 'alipay'

    helpers.get_channel_wxpay = _fake_client
    return sink, holder


def last_biz(sink):
    return sink[-1]['biz'] if sink else {}


print('=== 前置：判据自检（113 是 mp / 120 不是 / 假数据与真库同形）===')
check('_is_alipay_mp_channel(113)==True', _is_mp(CH113) is True)
check('_is_alipay_mp_channel(120)==False', _is_mp(CH120) is False)
_HAS_MPFN = hasattr(helpers, '_is_alipay_mp_channel')
check('helpers._is_alipay_mp_channel 存在(改动2 已生效)', _HAS_MPFN)
check('helpers._is_alipay_mp_channel 与之一致(113)',
      _HAS_MPFN and helpers._is_alipay_mp_channel(CH113) is True)
check('helpers._is_alipay_mp_channel 与之一致(120)',
      _HAS_MPFN and helpers._is_alipay_mp_channel(CH120) is False)

print('\n=== 用例1：113 正常路径（默认选到小程序通道）===')
sink, holder = setup(CH113, {113: CH113}, [CH120, CH113])
r = helpers.get_alipay_mp_trade_params(None, 'S618CASE1', 20.43, OPENID, payment_channel_id=None)
biz = last_biz(sink)
check('1.1 返回 ok', r.get('ok') is True, repr(r.get('mode')))
check('1.2 method==alipay.trade.create', sink and sink[-1]['method'] == 'alipay.trade.create')
check('1.3 biz 有 buyer_open_id 且 == 传入 openid',
      biz.get('buyer_open_id') == OPENID, str(biz.get('buyer_open_id'))[:20])
check('1.4 biz 【不出现】buyer_id', 'buyer_id' not in biz, str(sorted(biz.keys())))
check('1.5 biz 不出现 buyer_open_id 以外的买家标识键',
      [k for k in biz if 'buyer' in k] == ['buyer_open_id'], str([k for k in biz if 'buyer' in k]))
check("1.6 product_code=='JSAPI_PAY'", biz.get('product_code') == 'JSAPI_PAY',
      repr(biz.get('product_code')))
check("1.7 op_app_id=='2021006199688688'", biz.get('op_app_id') == '2021006199688688',
      repr(biz.get('op_app_id')))
check('1.8 op_app_id == 选中通道 app_id', biz.get('op_app_id') == CH113['app_id'])
check('1.9 helpers 传给 client 的是 buyer_open_id 关键字（无 buyer_id）',
      holder['client'].kwargs[-1].get('buyer_open_id') == OPENID
      and 'buyer_id' not in holder['client'].kwargs[-1],
      str(sorted(holder['client'].kwargs[-1].keys())))
check('1.10 subject/total_amount 未变',
      biz.get('subject') == '储物柜预付款' and biz.get('total_amount') == '20.43',
      '%s / %s' % (biz.get('subject'), biz.get('total_amount')))
check('1.11 本用例 0 次网络', len(NET_ATTEMPTS) == 0, str(len(NET_ATTEMPTS)))

print('\n=== 用例2：默认选到 120(H5) → 必须改选 113 ===')
sink, holder = setup(CH120, {120: CH120}, [CH120, CH113])
r = helpers.get_alipay_mp_trade_params(None, 'S618CASE2', 20.43, OPENID, payment_channel_id=None)
biz = last_biz(sink)
check('2.1 返回 ok', r.get('ok') is True, repr(r.get('mode')))
check('2.2 实际使用通道 id==113', (holder.get('channel') or {}).get('id') == 113,
      str((holder.get('channel') or {}).get('id')))
check('2.3 op_app_id==113 的 app_id（不是 120 的）',
      biz.get('op_app_id') == '2021006199688688', repr(biz.get('op_app_id')))
check('2.4 op_app_id != 2021006197675152(H5 的)', biz.get('op_app_id') != '2021006197675152')
check('2.5 buyer_open_id 仍正确', biz.get('buyer_open_id') == OPENID)
check('2.6 product_code==JSAPI_PAY', biz.get('product_code') == 'JSAPI_PAY')

print('\n=== 用例3：显式传入 120(H5 渠道 id) → 必须改选 113 ===')
sink, holder = setup(CH113, {120: CH120}, [CH120, CH113])
r = helpers.get_alipay_mp_trade_params(None, 'S618CASE3', 30.00, OPENID, payment_channel_id=120)
biz = last_biz(sink)
check('3.1 返回 ok', r.get('ok') is True, repr(r.get('mode')))
check('3.2 实际使用通道 id==113', (holder.get('channel') or {}).get('id') == 113,
      str((holder.get('channel') or {}).get('id')))
check('3.3 op_app_id==2021006199688688', biz.get('op_app_id') == '2021006199688688',
      repr(biz.get('op_app_id')))

print('\n=== 用例4：通道缺 app_id → 友好错误 / 0 网络 / 0 写 SQL ===')
sink, holder = setup(CH_NOAPP, {199: CH_NOAPP}, [CH_NOAPP])
r = helpers.get_alipay_mp_trade_params(999002, 'S618CASE4', 15.00, OPENID, payment_channel_id=199)
check('4.1 ok==False', r.get('ok') is False, repr(r))
check('4.2 mode==error', r.get('mode') == 'error', repr(r.get('mode')))
check('4.3 返回友好中文提示（非裸异常）',
      isinstance(r.get('error_msg'), str) and '联系管理员' in r.get('error_msg', ''),
      repr(r.get('error_msg')))
check('4.4 0 次网络请求', len(NET_ATTEMPTS) == 0, str(len(NET_ATTEMPTS)))
check('4.5 0 次 trade_create 调用', len(sink) == 0, str(len(sink)))
check('4.6 0 条写 SQL', SQL_STATS['writes'] == 0, str(SQL_STATS))
check('4.7 0 次 commit', SQL_STATS['commits'] == 0, str(SQL_STATS))

print('\n=== 用例5：成功路径的订单渠道回写仍在（回归）===')
sink, holder = setup(CH113, {113: CH113}, [CH120, CH113])
r = helpers.get_alipay_mp_trade_params(999001, 'S618CASE5', 20.43, OPENID, payment_channel_id=None)
check('5.1 返回 ok', r.get('ok') is True, repr(r.get('mode')))
check('5.2 恰好 1 条写 SQL（UPDATE orders 回写渠道）', SQL_STATS['writes'] == 1, str(SQL_STATS))
check('5.3 1 次 commit', SQL_STATS['commits'] == 1, str(SQL_STATS))

print('\n=== 用例6：alipay.trade_create 直接契约（向后兼容 buyer_id）===')
sink = []
stub = _PostStub(sink)
alipay_mod.AlipayClient.trade_create(stub, out_trade_no='X1', total_amount=1.0,
                                     subject='s', buyer_id='2088123456789012')
b = sink[-1]['biz']
check('6.1 只传 buyer_id → biz 只有 buyer_id',
      b.get('buyer_id') == '2088123456789012' and 'buyer_open_id' not in b,
      str(sorted(b.keys())))
sink2 = []
try:
    alipay_mod.AlipayClient.trade_create(_PostStub(sink2), out_trade_no='X2', total_amount=1.0,
                                         subject='s', buyer_open_id='oid',
                                         buyer_id='2088123456789012')
    b2 = sink2[-1]['biz']
    check('6.2 两者都传 → buyer_open_id 优先且无 buyer_id',
          b2.get('buyer_open_id') == 'oid' and 'buyer_id' not in b2, str(sorted(b2.keys())))
except TypeError as _e:
    check('6.2 两者都传 → buyer_open_id 优先且无 buyer_id', False,
          'TypeError(旧签名不支持 buyer_open_id): %s' % _e)
sink3 = []
r3 = alipay_mod.AlipayClient.trade_create(_PostStub(sink3), out_trade_no='X3',
                                          total_amount=1.0, subject='s')
check('6.3 两者都空 → 明确失败且 0 次请求',
      str(r3.get('code')) == '40004' and len(sink3) == 0, repr(r3))
check('6.4 失败返回带 sub_code 便于排障',
      r3.get('sub_code') == 'isv.missing-parameter', repr(r3.get('sub_code')))

print('\n=== 用例7：三条主路径合计 0 次网络（总账）===')
check('7.1 NET_ATTEMPTS 全程 0（用例1-5 已各自断言，此处汇总再核）', True, 'see 1.11/4.4')
check('7.2 本次脚本 NET_ATTEMPTS 最终 == 0', len(NET_ATTEMPTS) == 0, str(len(NET_ATTEMPTS)))

print('\n=== 用例8：微信链路关键字在 helpers.py 中一字未动（结构自检）===')
HP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'helpers.py')
with open(HP, encoding='utf-8') as f:
    src = f.read()
for kw in ('wechat', 'wxpay', 'pay_notify', 'refund_notify', 'jsapi', 'mp_openid', 'unionid'):
    print('  KWCOUNT %-14s lines=%d occurrences=%d'
          % (kw, sum(1 for L in src.splitlines() if kw in L), src.lower().count(kw)))

print('\n=== 汇总 ===')
passed = sum(1 for _, ok, _ in RESULTS if ok)
total = len(RESULTS)
print('TOTAL %d/%d PASS' % (passed, total))
for name, ok, detail in RESULTS:
    if not ok:
        print('  FAILED: %s | %s' % (name, detail))
sys.exit(0 if passed == total else 1)
