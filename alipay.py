# -*- coding: utf-8 -*-
"""[S251-20260919] 支付宝官方接口封装（不依赖官方 SDK，用 cryptography 实现 RSA2）

支持：
  · 手机网站支付 alipay.trade.wap.pay（H5 支付，页面跳转）
  · 交易查询   alipay.trade.query
  · 交易退款   alipay.trade.refund
  · 交易关闭   alipay.trade.close
  · 单笔转账   alipay.fund.trans.uni.transfer（提现到支付宝账户）
  · 异步通知 RSA2 验签 / 响应签名校验

安全红线：
  · 私钥只在服务端使用，绝不写日志、绝不下发前端
"""
import base64
import json
import re
import time
import urllib.parse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

PROD_GATEWAY = 'https://openapi.alipay.com/gateway.do'
SANDBOX_GATEWAY = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'

# 页面跳转类接口的产品码
PRODUCT_WAP = 'QUICK_WAP_WAY'


def _now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _wrap_pem(body, kind):
    body = re.sub(r'\s+', '', body)
    head, tail = {
        'private_pkcs8': ('-----BEGIN PRIVATE KEY-----', '-----END PRIVATE KEY-----'),
        'private_pkcs1': ('-----BEGIN RSA PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----'),
        'public': ('-----BEGIN PUBLIC KEY-----', '-----END PUBLIC KEY-----'),
    }[kind]
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return head + '\n' + '\n'.join(lines) + '\n' + tail + '\n'


