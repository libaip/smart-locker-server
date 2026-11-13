#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S1551-20261027] 微信支付投诉拉取 - 加限流保护

背景（2026-10-26 发现）：
  微信 /v3/merchant-service/complaints-v2 有严格频率限制。
  原脚本遍历 156 个商户号、中间零间隔，导致：
    · 日志累计 429 FREQUENCY_LIMIT_EXCEED 出现 7396 次
    · 排在后面的商户号（如 1002365805）永远查不到
    · 表现为"该商户号 0 投诉"—— 其实是根本没拉
  证据：logs/complaint_auto.log 里 [1002365805] 出现 11 次全是 429。

本版改动：
  1) 商户号之间 sleep 0.6~1.0 秒（随机抖动，避免同步撞墙）
  2) 429 时按指数退避重试（8s/16s/24s，最多 3 次）
  3) 商户号按"最近 24h 是否有订单"排序：有订单的优先查
  4) 上一轮失败的商户号本轮优先重查（失败名单存 /tmp）
  5) 全局时间预算 4 分钟：超时就停，剩下的下轮继续
     （cron 是 */5，留 1 分钟余量）

职责不变：仅把微信侧 PENDING/PROCESSING 且本地未登记的投诉补登记进
complaints 表（status=0）。不退款、不回复、不结案。
"""
import sys, os, json, time, base64, subprocess, requests, random
sys.path.insert(0, "/home/ubuntu/smart-locker")
import psycopg2, psycopg2.extras
from datetime import datetime, timedelta
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

DB_CFG = {"host": "127.0.0.1", "port": 6432, "user": "locker_admin",
          "password": "locker_pass_2024", "dbname": "smart_locker"}
SRC = "/home/ubuntu/smart-locker"

# [S1551] 限流相关参数
SLEEP_BETWEEN_MCH = (0.6, 1.0)      # 每个商户号之间的间隔（随机抖动）
RETRY_429_BACKOFF = [8, 16, 24]     # 429 退避秒数
MAX_RETRY = 3
TIME_BUDGET_SEC = 240               # 全局时间预算（cron */5，留 1 分钟）
FAILED_CACHE = "/tmp/complaint_auto_failed.json"

_T0 = time.time()


def _budget_left():
    return TIME_BUDGET_SEC - (time.time() - _T0)


def sign_req(method, url_path, body_str, mch_id, key_path, cert_path):
    with open(key_path) as f:
        pk = serialization.load_pem_private_key(f.read().encode(), password=None,
                                                backend=default_backend())
    r = subprocess.run(["openssl", "x509", "-in", cert_path, "-serial", "-noout"],
                       capture_output=True, text=True)
    serial = r.stdout.strip().split("=")[1]
    ts = str(int(time.time()))
    nonce = str(int(time.time())) + str(random.randint(1000, 9999))
    msg = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body_str}\n"
    sig = base64.b64encode(pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    auth = (f'WECHATPAY2-SHA256-RSA2048 mchid="{mch_id}",nonce_str="{nonce}",'
            f'timestamp="{ts}",serial_no="{serial}",signature="{sig}"')
    return {"Authorization": auth, "Content-Type": "application/json",
            "Accept": "application/json"}


def v3_get(url_path, mch_id, key_path, cert_path):
    h = sign_req("GET", url_path, "", mch_id, key_path, cert_path)
    return requests.get(f"https://api.mch.weixin.qq.com{url_path}", headers=h, timeout=20)


def v3_get_with_retry(url_path, mch_id, key_path, cert_path):
    """[S1551] 带 429 退避重试的 GET。返回 (resp, ok)"""
    last = None
    for attempt in range(MAX_RETRY + 1):
        try:
            resp = v3_get(url_path, mch_id, key_path, cert_path)
            last = resp
            if resp.status_code == 429:
                if attempt < MAX_RETRY:
                    wait = RETRY_429_BACKOFF[min(attempt, len(RETRY_429_BACKOFF) - 1)]
                    # 预算不够就别等了，直接留给下一轮
                    if _budget_left() < wait + 20:
                        print(f"[{mch_id}] 429 但预算不足，留到下一轮")
                        return resp, False
                    print(f"[{mch_id}] 429 限流，等待 {wait}s 后重试 ({attempt + 1}/{MAX_RETRY})")
                    time.sleep(wait)
                    continue
                print(f"[{mch_id}] 429 重试耗尽，本轮跳过")
                return resp, False
            return resp, True
        except Exception as e:
            last = None
            if attempt < MAX_RETRY:
                wait = 3 * (attempt + 1)
                if _budget_left() < wait + 20:
                    return None, False
                print(f"[{mch_id}] 请求异常 {e}，{wait}s 后重试 ({attempt + 1}/{MAX_RETRY})")
                time.sleep(wait)
                continue
            print(f"[{mch_id}] 请求异常重试耗尽: {e}")
            return None, False
    return last, False


def register_complaint(complaint, mch_id):
    cid = complaint.get("complaint_id", "")
    order_info = complaint.get("complaint_order_info", [])
    order_no = complaint.get("out_trade_no", "") or (
        order_info[0].get("out_trade_no", "") if order_info else "")
    payer_phone = complaint.get("payer_phone", "")
    detail = complaint.get("complaint_detail", "") or "微信投诉"
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor()
    c.execute("SELECT id FROM complaints WHERE wx_complaint_id=%s", (cid,))
    if c.fetchone():
        conn.close()
        print(f"    已存在，跳过: {cid}")
        return False
    c.execute(
        "INSERT INTO complaints (wx_complaint_id, order_no, type, content, status, mch_id, "
        "user_phone, complaint_type) VALUES (%s,%s,'wechat',%s,0,%s,%s,'wechat')",
        (cid, order_no, detail, mch_id, payer_phone))
    conn.commit()
    conn.close()
    print(f"    已登记待处理: {cid} | 订单 {order_no or '(无订单号)'} | 商户 {mch_id}")
    return True


def pull_complaints_paginated(mch_id, key_path, cert_path, days=3):
    """分页拉取投诉。返回 (items, ok)。ok=False 表示被限流/失败，本轮没拉到。"""
    all_items = []
    _begin = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    _end = datetime.now().strftime("%Y-%m-%d")
    offset = 0
    limit_val = 50
    for _pg in range(20):
        url = (f"/v3/merchant-service/complaints-v2?begin_date={_begin}"
               f"&end_date={_end}&limit={limit_val}&offset={offset}")
        resp, ok = v3_get_with_retry(url, mch_id, key_path, cert_path)
        if resp is None:
            return all_items, False
        if resp.status_code == 400 and '数据量过大' in resp.text:
            limit_val = max(5, limit_val // 2)
            url = (f"/v3/merchant-service/complaints-v2?begin_date={_begin}"
                   f"&end_date={_end}&limit={limit_val}&offset={offset}")
            resp, ok = v3_get_with_retry(url, mch_id, key_path, cert_path)
            if resp is None:
                return all_items, False
        if resp.status_code != 200:
            print(f"[{mch_id}] 查询失败: {resp.status_code} {resp.text[:160]}")
            return all_items, False
        data = resp.json()
        items = data.get("data", [])
        total = data.get("total_count") or data.get("total", 0)
        all_items.extend(items)
        if not items or len(all_items) >= (total or len(all_items)):
            break
        offset += len(items)
    return all_items, True


def _load_failed():
    try:
        with open(FAILED_CACHE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_failed(s):
    try:
        with open(FAILED_CACHE, "w") as f:
            json.dump(sorted(s), f)
    except Exception:
        pass


def main():
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    order_map = {}
    try:
        c.execute("""
            SELECT pc.mch_id,
                   COUNT(o.id) FILTER (WHERE o.store_time >= NOW() - INTERVAL '24 hours') AS o24
            FROM payment_channels pc
            LEFT JOIN orders o ON o.payment_channel_id = pc.id
            WHERE pc.cert_name IS NOT NULL AND pc.cert_name != ''
            GROUP BY pc.mch_id
        """)
        for r in c.fetchall():
            order_map[str(r["mch_id"] or "").strip()] = int(r["o24"] or 0)
    except Exception as e:
        print(f"[排序查询失败，按原顺序] {e}")

    c.execute("SELECT id, mch_id, cert_name FROM payment_channels "
              "WHERE cert_name IS NOT NULL AND cert_name != ''")
    channels = c.fetchall()
    conn.close()

    # [S1551] 排序：上轮失败优先，其次近24h有订单多的优先
    failed = _load_failed()
    def sort_key(ch):
        m = str(ch["mch_id"] or "").strip()
        return (0 if m in failed else 1, -order_map.get(m, 0), m)
    channels = sorted(channels, key=sort_key)

    stats = {"checked": 0, "pending": 0, "registered": 0,
             "skipped_cert": 0, "ratelimited": 0, "deferred": 0}
    failed_now = set()

    print(f"\n===== 开始巡检 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"商户数={len(channels)} 上轮失败={len(failed)} =====")

    for idx, ch in enumerate(channels):
        if _budget_left() < 15:
            rest = len(channels) - idx
            print(f"[预算用尽] 还剩 {rest} 个商户号留到下一轮")
            stats["deferred"] = rest
            break

        mch_id = str(ch["mch_id"] or "").strip()
        cert_name = ch["cert_name"]
        key_path = f"{SRC}/cert/{cert_name}_key.pem"
        cert_path = f"{SRC}/cert/{cert_name}_cert.pem"

        if not os.path.exists(key_path) or not os.path.exists(cert_path):
            stats["skipped_cert"] += 1
            continue

        stats["checked"] += 1
        complaints_list, ok = pull_complaints_paginated(mch_id, key_path, cert_path, days=3)

        if not ok:
            stats["ratelimited"] += 1
            failed_now.add(mch_id)
            # 被限流时多等一会儿，别继续撞墙
            time.sleep(random.uniform(2.0, 3.5))
            continue

        if not complaints_list:
            time.sleep(random.uniform(*SLEEP_BETWEEN_MCH))
            continue

        pending = [x for x in complaints_list
                   if x.get("complaint_state") in ("PENDING", "PROCESSING")]
        if not pending:
            time.sleep(random.uniform(*SLEEP_BETWEEN_MCH))
            continue

        conn2 = psycopg2.connect(**DB_CFG)
        c2 = conn2.cursor()
        keep = []
        for p in pending:
            c2.execute("SELECT id FROM complaints WHERE wx_complaint_id=%s",
                       (p["complaint_id"],))
            if not c2.fetchone():
                keep.append(p)
        conn2.close()

        if keep:
            print(f"\n[{mch_id}] 发现 {len(keep)} 个待登记投诉")
            stats["pending"] += len(keep)
            for complaint in keep:
                try:
                    if register_complaint(complaint, mch_id):
                        stats["registered"] += 1
                except Exception as e:
                    print(f"  登记失败: {e}")

        time.sleep(random.uniform(*SLEEP_BETWEEN_MCH))

    # [S1551] 失败名单只记本轮失败的；下一轮它们会被排到最前面先查
    _save_failed(failed_now)

    print(f"\n===== 巡检完成 =====")
    print(f"检查商户: {stats['checked']} | 待登记: {stats['pending']} | "
          f"已登记: {stats['registered']} | 限流跳过: {stats['ratelimited']} | "
          f"证书缺失: {stats['skipped_cert']} | 留给下轮: {stats['deferred']} | "
          f"耗时: {int(time.time() - _T0)}s")


if __name__ == "__main__":
    main()
