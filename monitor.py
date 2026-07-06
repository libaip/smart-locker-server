#!/usr/bin/env python3
import os, subprocess, json, urllib.request, datetime
from config import PUSHPLUS_TOKEN

def send_alert(title, content):
    if not PUSHPLUS_TOKEN: return
    data = json.dumps({"token": PUSHPLUS_TOKEN, "title": title, "content": content}).encode()
    try:
        req = urllib.request.Request("http://www.pushplus.plus/send", data=data,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except: pass

alerts = []
now = datetime.datetime.now()

# gunicorn worker memory
try:
    out = subprocess.check_output(["ps", "aux"], text=True)
    for line in out.split("\n"):
        if "gunicorn" in line and "worker" in line:
            parts = line.split()
            if len(parts) >= 6:
                rss = float(parts[5]) / 1024
                pid = parts[1]
                if rss > 500:
                    alerts.append("Worker %s RSS=%.0fMB > 500MB" % (pid, rss))
except: pass

# nginx 502 (last 5 min)
try:
    log = "/var/log/nginx/error.log"
    r = subprocess.run(["grep", "-c", " 502 ", log], capture_output=True, text=True, timeout=5)
    n = int(r.stdout.strip() or 0)
    if n > 10:
        alerts.append("Nginx 502 errors: %d" % n)
except: pass

# Send-Q
try:
    out = subprocess.check_output(["ss", "-tni", "sport", "=:5001"], text=True, timeout=5)
    for l in out.split("\n"):
        if "Send-Q" in l:
            for w in l.split():
                if w.isdigit() and int(w) > 50:
                    alerts.append("Send-Q >50 on 5001: %s" % w)
except: pass

if alerts:
    send_alert("[monitor] smart locker alert", "\n".join(alerts))
