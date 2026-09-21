# -*- coding: utf-8 -*-
"""
S511-20260921 精准放开支付宝 H5（修 S415 误伤）
==============================================
问题：S415 为了拦"支付宝小程序 webview 带着错身份下单"，条件是
      UA 含 'AlipayClient' 或 'AliApp(' 或 Referer 含 'alipay-eco.com'。
      但【支付宝 App 内置浏览器打开我们的 H5 时，UA 也是 AlipayClient / AliApp(AP/...)】，
      于是真实用户从支付宝扫柜机码进来，下单也会被 403 —— 支付宝这条路等于被整体堵死。

改法：只按【Referer】区分
      · 支付宝小程序内嵌页：Referer = 2021006199688688.hybrid.alipay-eco.com  → 继续拦（那条老路带了微信身份，会误伤微信商户）
      · 支付宝 App 打开 H5 ：Referer 为空 / 是我们的域名                        → 放行（走手机网站支付）
说明：S415 已同时修掉根因（已停用公众号不再参与按 openid 取 appid；APPID_MCHID_NOT_MATCH 只换渠道不再停商户），
      所以放开后即使有异常请求，也不会再像今天 08:00 那样把微信商户停掉。
"""
import hashlib
import py_compile
import shutil

P = 'routes/user.py'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S511-20260921' not in src, '已打过 S511 补丁，中止'

OLD_1 = """        _ua415 = request.headers.get('User-Agent', '') or ''
        _ref415 = request.headers.get('Referer', '') or ''
        if ('AlipayClient' in _ua415) or ('AliApp(' in _ua415) or ('alipay-eco.com' in _ref415):
            logger.warning('[S415] 拒绝支付宝小程序下单: ua=%s ref=%s', _ua415[:60], _ref415[:80])
            return json_response(message='该入口已停用，请用微信扫柜机上的二维码使用', code=403)"""

NEW_1 = """        # [S511-20260921] 精准放开：只拦"支付宝小程序内嵌页"（Referer 带 hybrid.alipay-eco.com）那条老路；
        #   支付宝 App 内置浏览器打开我们的 H5（UA 同样是 AlipayClient）【必须放行】，否则支付宝用户没法付钱。
        _ref511 = request.headers.get('Referer', '') or ''
        if 'alipay-eco.com' in _ref511:
            logger.warning('[S511] 拒绝支付宝小程序内嵌页下单: ref=%s', _ref511[:90])
            return json_response(message='该入口已停用，请用微信扫码，或在支付宝里打开我们的存包网页', code=403)"""

n1 = src.count(OLD_1)
assert n1 == 2, 'S415 拦截块命中数=%d（应为2：create-order 与 get-pay-params）' % n1
src2 = src.replace(OLD_1, NEW_1)
assert src2.count('S511-20260921') >= 2, '替换结果异常'
assert src2.count("('AlipayClient' in _ua415)") == 0, 'S415 的 UA 判断没删干净'

shutil.copy2(P, P + '.bak_s511')
open(P, 'w', encoding='utf-8').write(src2)
after = hashlib.md5(src2.encode('utf-8')).hexdigest()
py_compile.compile(P, doraise=True)

print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('S415 旧条件残留=%d（应为0）  S511 标记=%d' % (src2.count("('AlipayClient' in _ua415)"), src2.count('S511-20260921')))
