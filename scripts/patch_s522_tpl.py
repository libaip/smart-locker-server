# -*- coding: utf-8 -*-
"""[S522] /user/subscribe-templates 增加支付宝分支（platform=alipay 时返回支付宝自己的模板）
微信那段逻辑（Referer/appid 分流、A/B、落地页判定）与支付宝无关，这里短路返回；
platform 不是 alipay 时【一字不变】走原逻辑。
用法：python patch_s522_tpl.py <routes/user.py>
"""
import hashlib
import shutil
import sys

P = sys.argv[1]
s = open(P, encoding='utf-8', newline='').read()
before = hashlib.md5(s.encode('utf-8')).hexdigest()
assert 'S522' not in s, '已打过 S522'

ANCHOR = '    """返回订阅消息模板ID列表（动态下发，前端不写死）"""'
assert s.count(ANCHOR) == 1, '锚点命中 %d 次' % s.count(ANCHOR)

NEW = ANCHOR + '''
    # [S522-20260921] 支付宝小程序分支：返回支付宝自己的订阅消息模板（老板提供）
    #   微信这段（Referer/appid 分流、A/B、落地页）与支付宝无关，直接短路；
    #   platform 不是 alipay 时一字不变走下面原逻辑。
    try:
        if str(request.args.get('platform') or '').strip().lower() == 'alipay':
            from wx_config import template_id as _tid522
            _g522 = _tid522('subscribe_general', 'alipay', '')   # 账户余额通知
            _w522 = _tid522('subscribe_refund', 'alipay', '')    # 寄存押金退还通知
            _ids522 = [x for x in (_g522, _w522) if x]
            logger.info('[subscribe_templates] 支付宝分支: general=%s... refund=%s...',
                        str(_g522)[:8], str(_w522)[:8])
            return json_response(data={
                'templates': _ids522,
                'deposit': _g522, 'deposit_notify': _g522,
                'general': _g522, 'general_notify': _g522,
                'withdraw': _w522, 'withdraw_notify': _w522,
                'ab_group': 'alipay', 'ab_mode': 'alipay', 'requested_count': len(_ids522),
                'landing': False, 'platform': 'alipay',
            })
    except Exception as _e522:
        logger.warning('[subscribe_templates] 支付宝分支异常，回落微信逻辑: %s', _e522)
'''

s = s.replace(ANCHOR, NEW, 1)
shutil.copy2(P, P + '.bak_s522')
open(P, 'w', encoding='utf-8', newline='').write(s)
print('user.py %s -> %s' % (before, hashlib.md5(s.encode('utf-8')).hexdigest()))
print('标记: S522=%d' % s.count('S522-20260921'))
