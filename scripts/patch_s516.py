# -*- coding: utf-8 -*-
"""
S516-20260921 修支付宝 scheme 传参（官方格式）+ 带回调地址
=========================================================
官方（支付宝开放文档《小程序 scheme 链接介绍》）：
    alipays://platformapi/startapp?appId=[appId]&page=[page]&query=[query]
  · page：**页面路径原样**（斜杠不编码！），参数可直接跟在 ? 后面，且**参数部分必须 UrlEncode**
          官方示例：pages/index/index?key1%3D1%26key2%3D2
  · query：启动参数 key=value&key=value，**必须整体 UrlEncode**
          官方示例：key1%3Dvalue1%26key2%3Dvalue2

S515 的错：把 `page` 整个 encodeURIComponent -> pages%2Fsubscribe%2Fsubscribe
          -> 页面路径非法，参数全丢（老板实测"手机号没传过去"）。

本次改法：
  1) page 用原样路径 `pages/subscribe/subscribe`，参数走 `query`（整体 UrlEncode）
  2) 参数同时跟在 page 的 ? 后面（官方允许，双保险，任一路径生效即可）
  3) query 里新增 `return_url`：当前 H5 页面地址，供小程序"确认"后跳回
  4) 手机号取值加兜底：currentPhone 为空时直接读输入框
用法: python patch_s516.py <deposit.html>
"""
import hashlib
import shutil
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'static/deposit.html'
# newline='' = 不做换行转换，保证 md5 是【文件字节】的 md5（LF 保持 LF）
src = open(P, encoding='utf-8', newline='').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S516-20260921' not in src, '已打过 S516，中止'

OLD = """        var q = 'source=h5&order_id=' + encodeURIComponent(String(currentOrderId || '')) +
                '&phone=' + encodeURIComponent(currentPhone || '') +
                '&code=' + encodeURIComponent(currentAccessCode || '');
        var u = 'alipays://platformapi/startapp?appId=' + ALIPAY_MP_APPID +
                '&page=' + encodeURIComponent('pages/subscribe/subscribe') +
                '&query=' + encodeURIComponent(q);"""

NEW = """        // [S516] 手机号兜底：currentPhone 为空就直接读输入框
        var _ph = currentPhone || '';
        if (!_ph) { var _pe = document.getElementById('userPhone'); _ph = (_pe && _pe.value) || ''; }
        var _rt = '';
        try { _rt = location.href.split('#')[0]; } catch (e) { _rt = ''; }
        // [S516] 官方格式：query 整体 UrlEncode；page 用【原样路径】（斜杠不编码！）
        var _qk = 'source=h5&order_id=' + encodeURIComponent(String(currentOrderId || '')) +
                  '&phone=' + encodeURIComponent(_ph) +
                  '&code=' + encodeURIComponent(currentAccessCode || '') +
                  '&return_url=' + encodeURIComponent(_rt);
        var _qenc = encodeURIComponent(_qk);
        var u = 'alipays://platformapi/startapp?appId=' + ALIPAY_MP_APPID +
                '&page=pages/subscribe/subscribe?' + _qenc +
                '&query=' + _qenc;"""

assert src.count(OLD) == 1, '锚点=%d' % src.count(OLD)
src = src.replace(OLD, NEW, 1)
assert 'return_url=' in src and 'pages/subscribe/subscribe?' in src

shutil.copy2(P, P + '.bak_s516')
open(P, 'w', encoding='utf-8', newline='').write(src)
after = hashlib.md5(src.encode('utf-8')).hexdigest()
print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print("检查: encodeURIComponent('pages/subscribe/subscribe') 残留=%d（应为0）"
      % src.count("encodeURIComponent('pages/subscribe/subscribe')"))
print('return_url 次数=%d' % src.count('return_url='))
