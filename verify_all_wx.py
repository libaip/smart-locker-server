import psycopg2, requests, xml.etree.ElementTree as ET, hashlib, random, sys
sys.path.insert(0, "/home/ubuntu/smart-locker")
from config import WX_MCH_ID, WX_API_KEY, WX_CERT_PATH, WX_KEY_PATH, DATABASE_URL

def wx_sign(params, key):
    s = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v) + f"&key={key}"
    return hashlib.md5(s.encode()).hexdigest().upper()

def query_refund(tx_id, mch_id=WX_MCH_ID, api_key=WX_API_KEY):
    url = "https://api.mch.weixin.qq.com/pay/refundquery"
    params = {
        "appid": "wxd85204d0ec930d46",
        "mch_id": mch_id,
        "nonce_str": hashlib.md5(str(random.random()).encode()).hexdigest(),
        "transaction_id": tx_id,
    }
    params["sign"] = wx_sign(params, api_key)
    xml = "<xml>" + "".join(f"<{k}>{v}</{k}>" for k, v in params.items()) + "</xml>"
    r = requests.post(url, data=xml.encode(), cert=(WX_CERT_PATH, WX_KEY_PATH), timeout=10)
    return {c.tag: c.text for c in ET.fromstring(r.text)}

conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
cur = conn.cursor()
cur.execute("SELECT o.transaction_id, w.order_id FROM withdrawal_records w JOIN orders o ON w.order_id=o.id WHERE w.user_phone='18888889999' AND w.status=2 AND o.transaction_id IS NOT NULL ORDER BY w.id")
rows = cur.fetchall()
ok = fail = 0
for tx, oid in rows:
    if not tx:
        fail += 1
        continue
    try:
        r = query_refund(tx)
        rc = r.get("return_code", "?")
        if rc == "SUCCESS" and r.get("refund_count", "0") != "0":
            ok += 1
        else:
            fail += 1
            if fail <= 3:
                print(f"FAIL: order={oid} tx={tx} return_code={rc} err={r.get('err_code','?')} refund_count={r.get('refund_count','0')}")
    except Exception as e:
        fail += 1
        if fail <= 3:
            print(f"ERROR: order={oid} {e}")
print(f"\nTOTAL={ok+fail} OK={ok} FAIL={fail}")
conn.close()
