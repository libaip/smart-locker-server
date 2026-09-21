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

    def refund_query(self, out_trade_no=None, out_request_no=None, trade_no=None):
        """[S541-20260922] 退款查询：alipay.trade.fastpay.refund.query
        官方口径（alipay.trade.refund 文档）：
          "接口返回 fund_change=Y 为退款成功，fund_change=N 或无此字段值返回时需通过退款查询接口
           进一步确认。注意，接口中 code=10000 仅代表本次退款请求成功，不代表退款成功。"
        -> 本方法用于 fund_change 不是 Y 时的复核，按 out_request_no（我们自己的确定性单号）查。
        返回 {'code':'10000','refund_status':'REFUND_SUCCESS'|'REFUND_CLOSED'|...,'refund_amount':..}
        """
        biz = {}
        if out_request_no:
            biz['out_request_no'] = str(out_request_no)
        if out_trade_no:
            biz['out_trade_no'] = str(out_trade_no)
        if trade_no:
            biz['trade_no'] = str(trade_no)
        return self._post('alipay.trade.fastpay.refund.query', biz)
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

    # ---------------- 小程序订阅消息 ----------------
    # [S526-20260921] 支付宝小程序订阅消息（服务端下发）
    #   接口：alipay.open.app.mini.templatemessage.send
    #   与 alipay.trade.create（小程序支付）无关，也和微信 subscribe/send 不共用。
    def mini_template_message_send(self, to_user_id, user_template_id, page, data,
                                   form_id='', dry_run=False):
        """[S526] 支付宝小程序【订阅消息】发送：alipay.open.app.mini.templatemessage.send

        biz_content 参数：
          · to_user_id       必填 = 收件人的支付宝 user_id（本项目 = users.alipay_uid /
                              phone_openids.alipay_uid，由 /api/alipay/login 的 oauth_token 取得）
          · user_template_id 必填 = 商家平台领用的【订阅消息模板ID】（本项目 = wx_templates 里
                              channel='alipay' 的两条：subscribe_general / subscribe_refund）
          · page             必填 = 用户点击消息后跳转的小程序页面（例 pages/mine/mine）
          · data             必填 = 关键词数据，JSON **字符串**，形如
                              {"keyword1":{"value":"..."},"keyword2":{"value":"..."}}
                              关键词名称与顺序必须与申请模板时选的一一对应；
                              个数/名称不匹配 -> 支付宝返回 USER_TEMPLATE_LACK_KEYWORD。

        关于 form_id：老版本文档（表单/交易触达模型）把它写成必填，但【订阅消息】模型不需要，
          默认不传；万一支付宝回 FORM_ID_INVALID 或 isv.missing-parameter:form_id，
          再用 form_id= 传入（参数就是为此保留的）。

        中文与签名（S287 修过的坑，这里同样受益）：
          真正发出的 biz_content 由 build_params() 统一 json.dumps(..., ensure_ascii=True)，
          中文会变成 \\uXXXX 形式的纯 ASCII 转义；签名内容与实际传输内容完全一致。
          **不要**在这里自己 dumps 成明文中文再塞进去。

        dry_run=True：只组装参数并签名，**不发任何网络请求**，返回
          {'method', 'params', 'biz_content_obj', 'sign_content', 'dry_run': True}
          供离线校验签名/编码（S526 验证方案）。
        """
        method = 'alipay.open.app.mini.templatemessage.send'
        to_user_id = str(to_user_id or '').strip()
        user_template_id = str(user_template_id or '').strip()
        if not to_user_id:
            raise ValueError('to_user_id 不能为空')
        if not user_template_id:
            raise ValueError('user_template_id 不能为空')

        # data 允许传 dict（推荐）或已经序列化好的 JSON 字符串
        if isinstance(data, (dict, list)):
            data_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        else:
            data_str = str(data or '').strip()
            if not data_str:
                raise ValueError('data 不能为空')
            try:
                data_str = json.dumps(json.loads(data_str), ensure_ascii=False, separators=(',', ':'))
            except Exception:
                raise ValueError('data 不是合法 JSON: %s' % data_str[:80])

        if len(data_str.encode('utf-8')) > 2048:
            raise ValueError('data 超过 2048 字节（支付宝上限）')

        biz = {
            'to_user_id': to_user_id,
            'user_template_id': user_template_id,
            'page': str(page or '')[:128],
            'data': data_str,
        }
        if form_id:
            biz['form_id'] = str(form_id)

        if dry_run:
            p = self.build_params(method, biz)
            return {'method': method, 'params': p, 'biz_content_obj': biz,
                    'sign_content': self._sign_content(p, exclude=('sign',)),
                    'dry_run': True}
        return self._post(method, biz)

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
