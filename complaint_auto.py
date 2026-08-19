#!/usr/bin/env python3
"""微信支付投诉兜底登记 - 服务器本地运行

职责（2026-08-19 改造）：仅把微信侧 PENDING 且本地未登记的投诉补登记进 complaints 表（status=0）。
回复/退款/结案统一由应用内调度器（_complaint_scheduler）按三段式处理：
  回调秒回首响 → 5分钟后退款 → 到账通知 → 结案；失败重试3次转人工。
本脚本不退款、不回复、不结案，防止与调度器抢跑。
"""
import sys, os, json, time, base64, subprocess
sys.path.insert(0, "/home/ubuntu/smart-locker")
import psycopg2, psycopg2.extras, requests, random
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

DB_CFG = {"host":"127.0.0.1","port":6432,"user":"locker_admin","password":"locker_pass_2024","dbname":"smart_locker"}
SRC = "/home/ubuntu/smart-locker"
V3_KEY = "lichengju0904LICHENGJU0904libaip"

def sign_req(method, url_path, body_str, mch_id, key_path, cert_path):
    with open(key_path) as f:
        pk = serialization.load_pem_private_key(f.read().encode(), password=None, backend=default_backend())
    r = subprocess.run(["openssl","x509","-in",cert_path,"-serial","-noout"], capture_output=True, text=True)
    serial = r.stdout.strip().split("=")[1]
    ts = str(int(time.time()))
    nonce = str(int(time.time())) + str(random.randint(1000,9999))
    msg = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body_str}\n"
    sig = base64.b64encode(pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    auth = f'WECHATPAY2-SHA256-RSA2048 mchid="{mch_id}",nonce_str="{nonce}",timestamp="{ts}",serial_no="{serial}",signature="{sig}"'
    return {"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"}

def v3_get(url_path, mch_id, key_path, cert_path):
    h = sign_req("GET", url_path, "", mch_id, key_path, cert_path)
    return requests.get(f"https://api.mch.weixin.qq.com{url_path}", headers=h, timeout=15)

def register_complaint(complaint, mch_id):
    """把微信侧待处理投诉登记进本地 complaints 表（status=0），处理交给应用内调度器"""
    cid = complaint.get("complaint_id", "")
    order_info = complaint.get("complaint_order_info", [])
    order_no = complaint.get("out_trade_no", "") or (order_info[0].get("out_trade_no", "") if order_info else "")
    payer_phone = complaint.get("payer_phone", "")
    detail = complaint.get("complaint_detail", "") or "微信投诉"
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor()
    c.execute("SELECT id FROM complaints WHERE wx_complaint_id=%s", (cid,))
    if c.fetchone():
        conn.close()
        print(f"    已存在，跳过: {cid}")
        return
    c.execute(
        "INSERT INTO complaints (wx_complaint_id, order_no, type, content, status, mch_id, user_phone, complaint_type) VALUES (%s,%s,'wechat',%s,0,%s,%s,'wechat')",
        (cid, order_no, detail, mch_id, payer_phone)
    )
    conn.commit()
    conn.close()
    print(f"    已登记待处理: {cid} | 订单 {order_no or '(无订单号)'} | 商户 {mch_id}")

def main():
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT id, mch_id, cert_name FROM payment_channels WHERE cert_name IS NOT NULL AND cert_name != ''")
    channels = c.fetchall()
    conn.close()
    
    stats = {"checked": len(channels), "pending": 0, "registered": 0}
    
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
        _begin = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        _end = datetime.now().strftime("%Y-%m-%d")
        limit_val = 20
        url = f"/v3/merchant-service/complaints-v2?begin_date={_begin}&end_date={_end}&limit={limit_val}"
        try:
            resp = v3_get(url, mch_id, key_path, cert_path)
            if resp.status_code != 200:
                err_text = resp.text[:300]
                print(f"[{mch_id}] 查询失败: {resp.status_code} {err_text}")
                # 数据量过大时循环缩小limit重试
                while resp.status_code == 400 and ('数据量过大' in err_text or '日期跨度' in err_text) and limit_val > 5:
                    limit_val = max(5, limit_val // 2)
                    url = f"/v3/merchant-service/complaints-v2?begin_date={_begin}&end_date={_end}&limit={limit_val}"
                    print(f"[{mch_id}] 缩小limit为{limit_val}重试")
                    resp = v3_get(url, mch_id, key_path, cert_path)
                    err_text = resp.text[:300]
                if resp.status_code != 200:
                    print(f"[{mch_id}] 重试后仍失败: {resp.status_code} {resp.text[:200]}")
                    continue
            data = resp.json()
            complaints_list = data.get("data", [])
        except Exception as e:
            print(f"[{mch_id}] 查询异常: {e}")
            continue
        
        # 筛选PENDING
        pending = [x for x in complaints_list if x.get("complaint_state") == "PENDING"]
        # 去重: 本地已存在（任何状态）则跳过，统一交给调度器处理
        conn2 = psycopg2.connect(**DB_CFG)
        c2 = conn2.cursor()
        for p in pending[:]:
            c2.execute("SELECT id FROM complaints WHERE wx_complaint_id=%s", (p["complaint_id"],))
            row = c2.fetchone()
            if row:
                pending.remove(p)
        conn2.close()
        
        if not pending:
            continue
        
        print(f"\n[{mch_id}] 发现 {len(pending)} 个待登记投诉")
        stats["pending"] += len(pending)
        
        for complaint in pending:
            try:
                register_complaint(complaint, mch_id)
                stats["registered"] += 1
            except Exception as e:
                print(f"  登记失败: {e}")
    
    print(f"\n===== 巡检完成 =====")
    print(f"检查商户: {stats['checked']} | 待登记: {stats['pending']} | 已登记: {stats['registered']}")

if __name__ == "__main__":
    main()
