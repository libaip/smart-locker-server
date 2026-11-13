import json, time, requests, base64, os
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from urllib.parse import urlparse, quote

def create_wechatpay_auth(mch_id, cert_serial, private_key_path, method, url, timestamp, nonce, body):
    """Generate WeChat Pay API v3 authorization header"""
    parsed = urlparse(url)
    path = quote(parsed.path, safe='/:=')
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    signature = base64.b64encode(
        private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    ).decode("utf-8")
    return f'WECHATPAY2-SHA256-RSA2048 mchid="{mch_id}",nonce_str="{nonce}",timestamp="{timestamp}",serial="{cert_serial}",signature="{signature}"'
