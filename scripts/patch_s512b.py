# -*- coding: utf-8 -*-
"""
S512b-20260921 支付宝 H5 支付（第一步·后端选通道）
==================================================
问题：helpers.get_payment_params() 里发起支付时【只挑 channel_type='wechat' 的通道】
      （S317 当时的考虑：发起微信支付绝不能被选到支付宝渠道）。
      结果：用户在【支付宝内置浏览器】里打开 H5 时，也只会拿到微信通道 →
      后面那段 `ch_type == 'alipay'` 的支付宝下单代码永远不会执行（死代码）。

改法：只在【支付宝浏览器】里改成挑 alipay 通道；其它情况一字不变。
      · 支付宝通道存在 → 走 wap_pay（手机网站支付），返回 {'mode':'alipay','pay_url','form'}
      · 支付宝通道不存在 → 记 WARNING 并回退微信通道（保持原行为，不至于直接报错）

配套（在其它文件里，本次不改）：
  · 支付宝客户端已带 notify_url=/api/pay/notify/alipay、return_url=<h5_base>/store
  · 通道 120「支付宝-H5(2021006197675152)」已建并启用（cert_name=alipay_prod_user）
"""
import hashlib
import py_compile
import shutil

P = 'helpers.py'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S512b-20260921' not in src, '已打过 S512b，中止'

OLD = """    else:
        # [S317] 这里是要【发起微信支付】，必须只在 wechat 渠道里选，绝不能选到支付宝渠道
        current_channel = _get_payment_channel(channel_type='wechat')  # 自动选活跃的微信渠道"""

NEW = """    else:
        # [S317] 这里是要【发起微信支付】，必须只在 wechat 渠道里选，绝不能选到支付宝渠道
        # [S512b-20260921] 例外：在【支付宝内置浏览器】里必须挑支付宝通道。
        #   否则支付宝用户永远拿到的是微信通道 -> 下面 ch_type=='alipay' 那段成了死代码，
        #   用户在支付宝里根本付不了钱。支付宝通道不存在时回退微信通道（保持原行为）。
        if is_alipay_browser():
            current_channel = _get_payment_channel(channel_type='alipay')
            if current_channel:
                logger.info('[支付宝] 支付宝浏览器：选用支付宝通道 id=%s appid=%s',
                            current_channel.get('id'), current_channel.get('app_id'))
            else:
                logger.warning('[支付宝] 支付宝浏览器但没有可用的支付宝通道，回退微信通道')
                current_channel = _get_payment_channel(channel_type='wechat')
        else:
            current_channel = _get_payment_channel(channel_type='wechat')  # 自动选活跃的微信渠道"""

n = src.count(OLD)
assert n == 1, '锚点命中数=%d（应为1）' % n
src2 = src.replace(OLD, NEW, 1)
assert src2.count('S512b-20260921') == 1

shutil.copy2(P, P + '.bak_s512b')
open(P, 'w', encoding='utf-8').write(src2)
after = hashlib.md5(src2.encode('utf-8')).hexdigest()
py_compile.compile(P, doraise=True)

print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('is_alipay_browser 调用点数量 =', src2.count('is_alipay_browser()'))
