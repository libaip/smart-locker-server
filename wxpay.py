"""
微信支付工具模块 (APIv2 + MD5签名)
用于智能寄存柜系统的保证金支付和退款
"""

import hashlib
import time
import random
import string
import json
import ssl
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, Optional, Any


class WxPay:
    """微信支付工具类"""
    
    # 沙箱环境开关（生产环境设为 False）
    SANDBOX_MODE = False
    
    # 微信支付接口地址
    UNIFIEDORDER_URL = 'https://api.mch.weixin.qq.com/pay/unifiedorder'
    ORDER_QUERY_URL = 'https://api.mch.weixin.qq.com/pay/orderquery'
    CLOSEORDER_URL = 'https://api.mch.weixin.qq.com/pay/closeorder'
    REFUND_URL = 'https://api.mch.weixin.qq.com/secapi/pay/refund'
    REFUND_QUERY_URL = 'https://api.mch.weixin.qq.com/pay/refundquery'
    DOWNLOAD_BILL_URL = 'https://api.mh.weixin.qq.com/pay/downloadedbill'
    
    # 沙箱环境地址
    SANDBOX_UNIFIEDORDER_URL = 'https://api.mch.weixin.qq.com/sandboxnew/pay/unifiedorder'
    SANDBOX_ORDER_QUERY_URL = 'https://api.mch.weixin.qq.com/sandboxnew/pay/orderquery'
    SANDBOX_REFUND_URL = 'https://api.mch.weixin.qq.com/sandboxnew/pay/refund'
    SANDBOX_SIGN_KEY_URL = 'https://api.mch.weixin.qq.com/sandboxnew/pay/getsignkey'
    
    def __init__(self, mch_id: str, api_key: str, app_id: str, cert_path: str = None, key_path: str = None):
        """
        初始化微信支付
        
        Args:
            mch_id: 商户号
            api_key: API密钥
            app_id: 小程序AppID (H5支付时为微信公众号AppID)
            cert_path: 证书路径（退款时需要）
            key_path: 证书密钥路径（退款时需要）
        """
        self.mch_id = mch_id
        self.api_key = api_key
        self.app_id = app_id
        self.cert_path = cert_path
        self.key_path = key_path
        self.sandbox_sign_key = None
        
        if self.SANDBOX_MODE:
            self._get_sandbox_sign_key()
    
    def _get_sandbox_sign_key(self):
        """获取沙箱环境的签名密钥"""
        url = self.SANDBOX_SIGN_KEY_URL
        nonce_str = self.generate_nonce_str()
        
        sign_params = {
            'mch_id': self.mch_id,
            'nonce_str': nonce_str
        }
        sign_params['sign'] = self.make_sign(sign_params)
        
        xml_data = self.dict_to_xml(sign_params)
        response = self.http_request(url, xml_data, use_cert=False)
        
        if response and response.get('return_code') == 'SUCCESS':
            self.sandbox_sign_key = response.get('sandbox_signkey')
    
    @staticmethod
    def generate_nonce_str(length: int = 32) -> str:
        """生成随机字符串"""
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
    
    @staticmethod
    def generate_out_trade_no() -> str:
        """生成商户订单号"""
        return datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
    
    @staticmethod
    def generate_batch_refund_no() -> str:
        """生成退款批次号"""
        return 'R' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
    
    def make_sign(self, params: Dict[str, Any]) -> str:
        """
        生成签名 (MD5)
        
        Args:
            params: 待签名的参数字典
        
        Returns:
            签名字符串
        """
        # 使用沙箱密钥（如果启用沙箱模式）
        api_key = self.sandbox_sign_key if self.sandbox_sign_key else self.api_key
        
        # 按字典序排序参数
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        # 拼接成字符串
        string_a = '&'.join([f"{k}={v}" for k, v in sorted_params if v is not None and v != ''])
        # 拼接API密钥
        string_sign_temp = f"{string_a}&key={api_key}"
        # MD5签名并转大写
        sign = hashlib.md5(string_sign_temp.encode('utf-8')).hexdigest().upper()
        return sign
    
    def verify_sign(self, params: Dict[str, Any]) -> bool:
        """
        验签
        
        Args:
            params: 包含sign字段的参数字典
        
        Returns:
            验签结果
        """
        if 'sign' not in params:
            return False
        
        sign = params.pop('sign')
        expected_sign = self.make_sign(params)
        params['sign'] = sign  # 恢复sign字段
        
        return sign == expected_sign
    
    @staticmethod
    def dict_to_xml(params: Dict[str, Any]) -> str:
        """字典转XML"""
        xml_parts = ['<xml>']
        for key, value in params.items():
            if isinstance(value, (int, float)):
                xml_parts.append(f'<{key}>{value}</{key}>')
            else:
                # CDATA处理特殊字符
                value_str = str(value)
                if any(c in value_str for c in ['<', '>', '&', '"', "'"]):
                    xml_parts.append(f'<{key}><![CDATA[{value_str}]]></{key}>')
                else:
                    xml_parts.append(f'<{key}><![CDATA[{value_str}]]></{key}>')
        xml_parts.append('</xml>')
        return ''.join(xml_parts)
    
    @staticmethod
    def xml_to_dict(xml_str: str) -> Dict[str, Any]:
        """XML转字典"""
        try:
            root = ET.fromstring(xml_str)
            result = {}
            for child in root:
                result[child.tag] = child.text
            return result
        except Exception as e:
            print(f"XML解析失败: {e}")
            return {}
    
    def http_request(self, url: str, data: str = None, method: str = 'POST', use_cert: bool = True, timeout: int = 30) -> Dict[str, Any]:
        """
        发送HTTP请求
        
        Args:
            url: 请求URL
            data: 请求数据（XML字符串）
            method: 请求方法
            use_cert: 是否使用证书（退款需要）
            timeout: 超时时间（秒）
        
        Returns:
            响应字典
        """
        try:
            headers = {
                'Content-Type': 'application/xml',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            if use_cert and self.cert_path and self.key_path:
                # 使用requests库发送带证书的请求（退款）
                import requests as req_lib
                resp = req_lib.request(method, url, data=data.encode('utf-8') if data else None,
                                       headers=headers, timeout=timeout,
                                       cert=(self.cert_path, self.key_path), verify=False)
                result = resp.content.decode('utf-8')
            else:
                req = urllib.request.Request(url, data=data.encode('utf-8') if data else None, 
                                            headers=headers, method=method)
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, context=context, timeout=timeout) as response:
                    result = response.read().decode('utf-8')
            
            return self.xml_to_dict(result)
            
        except urllib.error.URLError as e:
            print(f"网络请求失败: {e}")
            return {'return_code': 'FAIL', 'return_msg': str(e)}
        except Exception as e:
            print(f"请求异常: {e}")
            return {'return_code': 'FAIL', 'return_msg': str(e)}
    
    def unifiedorder(self, trade_type: str, body: str, total_fee: int, out_trade_no: str, 
                     notify_url: str, openid: str = None, scene_info: str = None, 
                     time_start: str = None, time_expire: str = None) -> Dict[str, Any]:
        """
        统一下单
        
        Args:
            trade_type: 交易类型 JSAPI/Native/H5/App
            body: 商品描述
            total_fee: 总金额（分）
            out_trade_no: 商户订单号
            notify_url: 支付结果通知URL
            openid: 用户openid（JSAPI支付必填）
            scene_info: 场景信息（H5支付）
            time_start: 订单开始时间
            time_expire: 订单过期时间
        
        Returns:
            统一下单结果
        """
        url = self.SANDBOX_UNIFIEDORDER_URL if self.SANDBOX_MODE else self.UNIFIEDORDER_URL
        
        params = {
            'appid': self.app_id,
            'mch_id': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
            'body': body,
            'out_trade_no': out_trade_no,
            'total_fee': total_fee,
            'spbill_create_ip': '106.55.7.10',  # 服务器IP
            'notify_url': notify_url,
            'trade_type': trade_type,
        }
        
        if openid:
            params['openid'] = openid
        
        if scene_info:
            params['scene_info'] = scene_info
        
        if time_start:
            params['time_start'] = time_start
        
        if time_expire:
            params['time_expire'] = time_expire
        
        # 添加签名
        params['sign'] = self.make_sign(params)
        
        # 发送请求
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=False)
        
        return result
    
    def get_jsapi_params(self, prepay_id: str) -> Dict[str, Any]:
        """
        获取JSAPI调起支付的参数
        
        Args:
            prepay_id: 预支付交易会话标识
        
        Returns:
            JSAPI调起参数
        """
        nonce_str = self.generate_nonce_str()
        timestamp = str(int(time.time()))
        
        params = {
            'appId': self.app_id,
            'timeStamp': timestamp,
            'nonceStr': nonce_str,
            'package': f'prepay_id={prepay_id}',
            'signType': 'MD5'
        }
        params['paySign'] = self.make_sign(params)
        
        return params
    
    def get_h5_params(self, mweb_url: str) -> Dict[str, Any]:
        """
        获取H5支付的跳转URL
        
        Args:
            mweb_url: H5支付跳转链接
        
        Returns:
            包含跳转链接的字典
        """
        return {
            'mweb_url': mweb_url
        }
    
    def order_query(self, out_trade_no: str = None, transaction_id: str = None) -> Dict[str, Any]:
        """
        查询订单
        
        Args:
            out_trade_no: 商户订单号
            transaction_id: 微信订单号
        
        Returns:
            查询结果
        """
        url = self.SANDBOX_ORDER_QUERY_URL if self.SANDBOX_MODE else self.ORDER_QUERY_URL
        
        params = {
            'appid': self.app_id,
            'mch_id': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
        }
        
        if out_trade_no:
            params['out_trade_no'] = out_trade_no
        if transaction_id:
            params['transaction_id'] = transaction_id
        
        params['sign'] = self.make_sign(params)
        
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=False)
        
        return result
    
    def close_order(self, out_trade_no: str) -> Dict[str, Any]:
        """
        关闭订单
        
        Args:
            out_trade_no: 商户订单号
        
        Returns:
            关闭结果
        """
        url = self.CLOSEORDER_URL
        
        params = {
            'appid': self.app_id,
            'mch_id': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
            'out_trade_no': out_trade_no,
        }
        
        params['sign'] = self.make_sign(params)
        
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=False)
        
        return result
    
    def refund(self, out_trade_no: str, total_fee: int, refund_fee: int, 
               out_refund_no: str = None, refund_desc: str = None) -> Dict[str, Any]:
        """
        申请退款
        
        Args:
            out_trade_no: 商户订单号
            total_fee: 订单总金额（分）
            refund_fee: 退款金额（分）
            out_refund_no: 商户退款单号
            refund_desc: 退款原因
        
        Returns:
            退款结果
        """
        if not self.cert_path or not self.key_path:
            return {'return_code': 'FAIL', 'return_msg': '证书未配置，无法发起退款'}
        
        url = self.SANDBOX_REFUND_URL if self.SANDBOX_MODE else self.REFUND_URL
        
        if not out_refund_no:
            out_refund_no = self.generate_batch_refund_no()
        
        params = {
            'appid': self.app_id,
            'mch_id': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
            'out_trade_no': out_trade_no,
            'total_fee': total_fee,
            'refund_fee': refund_fee,
            'out_refund_no': out_refund_no,
        }
        
        if refund_desc:
            params['refund_desc'] = refund_desc
        
        params['sign'] = self.make_sign(params)
        
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=True)
        
        return result
    
    def transfer(self, partner_trade_no, openid, amount, desc='withdraw', check_name='NO_CHECK', spbill_create_ip='127.0.0.1'):
        """Transfer money to user's WeChat wallet"""
        if not self.cert_path or not self.key_path:
            return {'return_code': 'FAIL', 'return_msg': 'Certificate not configured'}
        url = 'https://api.mch.weixin.qq.com/mmpaymkttransfers/promotion/transfers'
        params = {
            'mch_appid': self.app_id,
            'mchid': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
            'partner_trade_no': partner_trade_no,
            'openid': openid,
            'check_name': check_name,
            'amount': amount,
            'desc': desc,
            'spbill_create_ip': spbill_create_ip,
        }
        params['sign'] = self.make_sign(params)
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=True)
        return result

    def refund_query(self, out_trade_no: str = None, out_refund_no: str = None) -> Dict[str, Any]:
        """
        查询退款
        
        Args:
            out_trade_no: 商户订单号
            out_refund_no: 商户退款单号
        
        Returns:
            查询结果
        """
        url = self.REFUND_QUERY_URL
        
        params = {
            'appid': self.app_id,
            'mch_id': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
        }
        
        if out_trade_no:
            params['out_trade_no'] = out_trade_no
        if out_refund_no:
            params['out_refund_no'] = out_refund_no
        
        params['sign'] = self.make_sign(params)
        
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=False)
        
        return result
    
    def parse_pay_notify(self, xml_data: str) -> Dict[str, Any]:
        """
        解析支付结果通知
        
        Args:
            xml_data: 通知数据（XML）
        
        Returns:
            解析后的数据字典
        """
        params = self.xml_to_dict(xml_data)
        
        if not params:
            return {'return_code': 'FAIL', 'return_msg': 'XML解析失败'}
        
        # 验证签名
        if not self.verify_sign(params):
            return {'return_code': 'FAIL', 'return_msg': '签名验证失败'}
        
        return params
    
    def parse_refund_notify(self, xml_data: str) -> tuple:
        """
        解析退款结果通知
        
        Args:
            xml_data: 通知数据（XML）
        
        Returns:
            (解密后的数据, 是否成功)
        """
        params = self.xml_to_dict(xml_data)
        
        if not params:
            return None, False
        
        req_info = params.get('req_info')
        if not req_info:
            return params, True  # 旧版退款通知不需要解密
        
        # 解密退款通知（req_info是加密的）
        # 使用MD5算出的密钥进行AES-ECB解密
        # 这里简化处理，实际生产环境需要使用PyCryptodome等库进行AES解密
        # 暂时返回原始参数
        return params, True
    
    @staticmethod
    def build_pay_notify_response(return_code: str = 'SUCCESS', return_msg: str = 'OK') -> str:
        """
        构建支付通知响应
        
        Args:
            return_code: 返回状态码
            return_msg: 返回信息
        
        Returns:
            XML响应字符串
        """
        params = {
            'return_code': return_code,
            'return_msg': return_msg
        }
        return WxPay.dict_to_xml(params)
    
    @staticmethod
    def calculate_refund_fee(total_fee: float, refund_ratio: float = 1.0) -> int:
        """
        计算退款金额
        
        Args:
            total_fee: 订单总金额（元）
            refund_ratio: 退款比例（0.0-1.0）
        
        Returns:
            退款金额（分，整数）
        """
        refund_amount = total_fee * refund_ratio
        return int(round(refund_amount * 100))


