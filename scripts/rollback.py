#!/usr/bin/env python3
import subprocess, sys, time
def run(cmd, t=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=t, shell=True, stdin=subprocess.DEVNULL)
    return r.stdout + r.stderr
LH_IP = "106.55.7.10"
print("="*60)
print("  Rollback: CVM -> LightHouse")
print("="*60)
print("\n[1/5] Check LH")
r = run("ping -c 2 -W 3 " + LH_IP + " 2>&1", 8)
if "1 received" not in r and "2 received" not in r: print("  LH unreachable"); sys.exit(1)
print("  LH alive")
ans = input("CDN origin back to " + LH_IP + "? (type YES): ")
if ans != "YES": print("Cancelled"); sys.exit(0)
print("\n[2/5] Stop CVM PG")
run("sudo systemctl stop postgresql@14-main", 15)
run("sudo find /var/lib/postgresql/14/main/ -mindepth 1 -delete 2>/dev/null", 30)
print("\n[3/5] pg_basebackup from LH")
run("sudo bash -c 'echo \"*:*:*:replicator:repl_pass_2024\" > /var/lib/postgresql/.pgpass && chmod 600 /var/lib/postgresql/.pgpass && chown postgres:postgres /var/lib/postgresql/.pgpass'", 10)
r = run("PGPASSWORD=repl_pass_2024 pg_basebackup -h " + LH_IP + " -U replicator -D /var/lib/postgresql/14/main -Fp -Xs -P -R 2>&1", 300)
if "completed" not in r: print("  FAIL: " + r[-80:]); sys.exit(1)
c = "primary_conninfo = 'host=" + LH_IP + " port=5432 user=replicator password=repl_pass_2024 sslmode=disable'\n"
c += "recovery_target_timeline = '1'\n"
run("echo '" + c + "' | sudo tee /var/lib/postgresql/14/main/postgresql.auto.conf > /dev/null", 10)
print("\n[4/5] Start PG")
r = run("sudo systemctl start postgresql@14-main; sleep 5", 20)
time.sleep(3)
r = run("sudo -u postgres psql -tAc 'SELECT pg_is_in_recovery();' 2>&1", 10)
print("  Mode: " + ("Standby" if "t" in r else r.strip()))
print("\n[5/5] Verify")
r = run("sudo -u postgres psql -d smart_locker -tAc 'SELECT count(*) FROM users;' 2>&1", 10)
print("  Users: " + r.strip())
print("\nRollback complete.")