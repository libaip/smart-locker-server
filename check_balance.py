# -*- coding: utf-8 -*-
"""只读验证：调用线上余额查询函数，不修改任何数据。"""
import os
import sys
import threading

SRV = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRV)

import config  # noqa: E402

_real_start = threading.Thread.start
threading.Thread.start = lambda self, *a, **k: None
try:
    import routes.admin_v2 as admin_mod  # noqa: E402
finally:
    threading.Thread.start = _real_start

import psycopg2  # noqa: E402
import requests  # noqa: E402
import base64  # noqa: E402
import time  # noqa: E402
import hashlib  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402


def query_balance(mch_id, cert_serial, key_path, account_type):
    url_path = '/v3/merchant/fund/balance/' + account_type
    timestamp = str(int(time.time()))
    nonce_str = os.urandom(16).hex()
    sign_str = 'GET\n' + url_path + '\n' + timestamp + '\n' + nonce_str + '\n\n'
    with open(key_path, 'r') as f:
        private_key = f.read()
    key_obj = serialization.load_pem_private_key(private_key.encode(), password=None)
    signature = key_obj.sign(sign_str.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
    sign_b64 = base64.b64encode(signature).decode('utf-8')
    authorization = 'WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",timestamp="%s",serial_no="%s",signature="%s"' % (
        mch_id, nonce_str, timestamp, cert_serial, sign_b64)
    resp = requests.get('https://api.mch.weixin.qq.com' + url_path, headers={
        'Accept': 'application/json',
        'Authorization': authorization,
    }, timeout=8)
    return resp.status_code, resp.text[:300]


def query_balance_v2(mch_id, api_key, cert_path, key_path):
    nonce = os.urandom(16).hex()
    params = {'mch_id': mch_id, 'nonce_str': nonce}
    sign_str = '&'.join('{}={}'.format(k, params[k]) for k in sorted(params)) + '&key=' + api_key
    params['sign'] = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()
    body = '<xml>' + ''.join('<{0}><![CDATA[{1}]]></{0}>'.format(k, v) for k, v in params.items()) + '</xml>'
    resp = requests.post('https://api.mch.weixin.qq.com/mmpaymkttransfers/query_balance',
                         data=body.encode('utf-8'), cert=(cert_path, key_path), timeout=8)
    return resp.status_code, resp.text[:400]

conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=8)
cur = conn.cursor()
cur.execute("SELECT mch_id, cert_serial_no, api_key FROM payment_channels WHERE channel_type='wechat' AND mch_id IN ('1749440470') ORDER BY id")
rows = cur.fetchall()
conn.close()

for mch_id, cert_serial, api_key in rows:
    key_path = os.path.join(os.path.dirname(config.WX_KEY_PATH), str(mch_id) + '_key.pem')
    cert_path = os.path.join(os.path.dirname(config.WX_CERT_PATH), str(mch_id) + '_cert.pem')
    if not cert_serial or not os.path.exists(key_path):
        print(mch_id, 'SKIP no cert/key', key_path)
        continue
    for acct in ('BASIC', 'OPERATION', 'FEES'):
        try:
            status, text = query_balance(str(mch_id), cert_serial, key_path, acct)
            print(mch_id, acct, status, text[:200])
        except Exception as e:
            print(mch_id, acct, 'ERR', str(e)[:200])
    try:
        status, text = query_balance_v2(str(mch_id), api_key, cert_path, key_path)
        print(mch_id, 'V2_QUERY_BALANCE', status, text[:300])
    except Exception as e:
        print(mch_id, 'V2_QUERY_BALANCE', 'ERR', str(e)[:200])
print('CHECK_DONE')
