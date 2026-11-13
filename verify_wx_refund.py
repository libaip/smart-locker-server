import psycopg2, requests, xml.etree.ElementTree as ET, hashlib, time, random, json
from config import WX_MCH_ID, WX_API_KEY, WX_CERT_PATH, WX_KEY_PATH, DATABASE_URL

def wx_sign(params, key):
    sign_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v) + f"&key={key}"
    return hashlib.md5(sign_str.encode()).hexdigest().upper()

def query_refund(transaction_id, mch_id=WX_MCH_ID, api_key=WX_API_KEY):
    url = "https://api.mch.weixin.qq.com/pay/refundquery"
    params = {
        "appid": "wxd85204d0ec930d46",
        "mch_id": mch_id,
        "nonce_str": hashlib.md5(str(random.random()).encode()).hexdigest(),
        "transaction_id": transaction_id,
    }
    params["sign"] = wx_sign(params, api_key)
    xml_body = "<xml>" + "".join(f"<{k}>{v}</{k}>" for k, v in params.items()) + "</xml>"
    resp = requests.post(url, data=xml_body.encode(), cert=(WX_CERT_PATH, WX_KEY_PATH), timeout=10)
    root = ET.fromstring(resp.text)
    result = {child.tag: child.text for child in root}
    return result

conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
cur = conn.cursor()
cur.execute("SELECT o.transaction_id, w.order_id, w.amount FROM withdrawal_records w JOIN orders o ON w.order_id=o.id WHERE w.user_phone='18888889999' AND w.status=2 AND w.amount > 0 AND o.transaction_id IS NOT NULL ORDER BY w.id DESC LIMIT 5")
rows = cur.fetchall()
for tx_id, oid, amt in rows:
    if not tx_id:
        print(f"order={oid} NO TXID, skip")
        continue
    try:
        r = query_refund(tx_id)
        refund_count = r.get("refund_count", "0")
        refund_status = r.get("refund_status_0", "N/A")
        refund_amount = r.get("refund_fee_0", "0")
        print(f"order={oid} tx={tx_id} refund_count={refund_count} status={refund_status} amount={refund_amount} wx_return={r.get('return_code','?')}")
    except Exception as e:
        print(f"order={oid} tx={tx_id} ERROR: {e}")
conn.close()
