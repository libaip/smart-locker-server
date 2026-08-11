import os, re, time, datetime, collections

LOG = "/var/log/nginx/access.log"
OFFSET = "/home/ubuntu/smart-locker/logs/nginx_access_offset.txt"
ALERT = "/home/ubuntu/smart-locker/logs/device_traffic_alert.log"
WINDOW = 300  # 5分钟

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def main():
    # 文件轮转检测
    try:
        size = os.path.getsize(LOG)
    except Exception:
        return
    last = 0
    try:
        with open(OFFSET, "r") as f:
            last = int(f.read().strip() or 0)
    except Exception:
        last = 0
    if size < last:
        last = 0  # 轮转，重读
    offset = last
    lines = []
    try:
        with open(LOG, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            lines = f.readlines()
            offset = f.tell()
    except Exception:
        return
    try:
        with open(OFFSET, "w") as f:
            f.write(str(offset))
    except Exception:
        pass

    # 只统计最近 WINDOW 秒
    cutoff = time.time() - WINDOW
    dev_cnt = collections.Counter()
    dev_bytes = collections.Counter()
    total_500 = 0
    for line in lines:
        p = line.split()
        if len(p) < 10:
            continue
        # [01/Aug/2026:22:30:01 +0800]
        m = re.search(r"\[(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})", line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(0)[1:-1], "%d/%b/%Y:%H:%M:%S"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        try:
            status = p[8]
            b = int(p[9])
        except Exception:
            continue
        if status == "500":
            total_500 += 1
        req = p[6]
        if req.startswith(("/api/cabinets/by-mainboard/", "/api/pending-commands/", "/api/active-orders/by-device/")):
            dm = re.search(r"/(\d+)(?:\?|$)", req)
            if dm:
                dev_cnt[dm.group(1)] += 1
                dev_bytes[dm.group(1)] += b

    alerts = []
    # 设备阈值：5分钟内 > 300 次（正常约160）
    for dev, cnt in dev_cnt.items():
        if cnt > 300:
            alerts.append(f"[设备异常] device={dev} 5分钟请求{cnt}次 流量{round(dev_bytes[dev]/1024,1)}KB")
    # 500 阈值：5分钟内 > 10
    if total_500 > 10:
        alerts.append(f"[接口异常] 5分钟500错误{total_500}次")

    if alerts:
        with open(ALERT, "a", encoding="utf-8") as f:
            for a in alerts:
                f.write(f"{now_str()} {a}\n")
        print("\n".join(alerts))
    else:
        print("正常 5分钟请求设备数:", len(dev_cnt), "500:", total_500)

if __name__ == "__main__":
    main()
