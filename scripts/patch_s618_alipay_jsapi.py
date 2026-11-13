#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S618-20260922] 支付宝「小程序内 JSAPI 支付」服务端下单参数修复（纯增量、锚点校验、默认干跑）

改什么：
  alipay.py  : trade_create 签名 / 买家标识口径(open_id 优先) / JSAPI 必传参数说明
  helpers.py : _mp_pick_alipay_channel 优先返回【小程序】通道；
               get_alipay_mp_trade_params 改传 buyer_open_id + product_code='JSAPI_PAY' + op_app_id

用法：
  python3 scripts/patch_s618_alipay_jsapi.py            # 干跑（只打印，不写文件）
  python3 scripts/patch_s618_alipay_jsapi.py --apply    # 写入（自动备份）
"""
import argparse
import difflib
import os
import shutil
import sys
from datetime import datetime

ALIPAY = 'alipay.py'
HELPERS = 'helpers.py'

# ======================================================================
# alipay.py —— 改动1
# ======================================================================
A1_OLD = """    def trade_create(self, out_trade_no, total_amount, subject, buyer_id,
                     body='', timeout_express='15m', product_code='', op_app_id=''):
"""
A1_NEW = """    def trade_create(self, out_trade_no, total_amount, subject, buyer_open_id='',
                     buyer_id='', body='', timeout_express='15m', product_code='',
                     op_app_id=''):
"""

A2_OLD = """          · buyer_id 必传 = 买家的支付宝 user_id（本项目 = users.alipay_uid）；
"""
A2_NEW = """          · [S618] 买家标识按官方口径二选一：buyer_open_id 优先、buyer_id 兜底，
            两者都空 → 直接返回失败（绝不发出无买家标识的请求）；
            buyer_open_id = 用户在本小程序(appid)下的 openid（本项目 = users.alipay_uid）；
            buyer_id      = 2088 开头 16 位支付宝 user_id（老字段，保留兼容）；
          · [S618] 小程序支付必传 product_code='JSAPI_PAY' 与
            op_app_id=「唤起收银台支付所在的小程序 appid」（须先在产品中心绑定该 appid）；
"""

A3_OLD = """        biz = {
            'out_trade_no': str(out_trade_no),
            'total_amount': '%.2f' % float(total_amount),
            'subject': (subject or '储物柜预付款')[:256],
            'buyer_id': str(buyer_id),
        }
"""
A3_NEW = """        buyer_open_id = str(buyer_open_id or '').strip()
        buyer_id = str(buyer_id or '').strip()
        if not buyer_open_id and not buyer_id:
            return {'code': '40004', 'msg': 'Business Failed',
                    'sub_code': 'isv.missing-parameter',
                    'sub_msg': '缺少买家标识: buyer_open_id 与 buyer_id 不能同时为空'}
        biz = {
            'out_trade_no': str(out_trade_no),
            'total_amount': '%.2f' % float(total_amount),
            'subject': (subject or '储物柜预付款')[:256],
        }
        # [S618] 官方口径：buyer_open_id 与 buyer_id 二选一
        #   （新商户推荐 buyer_open_id；本项目 users.alipay_uid 存的就是 openid）
        if buyer_open_id:
            biz['buyer_open_id'] = buyer_open_id
        else:
            biz['buyer_id'] = buyer_id
"""

# ======================================================================
# helpers.py —— 改动2
# ======================================================================
B1_OLD = """# ============================================
# [S334] 支付宝小程序支付参数（alipay.trade.create → 前端 my.tradePay）
# ============================================
def _mp_pick_alipay_channel(channel_id=None):
"""
B1_NEW = """# ============================================
# [S334] 支付宝小程序支付参数（alipay.trade.create → 前端 my.tradePay）
# ============================================
# [S618] 支付宝「小程序」通道判据：小程序支付的 op_app_id 必须是小程序 appid，
#   而 payment_channels 里 113=支付宝小程序(2021006199688688) 与
#   120=支付宝-H5(2021006197675152) 两条 channel_type 都是 'alipay' 且都 is_active=1，
#   所以选通道时必须显式优先小程序通道，否则会把 H5 的 appid 当 op_app_id 发出去。
_ALIPAY_MP_APP_ID = '2021006199688688'


def _is_alipay_mp_channel(channel):
    \"\"\"[S618] 该支付宝通道是否为【小程序】通道（小程序支付必须用它）。任一命中即为真：

      1) app_id == 小程序 appid 2021006199688688；
      2) cert_name == 'alipay_mp'（密钥文件名口径）；
      3) name 含「小程序」。

    H5 通道 120（app_id=2021006197675152 / cert_name=alipay_prod_user / 名称「支付宝-H5」）
    三条都不命中 → 不会被误判成小程序通道。
    \"\"\"
    if not channel:
        return False
    if str(channel.get('app_id') or '').strip() == _ALIPAY_MP_APP_ID:
        return True
    if str(channel.get('cert_name') or '').strip().lower() == 'alipay_mp':
        return True
    return '小程序' in str(channel.get('name') or '')


