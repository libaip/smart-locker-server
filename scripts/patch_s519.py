# -*- coding: utf-8 -*-
"""
S519-20260921 订单桥接：支付宝两边 ID 都落到同一张订单上（只动支付宝）
=====================================================================
背景：支付宝 H5（appId 2021006197675152）和小程序（appId 2021006199688688）是两个应用，
      拿到的 uid 是【应用维度】的，很可能不是同一个值 —— 所以用"订单号"当桥：
      同一张订单上，小程序侧落一个 uid、支付侧落一个 uid，映射关系自然就有了。

改动（全部是加法，微信那套一行不碰）：
  1) DB：orders 新增两列
       alipay_mp_uid   —— 小程序侧 uid（my.getAuthCode → /alipay/login 的 user_id）
       alipay_pay_uid  —— 支付侧 uid（支付宝回调/查单里的 buyer_id / buyer_user_id）
  2) routes/user.py  /alipay/login   ：接受可选 order_id，把小程序 uid 写到那张订单的 alipay_mp_uid
       · 只写这一列；绝不碰 orders.user_id / openid / mp_openid / unionid
       · 只在空时写（COALESCE(alipay_mp_uid,'')=''），失败只告警、不影响登录
  3) routes/payment.py /pay/notify/alipay ：取付款人 uid（验签路径取 buyer_id；查单路径取 buyer_user_id）
       写到 orders.alipay_pay_uid（同样只在空时写，失败不影响订单已支付）

用法：python patch_s519.py <user.py> <payment.py>
"""
import hashlib
import shutil
import sys

UP = sys.argv[1]
PP = sys.argv[2]
bu = open(UP, encoding='utf-8', newline='').read()
bp = open(PP, encoding='utf-8', newline='').read()
mu_before = hashlib.md5(bu.encode('utf-8')).hexdigest()
mp_before = hashlib.md5(bp.encode('utf-8')).hexdigest()
assert 'S519' not in bu or 'alipay_mp_uid' not in bu, 'user.py 已打过 S519'
assert 'alipay_pay_uid' not in bp, 'payment.py 已打过 S519'
errors = []


def rep(text, old, new, tag):
    n = text.count(old)
    if n != 1:
        errors.append('%s 锚点命中 %d 次（应为1）' % (tag, n))
        return text
    return text.replace(old, new, 1)


# ---------- 1) /alipay/login：落 alipay_mp_uid ----------
bu = rep(bu,
         """                logger.info('[alipay_login] 新建支付宝用户 id=%s alipay_uid=%s...' % (user_id, alipay_uid[:8]))
            conn.commit()""",
         """                logger.info('[alipay_login] 新建支付宝用户 id=%s alipay_uid=%s...' % (user_id, alipay_uid[:8]))
            # [S519] 订单桥接：小程序把 order_id 带上来了 -> 把"小程序侧 uid"落到这张订单上。
            #   只写支付宝专用列 alipay_mp_uid，绝不动 orders.user_id / openid / mp_openid / unionid
            #   （H5/微信那一侧的认人方式完全不变）。只在空时写；失败只告警，不影响登录。
            _oid = 0
            try:
                _oid = int(str(data.get('order_id') or data.get('orderId') or '').strip() or 0)
            except Exception:
                _oid = 0
            _bound = False
            if _oid:
                try:
                    cur.execute(\"\"\"UPDATE orders SET alipay_mp_uid = %s
                                   WHERE id = %s AND COALESCE(alipay_mp_uid, '') = ''\"\"\",
                                (alipay_uid, _oid))
                    _bound = cur.rowcount > 0
                    if _bound:
                        logger.info('[alipay_login] 订单桥接: order=%s alipay_mp_uid=%s...'
                                    % (_oid, alipay_uid[:8]))
                except Exception as _oe:
                    logger.warning('[alipay_login] 订单桥接失败(不影响登录): order=%s err=%s' % (_oid, _oe))
            conn.commit()""",
         'user.py 订单桥接写入')

bu = rep(bu,
         """        return json_response({'alipay_uid': alipay_uid, 'user_id': user_id,
                              'phone': phone, 'is_new': is_new}, code=200)""",
         """        return json_response({'alipay_uid': alipay_uid, 'user_id': user_id,
                              'phone': phone, 'is_new': is_new,
                              'order_id': _oid, 'order_bound': _bound}, code=200)""",
         'user.py 返回值加 order_id/order_bound')

# ---------- 2) 支付宝回调：取付款人 uid ----------
bp = rep(bp,
         """        params = dict(request.form) if request.form else dict(request.args)
        out_trade_no = str(params.get('out_trade_no') or '')""",
         """        params = dict(request.form) if request.form else dict(request.args)
        out_trade_no = str(params.get('out_trade_no') or '')
        # [S519] 订单桥接的另一半：付款人的支付宝 uid。
        #   验签路径 -> 通知里的 buyer_id；查单路径 -> 查单结果里的 buyer_user_id（下面覆盖）
        _buyer_uid = str(params.get('buyer_id') or params.get('buyer_user_id') or '').strip()""",
         'payment.py 取 buyer_id')

bp = rep(bp,
         """                verified_by = 'query'
                params = {'trade_no': q.get('trade_no'), 'total_amount': q.get('total_amount')}""",
         """                verified_by = 'query'
                _buyer_uid = str(q.get('buyer_user_id') or q.get('buyer_id') or _buyer_uid or '').strip()
                params = {'trade_no': q.get('trade_no'), 'total_amount': q.get('total_amount')}""",
         'payment.py 查单路径取 buyer_user_id')

bp = rep(bp,
         """            if order.get('payment_channel_id'):
                try:
                    update_channel_stats(order['payment_channel_id'], amount)
                except Exception:
                    pass
        conn.commit()""",
         """            if order.get('payment_channel_id'):
                try:
                    update_channel_stats(order['payment_channel_id'], amount)
                except Exception:
                    pass
        # [S519] 订单桥接：把付款人 uid 落到本单（只写支付宝专用列，不动 user_id/openid/mp_openid）
        if updated and _buyer_uid:
            try:
                cursor.execute(\"\"\"UPDATE orders SET alipay_pay_uid = %s
                                  WHERE id = %s AND COALESCE(alipay_pay_uid, '') = ''\"\"\",
                               (_buyer_uid, order['id']))
                logger.info('[支付宝回调] 订单桥接: order=%s alipay_pay_uid=%s...'
                            % (order['id'], _buyer_uid[:8]))
            except Exception as _be:
                logger.error('[支付宝回调] 落 alipay_pay_uid 失败(不影响订单已支付): order=%s err=%s'
                             % (order['id'], _be))
        conn.commit()""",
         'payment.py 落 alipay_pay_uid')

if errors:
    print('❌ 有锚点没命中，未写任何文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

for path, text in ((UP, bu), (PP, bp)):
    shutil.copy2(path, path + '.bak_s519')
    open(path, 'w', encoding='utf-8', newline='').write(text)

print('user.py    %s -> %s' % (mu_before, hashlib.md5(bu.encode('utf-8')).hexdigest()))
print('payment.py %s -> %s' % (mp_before, hashlib.md5(bp.encode('utf-8')).hexdigest()))
print('alipay_mp_uid 出现 %d 次；alipay_pay_uid 出现 %d 次' % (bu.count('alipay_mp_uid'), bp.count('alipay_pay_uid')))
