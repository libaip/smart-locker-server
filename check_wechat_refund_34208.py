import psycopg2, requests, xml.etree.ElementTree as ET, hashlib, random, sys
from config import DATABASE_URL, WX_APP_ID

conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
cur = conn.cursor()
cur.execute("SELECT o.transaction_id, o.refund_id, o.payment_channel_id, o.order_no, pc.mch_id, pc.api_key, pc.cert_name FROM orders o LEFT JOIN payment_channels pc ON o.payment_channel_id = pc.id WHERE o.id=34208")
row = cur.fetchone()
conn.close()

tx, refund_id, pcid, order_no, mch_id, api_key, cert_name = row
print("order_no=" + str(order_no) + " tx=" + str(tx) + " refund_id=" + str(refund_id) + " mch=" + str(mch_id))

def wx_sign(params, key):
    s = "&".join(str(k) + "=" + str(v) for k, v in sorted(params.items()) if v) + "&key=" + str(key)
    return hashlib.md5(s.encode()).hexdigest().upper()

def call(url, params, key):
    params["sign"] = wx_sign(params, key)
    xml = "<xml>" + "".join("<" + k + ">" + str(v) + "</" + k + ">" for k, v in params.items()) + "</xml>"
    r = requests.post(url, data=xml.encode(), cert=("/home/ubuntu/smart-locker/cert/apiclient_cert.pem", "/home/ubuntu/smart-locker/cert/apiclient_key.pem"), timeout=10)
    return {c.tag: c.text for c in ET.fromstring(r.text)}

params = {
    "appid": WX_APP_ID,
    "mch_id": mch_id,
    "nonce_str": hashlib.md5(str(random.random()).encode()).hexdigest(),
    "out_refund_no": refund_id,
}
result = call("https://api.mch.weixin.qq.com/pay/refundquery", params, api_key)
print("REFUND_QUERY:", result)

params2 = {
    "appid": WX_APP_ID,
    "mch_id": mch_id,
    "nonce_str": hashlib.md5(str(random.random()).encode()).hexdigest(),
    "transaction_id": tx,
}
result2 = call("https://api.mch.weixin.qq.com/pay/refundquery", params2, api_key)
print("ORDER_QUERY:", result2)