class MockWxPay:
    """模拟微信支付（开发测试用）"""
    
    @staticmethod
    def generate_nonce_str(length: int = 32) -> str:
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
    
    @staticmethod
    def generate_out_trade_no() -> str:
        return 'MOCK' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
    
    @staticmethod
    def unifiedorder(trade_type: str, body: str, total_fee: int, out_trade_no: str,
                    notify_url: str, **kwargs) -> Dict[str, Any]:
        """模拟统一下单"""
        # 模拟成功响应
        return {
            'return_code': 'SUCCESS',
            'result_code': 'SUCCESS',
            'trade_type': trade_type,
            'prepay_id': f'mock_prepay_{out_trade_no}',
            'code_url': f'mock://pay/{out_trade_no}',
            'mweb_url': f'mock://h5pay/{out_trade_no}'
        }
    
    @staticmethod
    def get_jsapi_params(prepay_id: str) -> Dict[str, Any]:
        """模拟JSAPI参数"""
        timestamp = str(int(time.time()))
        nonce_str = MockWxPay.generate_nonce_str()
        return {
            'appId': 'mock_appid',
            'timeStamp': timestamp,
            'nonceStr': nonce_str,
            'package': f'prepay_id={prepay_id}',
            'signType': 'MD5',
            'paySign': 'mock_paysign'
        }
    
    @staticmethod
    def get_h5_params(mweb_url: str) -> Dict[str, Any]:
        """模拟H5支付参数"""
        return {
            'mweb_url': mweb_url or f'mock://h5pay/{MockWxPay.generate_out_trade_no()}'
        }
    
    @staticmethod
    def order_query(out_trade_no: str = None, transaction_id: str = None) -> Dict[str, Any]:
        """模拟查询订单"""
        return {
            'return_code': 'SUCCESS',
            'result_code': 'SUCCESS',
            'trade_state': 'SUCCESS',
            'out_trade_no': out_trade_no,
            'transaction_id': f'TXMock{datetime.now().strftime("%Y%m%d%H%M%S")}',
            'total_fee': 2000,
            'cash_fee': 2000,
            'trade_state_desc': '支付成功'
        }
    
    @staticmethod
    def refund(out_trade_no: str, total_fee: int, refund_fee: int, 
              out_refund_no: str = None, **kwargs) -> Dict[str, Any]:
        """模拟退款"""
        if not out_refund_no:
            out_refund_no = 'R' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
        
        return {
            'return_code': 'SUCCESS',
            'result_code': 'SUCCESS',
            'out_trade_no': out_trade_no,
            'out_refund_no': out_refund_no,
            'refund_id': f'RFMock{datetime.now().strftime("%Y%m%d%H%M%S")}',
            'total_fee': total_fee,
            'refund_fee': refund_fee
        }
    
    @staticmethod
    def parse_pay_notify(xml_data: str) -> Dict[str, Any]:
        """模拟解析支付通知"""
        return {
            'return_code': 'SUCCESS',
            'result_code': 'SUCCESS',
            'out_trade_no': 'mock_order',
            'transaction_id': f'TXMock{datetime.now().strftime("%Y%m%d%H%M%S")}',
            'total_fee': 2000,
            'cash_fee': 2000,
            'trade_state': 'SUCCESS'
        }
    
    @staticmethod
    @staticmethod
    def transfer(partner_trade_no='', openid='', amount=0, **kwargs):
        """Mock transfer to WeChat wallet"""
        return {
            'return_code': 'SUCCESS',
            'result_code': 'SUCCESS',
            'payment_no': 'PMOCK' + datetime.now().strftime('%Y%m%d%H%M%S'),
            'partner_trade_no': partner_trade_no,
        }

    def build_pay_notify_response(return_code: str = 'SUCCESS', return_msg: str = 'OK') -> str:
        """模拟构建响应"""
        return f'<xml><return_code>{return_code}</return_code><return_msg><![CDATA[{return_msg}]]></return_msg></xml>'