def _pick_alipay_mp_channel_from_db():
    \"\"\"[S618] 直接查库取【小程序】通道（含 is_active=0 的兜底）；取不到返回 None。

    只读 SELECT，不写库；异常只记日志并返回 None（绝不抛出）。
    \"\"\"
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(\"SELECT * FROM payment_channels WHERE channel_type='alipay' \"
                       \"ORDER BY is_active DESC, auto_disabled ASC, rotation_index ASC, id ASC\")
        rows = cursor.fetchall()
        conn.close()
        for _r in rows:
            if (_r.get('channel_type') or '') == 'alipay' and _is_alipay_mp_channel(_r):
                return dict(_r)
    except Exception as _e:
        logger.error('[alipay-mp] 查库挑支付宝小程序通道失败: %s', _e)
    return None


def _mp_pick_alipay_channel(channel_id=None):
"""

B2_OLD = """    现状（2026-09-19）：payment_channels.id=113（app_id=2021006199688688，cert_name=alipay_mp）
    就是本项目的支付宝【小程序应用】，但 is_active=0 —— 老板要求先别改库。而
    _get_payment_channel(channel_type='alipay') 只认 is_active=1，所以这里：
      1) 先按官方口径选【活跃】的支付宝通道；
      2) 一个都没有时，退回查库拿未启用的支付宝通道（只为把链路先跑通，日志明确告警）；
         一旦老板把 113 置成 is_active=1，第 2 步就永远不会触发。
    \"\"\"
"""
B2_NEW = """    [S618] 两个现状更正 + 小程序优先：
      · payment_channels 里 113（支付宝小程序 2021006199688688 / cert_name=alipay_mp）
        与 120（支付宝-H5 2021006197675152 / cert_name=alipay_prod_user）
        【都】是 channel_type='alipay' 且【都】is_active=1；
      · 所以选通道时必须优先返回【小程序】通道（见 _is_alipay_mp_channel）：
        小程序支付的 op_app_id 必须是小程序 appid，选到 120 就会传错 appid 被支付宝拒；
      · 一个活跃的都没有时，仍退回查库（含 is_active=0，仅告警不拦），同样优先小程序通道。
    \"\"\"
"""

B3_OLD = """        if _ch and (_ch.get('channel_type') or '') == 'alipay':
            return _ch
        return None
"""
B3_NEW = """        if _ch and (_ch.get('channel_type') or '') == 'alipay':
            # [S618] 显式指定的渠道若不是【小程序】通道（例如订单挂的是 120=H5），
            #   仍要改选小程序通道：小程序支付的 op_app_id 必须是小程序 appid。
            if _is_alipay_mp_channel(_ch):
                return _ch
            _mp = _pick_alipay_mp_channel_from_db()
            if _mp:
                logger.warning('[alipay-mp] 指定通道 id=%s 不是支付宝小程序通道，'
                               '改选小程序通道 id=%s', channel_id, _mp.get('id'))
                return _mp
            logger.warning('[alipay-mp] 库里没有支付宝小程序通道，沿用指定通道 id=%s', channel_id)
            return _ch
        return None
"""

B4_OLD = """    if _ch and (_ch.get('channel_type') or '') == 'alipay':
        return _ch
    # 兜底：查库直接找 alipay 通道（包含 is_active=0 的 113，仅告警不拦）
"""
B4_NEW = """    if _ch and (_ch.get('channel_type') or '') == 'alipay':
        if _is_alipay_mp_channel(_ch):
            return _ch
        # [S618] 选到的是 H5 通道 → 改选小程序通道
        #   （轮转/sequential 两种模式都可能选到 120）
        _mp = _pick_alipay_mp_channel_from_db()
        if _mp:
            logger.warning('[alipay-mp] 选到非小程序通道 id=%s，改选小程序通道 id=%s',
                           _ch.get('id'), _mp.get('id'))
            return _mp
        return _ch
    # 兜底：查库直接找 alipay 通道（包含 is_active=0 的 113，仅告警不拦）
"""

B5_OLD = """        for _r in rows:
            if (_r.get('channel_type') or '') == 'alipay':
                logger.warning('[alipay-mp] 没有 is_active=1 的支付宝通道，退回使用未启用通道 id=%s '
                               '（老板要求暂不改库；置 is_active=1 后本告警消失）', _r.get('id'))
                return dict(_r)
