#!/usr/bin/env python3
"""微信支付投诉自动处理 - 服务器本地运行"""
import sys, os, json, time, base64, subprocess
sys.path.insert(0, "/home/ubuntu/smart-locker")
import psycopg2, psycopg2.extras, requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

DB_CFG = {"host":"127.0.0.1","user":"locker_admin","password":"locker_pass_2024","dbname":"smart_locker"}
SRC = "/home/ubuntu/smart-locker"
V3_KEY = "lichengju0904LICHENGJU0904libaip"
REPLY_MSG = "您好，您的押金已全额退款，请注意查收。如有疑问请拨打客服电话4006981080。"

def sign_req(method, url_path, body_str, mch_id, key_path, cert_path):
    with open(key_path) as f:
        pk = serialization.load_pem_private_key(f.read().encode(), password=None, backend=default_backend())
    r = subprocess.run(["openssl","x509","-in",cert_path,"-serial","-noout"], capture_output=True, text=True)
    serial = r.stdout.strip().split("=")[1]
    ts = str(int(time.time()))
    nonce = "complaint_check"
    msg = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body_str}\n"
    sig = base64.b64encode(pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    auth = f'WECHATPAY2-SHA256-RSA2048 mch_id="{mch_id}",nonce_str="{nonce}",timestamp="{ts}",serial_no="{serial}",signature="{sig}"'
    return {"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"}

def v3_get(url_path, mch_id, key_path, cert_path):
    h = sign_req("GET", url_path, "", mch_id, key_path, cert_path)
    return requests.get(f"https://api.mch.weixin.qq.com{url_path}", headers=h, timeout=15)

def v3_post(url_path, body, mch_id, key_path, cert_path):
    bs = json.dumps(body, ensure_ascii=False)
    h = sign_req("POST", url_path, bs, mch_id, key_path, cert_path)
    return requests.post(f"https://api.mch.weixin.qq.com{url_path}", headers=h, data=bs.encode(), timeout=15)

def get_wxpay(mch_id):
    """通过helpers获取WxPay实例"""
    os.environ["PGPASSWORD"] = DB_CFG["password"]
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor()
    c.execute("SELECT id, api_key, cert_name, api_v3_key FROM payment_channels WHERE mch_id=%s AND is_active=1", (mch_id,))
    ch = c.fetchone()
    conn.close()
    if not ch:
        # fallback: try any active channel
        conn = psycopg2.connect(**DB_CFG)
        c = conn.cursor()
        c.execute("SELECT id, api_key, cert_name, api_v3_key FROM payment_channels WHERE cert_name IS NOT NULL AND cert_name != '' ORDER BY id LIMIT 1")
        ch = c.fetchone()
        conn.close()
    return ch

def do_refund(order_no, total_fee, mch_id):
    """执行退款"""
    sys.path.insert(0, SRC)
    from helpers import get_channel_wxpay
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT id, order_no, deposit_amount, refund_status, payment_channel_id FROM orders WHERE order_no=%s", (order_no,))
    order = c.fetchone()
    if not order:
        conn.close()
        return False, "订单不存在"
    if order["refund_status"] == "refunded":
        conn.close()
        return True, "已退款"
    ch_id = order["payment_channel_id"]
    wx, ch_type = get_channel_wxpay(conn, channel_id=ch_id)
    if not wx:
        conn.close()
        return False, f"无法获取WxPay(channel={ch_id})"
    amount = int(order["deposit_amount"] * 100) if order["deposit_amount"] else 0
    if amount <= 0:
        amount = total_fee
    out_refund = f"RF-{order['order_no']}"
    try:
        result = wx.refund(order["order_no"], amount, amount, out_refund_no=out_refund, refund_desc="押金退款-投诉自动处理")
    except Exception as e:
        conn.close()
        return False, f"退款异常: {e}"
    if result and result.get("return_code") == "SUCCESS" and result.get("result_code") == "SUCCESS":
        refund_id = result.get("refund_id", "")
        c.execute("UPDATE orders SET refund_status='refunded', refund_id=%s WHERE id=%s", (refund_id, order["id"]))
        conn.commit()
        conn.close()
        return True, refund_id
    else:
        err = result.get("err_code_des", "") if result else "无响应"
        conn.close()
        return False, err

def process_complaint(complaint, mch_id, key_path, cert_path):
    cid = complaint.get("complaint_id","")
    order_info = complaint.get("complaint_order_info", [])
    order_no = complaint.get("out_trade_no","") or (order_info[0].get("out_trade_no","") if order_info else "")
    txn_id = order_info[0].get("transaction_id","") if order_info else ""
    amount = complaint.get("complaint_order_info",[{}])[0].get("total_pay_amount", 0) if order_info else 0
    payer_phone = complaint.get("payer_phone","")
    
    print(f"  处理投诉 {cid} | 订单 {order_no}")
    
    # 1. 退款
    ok, msg = do_refund(order_no, amount, mch_id)
    print(f"    退款: {'OK' if ok else 'FAIL'} - {msg}")
    
    # 2. 回复投诉
    reply_body = {"complainted_mchid": mch_id, "response_content": REPLY_MSG}
    rr = v3_post(f"/v3/merchant-service/complaints-v2/{cid}/response", reply_body, mch_id, key_path, cert_path)
    print(f"    回复: HTTP {rr.status_code}")
    
    # 3. 结案
    complete_body = {"complainted_mchid": mch_id, "remark": "已退款"}
    cr = v3_post(f"/v3/merchant-service/complaints-v2/{cid}/complete", complete_body, mch_id, key_path, cert_path)
    print(f"    结案: HTTP {cr.status_code}")
    
    # 4. 记录到complaints表
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor()
    c.execute("SELECT id FROM complaints WHERE wx_complaint_id=%s", (cid,))
    existing = c.fetchone()
    if not existing:
        c.execute("INSERT INTO complaints (wx_complaint_id, order_no, type, content, status, mch_id, user_phone, complaint_type) VALUES (%s,%s,'wechat',%s,3,%s,%s,'wechat')",
                  (cid, order_no, complaint.get("complaint_detail","已处理"), mch_id, payer_phone))
    else:
        c.execute("UPDATE complaints SET status=3 WHERE wx_complaint_id=%s", (cid,))
    conn.commit()
    conn.close()

def main():
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT id, mch_id, cert_name FROM payment_channels WHERE cert_name IS NOT NULL AND cert_name != ''")
    channels = c.fetchall()
    conn.close()
    
    stats = {"checked": len(channels), "pending": 0, "refunded": 0, "replied": 0}
    
    for ch in channels:
        mch_id = ch["mch_id"]
        cert_name = ch["cert_name"]
        key_path = f"{SRC}/cert/{cert_name}_key.pem"
        cert_path = f"{SRC}/cert/{cert_name}_cert.pem"
        
        if not os.path.exists(key_path):
            print(f"[跳过] {mch_id} - 证书不存在")
            continue
        
        # 查询投诉
        from datetime import datetime, timedelta
        _begin = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        _end = datetime.now().strftime("%Y-%m-%d")
        url = f"/v3/merchant-service/complaints-v2?begin_date={_begin}&end_date={_end}&limit=50"
        try:
            resp = v3_get(url, mch_id, key_path, cert_path)
            if resp.status_code != 200:
                print(f"[{mch_id}] 查询失败: {resp.status_code} {resp.text[:200]}")
                continue
            data = resp.json()
            complaints_list = data.get("data", [])
        except Exception as e:
            print(f"[{mch_id}] 查询异常: {e}")
            continue
        
        # 筛选PENDING
        pending = [x for x in complaints_list if x.get("complaint_state") == "PENDING"]
        # 去重: 跳过complaints表已处理的
        conn2 = psycopg2.connect(**DB_CFG)
        c2 = conn2.cursor()
        for p in pending[:]:
            c2.execute("SELECT id FROM complaints WHERE wx_complaint_id=%s AND status::int>=3", (p["complaint_id"],))
            row = c2.fetchone()
            if row:
                pending.remove(p)
        conn2.close()
        
        if not pending:
            continue
        
        print(f"\n[{mch_id}] 发现 {len(pending)} 个待处理投诉")
        stats["pending"] += len(pending)
        
        for complaint in pending:
            try:
                process_complaint(complaint, mch_id, key_path, cert_path)
                stats["refunded"] += 1
                stats["replied"] += 1
            except Exception as e:
                print(f"  处理失败: {e}")
    
    print(f"\n===== 巡检完成 =====")
    print(f"检查商户: {stats['checked']} | 待处理: {stats['pending']} | 已退款: {stats['refunded']} | 已回复: {stats['replied']}")

if __name__ == "__main__":
    main()