# ---------------------------------------------------------------------------
# ThirdPartyPay - 第三方支付平台（虎皮椒/xunhupay）
# 支持微信+支付宝，无需商户号
# ---------------------------------------------------------------------------
import logging as _logging
_logger = _logging.getLogger(__name__)


class ThirdPartyPay:
    """第三方支付平台（虎皮椒/xunhupay）- 支持微信+支付宝，无需商户号"""

    PAY_URL = 'https://api.xunhupay.com/payment/do.html'

    def __init__(self, appid, appsecret, notify_url, return_url=''):
        self.appid = appid
        self.appsecret = appsecret
        self.notify_url = notify_url
        self.return_url = return_url

    def _make_hash(self, params):
        """生成签名: MD5(按key升序拼接参数 + appsecret)"""
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        string_a = '&'.join([f"{k}={v}" for k, v in sorted_params if v is not None and str(v) != ''])
        string_sign_temp = f"{string_a}{self.appsecret}"
        import hashlib as _hl
        return _hl.md5(string_sign_temp.encode('utf-8')).hexdigest().upper()

    def unifiedorder(self, trade_type='wechat', body='', total_fee=0, out_trade_no='',
                     notify_url=None, return_url=None, openid=None, **kwargs):
        """
        统一下单
        trade_type: 'wechat' 或 'alipay'
        total_fee: 金额（分）
        """
        import time as _time
        params = {
            'version': '1.1',
            'appid': self.appid,
            'trade_order_id': out_trade_no,
            'total_fee': total_fee,
            'title': body,
            'time': int(_time.time()),
            'notify_url': notify_url or self.notify_url,
            'nonce_str': WxPay.generate_nonce_str(),
            'type': trade_type,
        }
        if return_url or self.return_url:
            params['return_url'] = return_url or self.return_url
        if openid:
            params['openid'] = openid

        params['hash'] = self._make_hash(params)

        import urllib.parse as _uparse
        import urllib.request as _urequest
        try:
            form_data = _uparse.urlencode(params).encode('utf-8')
            req = _urequest.Request(self.PAY_URL, data=form_data,
                                    headers={'User-Agent': 'SmartLocker/1.0'})
            with _urequest.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            if result.get('errcode') == 0:
                return {
                    'return_code': 'SUCCESS',
                    'result_code': 'SUCCESS',
                    'trade_type': 'third_party',
                    'url_qrcode': result.get('url_qrcode', ''),
                    'url': result.get('url', ''),
                    'order_id': result.get('order_id', ''),
                }
            else:
                return {
                    'return_code': 'FAIL',
                    'result_code': 'FAIL',
                    'return_msg': result.get('errmsg', '第三方下单失败'),
                    'errcode': result.get('errcode', -1)
                }
        except Exception as e:
            _logger.error(f"[第三方支付] 下单异常: {e}")
            return {'return_code': 'FAIL', 'return_msg': str(e)}

    def refund(self, out_trade_no='', total_fee=0, refund_fee=0, **kwargs):
        """第三方支付退款（虎皮椒通过管理后台操作，API不直接支持）"""
        return {
            'return_code': 'FAIL',
            'return_msg': '第三方支付退款请在平台后台操作'
        }

    def transfer(self, partner_trade_no='', openid='', amount=0, **kwargs):
        """Third-party transfer (not supported via API)"""
        return {'return_code': 'FAIL', 'return_msg': 'Transfer via platform backend'}

    def verify_notify(self, params):
        """验证回调签名"""
        if 'hash' not in params:
            return False
        received_hash = params.pop('hash')
        expected_hash = self._make_hash(params)
        params['hash'] = received_hash
        return received_hash == expected_hash

    @staticmethod
    def get_jsapi_params(prepay_id=''):
        return {}

    @staticmethod
    def build_pay_notify_response(return_code='SUCCESS', return_msg='OK'):
        return 'success' if return_code == 'SUCCESS' else 'fail'