class AlipayClient(object):
    def __init__(self, app_id, private_key, alipay_public_key='', gateway=PROD_GATEWAY,
                 sign_type='RSA2', notify_url='', return_url='', timeout=10):
        self.app_id = str(app_id or '').strip()
        self.gateway = (gateway or PROD_GATEWAY).strip()
        self.sign_type = sign_type or 'RSA2'
        self.notify_url = notify_url or ''
        self.return_url = return_url or ''
        self.timeout = timeout
        self._priv = self._load_private_key(private_key)
        self._pub = self._load_public_key(alipay_public_key) if alipay_public_key else None

    # ---------------- 密钥加载 ----------------
    @staticmethod
    def _load_private_key(raw):
        if not raw:
            raise ValueError('缺少应用私钥')
        raw = raw.strip()
        if raw.startswith('-----'):
            return serialization.load_pem_private_key(raw.encode(), password=None)
        body = re.sub(r'\s+', '', raw)
        last_err = None
        for kind in ('private_pkcs8', 'private_pkcs1'):
            try:
                return serialization.load_pem_private_key(_wrap_pem(body, kind).encode(), password=None)
            except Exception as e:  # noqa
                last_err = e
        raise ValueError('应用私钥格式无法识别: %s' % last_err)

    @staticmethod
    def _load_public_key(raw):
        if not raw:
            return None
        raw = raw.strip()
        if raw.startswith('-----'):
            return serialization.load_pem_public_key(raw.encode())
        body = re.sub(r'\s+', '', raw)
        try:
            return serialization.load_pem_public_key(_wrap_pem(body, 'public').encode())
        except Exception:
            # 兼容 PKCS#1 公钥
            return serialization.load_pem_public_key(
                ('-----BEGIN RSA PUBLIC KEY-----\n' +
                 '\n'.join(body[i:i + 64] for i in range(0, len(body), 64)) +
                 '\n-----END RSA PUBLIC KEY-----\n').encode())

    # ---------------- 签名 / 验签 ----------------
    def sign(self, content):
        data = content.encode('utf-8')
        sig = self._priv.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def verify(self, content, signature):
        if not self._pub:
            return False
        try:
            self._pub.verify(base64.b64decode(signature), content.encode('utf-8'),
                             padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            return False

    @staticmethod
    def _sign_content(params, exclude=('sign',)):
        items = sorted((k, v) for k, v in params.items()
                       if k not in exclude and v is not None and str(v) != '')
        return '&'.join('%s=%s' % (k, v) for k, v in items)

    # ---------------- 请求组装 ----------------
    def _common_params(self, method, notify_url=None, return_url=None):
        p = {
            'app_id': self.app_id,
            'method': method,
            'format': 'json',
            'charset': 'utf-8',
            'sign_type': self.sign_type,
            'timestamp': _now_str(),
            'version': '1.0',
        }
        nu = notify_url if notify_url is not None else self.notify_url
        ru = return_url if return_url is not None else self.return_url
        if nu:
            p['notify_url'] = nu
        if ru:
            p['return_url'] = ru
        return p

    def build_params(self, method, biz_content, notify_url=None, return_url=None):
        p = self._common_params(method, notify_url, return_url)
        # [S287] 必须 ensure_ascii=True：中文若原样发出，requests 会把它 URL 编码，
        #   而签名用的是原始中文字符串，两边对不上 → 支付宝报 isv.invalid-signature。
        #   转成 \uXXXX（纯 ASCII）后，签名内容与实际传输完全一致。
        p['biz_content'] = json.dumps(biz_content, ensure_ascii=True, separators=(',', ':'))
        p['sign'] = self.sign(self._sign_content(p, exclude=('sign',)))
        return p

    def build_page_url(self, method, biz_content, notify_url=None, return_url=None):
        p = self.build_params(method, biz_content, notify_url, return_url)
        return self.gateway + '?' + urllib.parse.urlencode(p)

    def build_page_form(self, method, biz_content, notify_url=None, return_url=None, form_id='alipaysubmit'):
        """页面跳转类接口：返回自动提交的 HTML 表单（前端必须 submit，不能 location.href 拼串）"""
        p = self.build_params(method, biz_content, notify_url, return_url)
        inputs = '\n'.join(
            '<input type="hidden" name="%s" value="%s"/>' %
            (k, str(v).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;'))
            for k, v in p.items())
        return ('<form id="%s" name="%s" method="get" action="%s">\n%s\n</form>\n'
                '<script>document.forms["%s"].submit();</script>' % (form_id, form_id, self.gateway, inputs, form_id))

    def _post(self, method, biz_content, notify_url=None, return_url=None):
        p = self.build_params(method, biz_content, notify_url, return_url)
        r = requests.post(self.gateway, data=p, timeout=self.timeout)
        raw = r.text
        try:
            body = json.loads(raw)
        except Exception:
            raise RuntimeError('支付宝返回非 JSON: %s' % raw[:200])
        node = method.replace('.', '_') + '_response'
        if node not in body:
            for k in body:
                if k.endswith('_response'):
                    node = k
                    break
        data = body.get(node) or {}
        data['_raw_body'] = raw
        data['_node'] = node
        # 响应签名校验（失败只记录，不阻断，便于排查）
        try:
            sign = body.get('sign')
            if sign and self._pub:
                idx = raw.find('"%s"' % node)
                if idx >= 0:
                    start = raw.find('{', idx)
                    depth, end = 0, -1
                    for i in range(start, len(raw)):
                        if raw[i] == '{':
                            depth += 1
                        elif raw[i] == '}':
                            depth -= 1
                            if depth == 0:
                                end = i
                                break
                    if end > start:
                        data['_sign_ok'] = self.verify(raw[start:end + 1], sign)
        except Exception:
            pass
        return data

    @staticmethod
    def ok(resp):
        return str(resp.get('code')) == '10000'

    # ---------------- 业务接口 ----------------
    def wap_pay(self, out_trade_no, total_amount, subject, body='', quit_url='',
                notify_url=None, return_url=None, timeout_express='15m'):
        """手机网站支付：返回 {'url':..., 'form':...}"""
        biz = {
            'out_trade_no': str(out_trade_no),
            'total_amount': '%.2f' % float(total_amount),
            'subject': (subject or '储物柜预付款')[:256],
            'product_code': PRODUCT_WAP,
        }
        if body:
            biz['body'] = str(body)[:128]
        if quit_url:
            biz['quit_url'] = quit_url
        if timeout_express:
            biz['timeout_express'] = timeout_express
        return {
            'method': 'alipay.trade.wap.pay',
            'url': self.build_page_url('alipay.trade.wap.pay', biz, notify_url, return_url),
            'form': self.build_page_form('alipay.trade.wap.pay', biz, notify_url, return_url),
        }

    def trade_create(self, out_trade_no, total_amount, subject, buyer_id,
                     body='', timeout_express='15m', product_code='', op_app_id=''):
        """[S334] 支付宝【小程序支付】创建交易：alipay.trade.create

        与 alipay.trade.wap.pay（H5 手机网站支付）的区别：
          · 小程序支付是「服务端创建交易 → 客户端 my.tradePay({tradeNO}) 拉起收银台」，
            不跳转页面、不需要 form/url；
          · buyer_id 必传 = 买家的支付宝 user_id（本项目 = users.alipay_uid）；
          · 返回的 trade_no 交给前端；真正的付款结果以异步通知
            /api/pay/notify/alipay 为准（未配置回调时可用 alipay.trade.query 核对）。

        注意：biz_content 的 JSON 序列化在 build_params() 里已强制 ensure_ascii=True
              （中文转成反斜杠 u 形式的纯 ASCII 转义）。若改成 ensure_ascii=False，
              requests 会把中文 URL 编码，签名内容与实际传输对不上 →
              支付宝报 isv.invalid-signature（S287 修过的坑）。
        """
        biz = {
            'out_trade_no': str(out_trade_no),
            'total_amount': '%.2f' % float(total_amount),
            'subject': (subject or '储物柜预付款')[:256],
            'buyer_id': str(buyer_id),
        }
        if product_code:
            biz['product_code'] = str(product_code)
        if op_app_id:
            biz['op_app_id'] = str(op_app_id)
        if body:
            biz['body'] = str(body)[:128]
        if timeout_express:
            biz['timeout_express'] = timeout_express
        return self._post('alipay.trade.create', biz)

    def query(self, out_trade_no=None, trade_no=None):
        biz = {}
        if out_trade_no:
            biz['out_trade_no'] = str(out_trade_no)
        if trade_no:
            biz['trade_no'] = str(trade_no)
        return self._post('alipay.trade.query', biz)

    def refund(self, out_trade_no, refund_amount, out_request_no=None, refund_reason='',
               trade_no=None, notify_url=None):
        biz = {
            'refund_amount': '%.2f' % float(refund_amount),
            'out_request_no': str(out_request_no or ('R' + str(int(time.time())))),
        }
        if out_trade_no:
            biz['out_trade_no'] = str(out_trade_no)
        if trade_no:
            biz['trade_no'] = str(trade_no)
        if refund_reason:
            biz['refund_reason'] = str(refund_reason)[:256]
        return self._post('alipay.trade.refund', biz, notify_url=None)

    def close(self, out_trade_no=None, trade_no=None):
        biz = {}
        if out_trade_no:
            biz['out_trade_no'] = str(out_trade_no)
        if trade_no:
            biz['trade_no'] = str(trade_no)
        return self._post('alipay.trade.close', biz)

    def transfer(self, out_biz_no, identity, amount, name='', order_title='余额提现',
                 remark='', identity_type='ALIPAY_LOGON_ID', product_code='TRANS_ACCOUNT_NO_PWD',
                 biz_scene='DIRECT_TRANSFER'):
        """单笔转账到支付宝账户（提现）。金额单位：元"""
        payee = {'identity': str(identity), 'identity_type': identity_type}
        if name:
            payee['name'] = str(name)
        biz = {
            'out_biz_no': str(out_biz_no),
            'trans_amount': '%.2f' % float(amount),
            'product_code': product_code,
            'biz_scene': biz_scene,
            'order_title': str(order_title)[:128],
            'payee_info': payee,
        }
        if remark:
            biz['remark'] = str(remark)[:200]
        return self._post('alipay.fund.trans.uni.transfer', biz)

    # ---------------- 小程序登录（authCode → user_id）----------------
    def oauth_token(self, code, grant_type='authorization_code'):
        """支付宝小程序 authCode 换 user_id

        接口：alipay.system.oauth.token
        注意：本接口的参数是【顶层参数】（grant_type / code），不放在 biz_content，
              所以不能复用 _post。
        返回：{'user_id': '2088...', 'access_token': ..., 'code': '10000', ...}
              出错时返回 {'code': '40004', 'sub_code': ..., 'sub_msg': ...}
        """
        p = self._common_params('alipay.system.oauth.token')
        p['grant_type'] = grant_type
        p['code'] = code
        p['sign'] = self.sign(self._sign_content(p, exclude=('sign',)))
        r = requests.post(self.gateway, data=p, timeout=self.timeout)
        raw = r.text
        try:
            body = json.loads(raw)
        except Exception:
            raise RuntimeError('支付宝返回非 JSON: %s' % raw[:200])
        node = 'alipay_system_oauth_token_response'
        if node not in body:
            for k in body:
                if k.endswith('_response'):
                    node = k
                    break
        data = body.get(node) or {}
        data['_raw_body'] = raw
        data['_node'] = node
        # 响应验签（失败只记录，不阻断）
        try:
            sign = body.get('sign')
            if sign and self._pub:
                idx = raw.find('"%s"' % node)
                if idx >= 0:
                    start = raw.find('{', idx)
                    depth, end = 0, -1
                    for i in range(start, len(raw)):
                        if raw[i] == '{':
                            depth += 1
                        elif raw[i] == '}':
                            depth -= 1
                            if depth == 0:
                                end = i
                                break
                    if end > start:
                        data['_sign_ok'] = self.verify(raw[start:end + 1], sign)
        except Exception:
            pass
        return data

    # ---------------- 通知验签 ----------------
    def verify_notify(self, params):
        """异步通知验签：去除 sign / sign_type / sign_type 参与项后验签"""
        if not self._pub or not params:
            return False
        sign = params.get('sign')
        if not sign:
            return False
        content = self._sign_content(params, exclude=('sign', 'sign_type'))
        return self.verify(content, sign)
