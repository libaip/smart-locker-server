#!/usr/bin/env python3
import subprocess, sys, time
def run(cmd, t=15):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=t, shell=True, stdin=subprocess.DEVNULL)
    return r.stdout + r.stderr
LH_IP = "106.55.7.10"
CVM_IP = "175.178.156.121"
print("="*60)
print("  Failover: LightHouse -> CVM")
print("  ONLY run when LightHouse is DOWN!")
print("="*60)
print("\n[1/5] Ping LH: " + LH_IP)
r = run("ping -c 2 -W 3 " + LH_IP + " 2>&1", 8)
print("  " + ("Ping OK" if ("1 received" in r or "2 received" in r) else "Ping FAIL"))
ans = input("\nLightHouse是完全宕机状态？(type YES): ")
if ans != "YES": print("Cancelled"); sys.exit(0)
print("\n[2/5] Promoting CVM PG...")
r = run("sudo -u postgres psql -c 'SELECT pg_promote();' 2>&1", 15)
time.sleep(3)
r = run("sudo -u postgres psql -tAc 'SELECT pg_is_in_recovery();' 2>&1", 10)
if "f" in r: print("  Promoted OK")
else: print("  FAILED: " + r.strip()); sys.exit(1)
print("\n[3/5] CDN Change Required")
print("  Tencent Cloud -> EdgeOne -> locker.cqdyxl.com")
print("  Origin: " + LH_IP + " -> " + CVM_IP)
input("  Press Enter after CDN change...")
print("\n[4/5] Verify services")
for n,c in [("Nginx","sudo systemctl is-active nginx"),("Flask","sudo systemctl is-active smart-locker"),("WS","sudo systemctl is-active ws-proxy")]:
    r = run(c,10); print("  " + n + ": OK" if "active" in r else "  " + n + ": FAIL")
print("\n[5/5] Done. CVM is primary.")