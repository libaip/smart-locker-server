import sys
sys.path.insert(0, "/home/ubuntu/smart-locker")
from complaint_auto import sign_req, v3_get, DB_CFG, SRC
import psycopg2, os

conn = psycopg2.connect(**DB_CFG)
c = conn.cursor()
c.execute("SELECT mch_id, cert_name FROM payment_channels WHERE cert_name IS NOT NULL AND cert_name != ''")
rows = c.fetchall()
conn.close()
print("total channels:", len(rows))
for mch_id, cert_name in rows:
    key_path = f"{SRC}/cert/{cert_name}_key.pem"
    cert_path = f"{SRC}/cert/{cert_name}_cert.pem"
    if not os.path.exists(key_path):
        continue
    try:
        r = v3_get("/v3/merchant-service/complaint-notifications", mch_id, key_path, cert_path)
        print(mch_id, r.status_code, r.text[:200])
    except Exception as e:
        print(mch_id, "ERR", e)
