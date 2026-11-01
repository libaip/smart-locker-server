# -*- coding: utf-8 -*-
"""核查395条假退款订单的真实微信状态: 已退/未退/交易不存在"""
import sys, os, json, time, base64, csv
sys.path.insert(0, '/home/ubuntu/smart-locker')
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests, psycopg2, psycopg2.extras
import complaint_auto as ca

SRC = '/home/ubuntu/smart-locker'
conn = psycopg2.connect(**ca.DB_CFG)
c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
c.execute("""
SELECT o.id, o.order_no, o.user_phone, o.deposit_amount, o.payment_channel_id,
       pc.mch_id, pc.cert_name, o.refund_status, o.status, o.refund_time, o.transaction_id
FROM orders o LEFT JOIN payment_channels pc ON o.payment_channel_id=pc.id
WHERE o.refund_status='refunded' AND (o.refund_id IS NULL OR o.refund_id='') AND o.status IN (4,5)
ORDER BY pc.mch_id, o.id
""")
rows = c.fetchall()
conn.close()
print("total orders to check:", len(rows))

# 缓存已加载证书的商户
key_cache = {}
def get_key(mch_id, cert_name):
    if not cert_name: return None, None
    k = f"{SRC}/cert/{cert_name}_key.pem"
    if not os.path.exists(k): return None, None
    if mch_id not in key_cache:
        try:
            with open(k) as f:
                pk = serialization.load_pem_private_key(f.read().encode(), password=None)
            key_cache[mch_id] = pk
        except Exception as e:
            key_cache[mch_id] = None
    return key_cache.get(mch_id), k

def wx_query_trade(order_no, mch_id):
    pk, _ = get_key(mch_id, mch_id)
    if pk is None:
        return 'NO_CERT'
    ts = str(int(time.time()))
    nonce = 'chk' + ts[-5:]
    url_path = f'/v3/pay/transactions/out-trade-no/{order_no}?mchid={mch_id}'
    msg = f'GET\n{url_path}\n{ts}\n{nonce}\n\n'
    try:
        sig = base64.b64encode(pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        # serial from cert file
        import subprocess
        cert_path = f"{SRC}/cert/{mch_id}_cert.pem"
        r = subprocess.run(['openssl','x509','-in',cert_path,'-serial','-noout'], capture_output=True, text=True)
        serial_no = r.stdout.strip().split('=')[1]
        auth = f'WECHATPAY2-SHA256-RSA2048 mchid="{mch_id}",nonce_str="{nonce}",timestamp="{ts}",serial_no="{serial_no}",signature="{sig}"'
        resp = requests.get(f'https://api.mch.weixin.qq.com{url_path}', headers={"Authorization": auth, "Content-Type":"application/json"}, timeout=12)
        if resp.status_code == 200:
            return resp.json().get('trade_state', 'UNKNOWN')
        err = resp.text[:200]
        if 'ORDERNOTEXIST' in err or '订单不存在' in err or resp.status_code == 404:
            return 'TX_NOT_FOUND'
        return f'ERR_{resp.status_code}'
    except Exception as e:
        return f'EXC_{str(e)[:60]}'

results = []
for i, r in enumerate(rows):
    oid = r['id']
    ono = r['order_no']
    mch_id = r['mch_id'] or ''
    cert_name = r['cert_name'] or ''
    if not mch_id or not cert_name:
        state = 'NO_MCH'
    else:
        state = wx_query_trade(ono, mch_id)
    results.append({
        'order_id': oid, 'order_no': ono, 'user_phone': r['user_phone'],
        'amount': r['deposit_amount'], 'mch_id': mch_id, 'local_status': r['status'],
        'wx_state': state
    })
    if (i+1) % 20 == 0:
        print(f"  checked {i+1}/{len(rows)}")
    time.sleep(0.15)

# 汇总
from collections import Counter
cnt = Counter(x['wx_state'] for x in results)
print("\n=== 汇总 ===")
for k, v in cnt.most_common():
    print(f"{k}: {v}")

# 写CSV
out = '/tmp/refund_backfill_check_result.csv'
with open(out, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=['order_id','order_no','user_phone','amount','mch_id','local_status','wx_state'])
    w.writeheader()
    w.writerows(results)
print("\n结果已写:", out)