"""
B5_NEW = """        for _r in rows:                     # [S618] 先找小程序通道
            if (_r.get('channel_type') or '') == 'alipay' and _is_alipay_mp_channel(_r):
                logger.warning('[alipay-mp] 没有 is_active=1 的支付宝通道，'
                               '退回使用未启用小程序通道 id=%s', _r.get('id'))
                return dict(_r)
        for _r in rows:
            if (_r.get('channel_type') or '') == 'alipay':
                logger.warning('[alipay-mp] 没有 is_active=1 的支付宝通道，退回使用未启用通道 id=%s '
                               '（老板要求暂不改库；置 is_active=1 后本告警消失）', _r.get('id'))
                return dict(_r)
"""

B6_OLD = """        client, ch_type = get_channel_wxpay(channel)
        if client is None or ch_type != 'alipay':
"""
B6_NEW = """        # [S618] 小程序支付官方必传：op_app_id = 唤起收银台支付所在的小程序 appid。
        #   取不到就明确报错并返回友好提示，绝不静默发一个缺必选参数的请求。
        _op_app_id = str(channel.get('app_id') or '').strip()
        if not _op_app_id:
            logger.error('[alipay-mp] 通道 id=%s 未配置 app_id，无法传 op_app_id（小程序支付必传）order=%s',
                         channel.get('id'), order_no)
            return {'ok': False, 'mode': 'error',
                    'error_msg': '支付宝商户配置不完整，请联系管理员'}

        client, ch_type = get_channel_wxpay(channel)
        if client is None or ch_type != 'alipay':
"""

B7_OLD = """        resp = client.trade_create(out_trade_no=order_no, total_amount=amount,
                                   subject=subject, buyer_id=alipay_uid,
                                   timeout_express=timeout_express)
"""
B7_NEW = """        resp = client.trade_create(out_trade_no=order_no, total_amount=amount,
                                   subject=subject, buyer_open_id=alipay_uid,
                                   product_code='JSAPI_PAY', op_app_id=_op_app_id,
                                   timeout_express=timeout_express)
"""

PATCHES = [
    (ALIPAY, 'A1 signature', A1_OLD, A1_NEW),
    (ALIPAY, 'A2 docstring', A2_OLD, A2_NEW),
    (ALIPAY, 'A3 biz buyer id', A3_OLD, A3_NEW),
    (HELPERS, 'B1 insert mp helpers', B1_OLD, B1_NEW),
    (HELPERS, 'B2 docstring refresh', B2_OLD, B2_NEW),
    (HELPERS, 'B3 explicit channel_id mp', B3_OLD, B3_NEW),
    (HELPERS, 'B4 default pick mp', B4_OLD, B4_NEW),
    (HELPERS, 'B5 db fallback mp', B5_OLD, B5_NEW),
    (HELPERS, 'B6 op_app_id guard', B6_OLD, B6_NEW),
    (HELPERS, 'B7 call site', B7_OLD, B7_NEW),
]


def diffstat(fn, new_text):
    with open(fn, encoding='utf-8') as f:
        old_lines = f.read().splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks = added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        hunks += 1
        removed += i2 - i1
        added += j2 - j1
    return hunks, added, removed, len(old_lines), len(new_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--backup-dir', default='')
    args = ap.parse_args()

    texts = {}
    for fn in (ALIPAY, HELPERS):
        with open(fn, encoding='utf-8') as f:
            texts[fn] = f.read()

    ok = True
    for fn, tag, old, new in PATCHES:
        n = texts[fn].count(old)
        print('ANCHOR %-24s %-10s hits=%d' % (tag, fn, n))
        if n != 1:
            ok = False
            continue
        texts[fn] = texts[fn].replace(old, new, 1)

    if not ok:
        print('\n[FAIL] anchor hits != 1, nothing written.')
        return 1

    for fn in (ALIPAY, HELPERS):
        hunks, added, removed, lo, ln = diffstat(fn, texts[fn])
        print('DIFF %-11s hunks=%d added=%d removed=%d lines %d->%d'
              % (fn, hunks, added, removed, lo, ln))

    if not args.apply:
        print('\n(dry-run) nothing written. use --apply')
        return 0

    bdir = args.backup_dir or ('backups/s618_jsapi_%s' % datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(bdir, exist_ok=True)
    for fn in (ALIPAY, HELPERS):
        shutil.copy2(fn, os.path.join(bdir, fn))
    print('BACKUP_DIR=%s' % bdir)

    for fn in (ALIPAY, HELPERS):
        with open(fn, 'w', encoding='utf-8', newline='') as f:
            f.write(texts[fn])
        print('WROTE %s' % fn)
    return 0


if __name__ == '__main__':
    sys.exit(main())
