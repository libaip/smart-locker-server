#!/usr/bin/env python3
import os, sys, logging, json, time, requests, base64
os.chdir('/home/ubuntu/smart-locker')
sys.path.insert(0, '.')
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[
    logging.FileHandler('/home/ubuntu/smart-locker/batch_complaints.log'),
    logging.StreamHandler()
])
log = logging.getLogger(__name__)

from app import app
with app.app_context():
    from config import WX_KEY_PATH, WX_CERT_SERIAL_NO
    from database import get_db as _gdb
    from helpers import do_real_refund

    conn = _gdb()
    cur = conn.cursor()
    cur.execute("SELECT wx_complaint_id, order_no, mch_id FROM complaints WHERE status='3'")
    rows = cur.fetchall()
    conn.close()
    log.info("Found %d unprocessed complaints", len(rows))

    for i, row in enumerate(rows[:50]):
        cid, order_no, cmch = row[0], row[1], row[2]
        log.info("[%d/%d] Processing complaint %s order=%s", i+1, min(len(rows),50), cid, order_no)
        try:
            conn2 = _gdb()
            cur2 = conn2.cursor()
            cur2.execute("SELECT id, transaction_id FROM orders WHERE order_no=%s LIMIT 1", (order_no,))
            o = cur2.fetchone()
            conn2.close()
            if not o:
                log.warning("  Order not found: %s", order_no)
                continue
            oid, txid = o[0], o[1] or ''
            from routes.admin_v2 import _auto_refund_complaint_order, _auto_reply_complaint, _auto_complete_complaint
            ok = _auto_refund_complaint_order(order_no, txid, cid)
            log.info("  Refund result: %s", ok[0] if isinstance(ok, tuple) else ok)
            mch = cmch or '1747970416'
            cert_ser = WX_CERT_SERIAL_NO
            key_path = WX_KEY_PATH
            _auto_reply_complaint(cid, order_no, txid, mch_id=mch, cert_serial=cert_ser, private_key_path=key_path)
            _auto_complete_complaint(cid, mch, cert_ser, key_path)
            time.sleep(1)
        except Exception as e:
            log.error("  Failed complaint %s: %s", cid, e)

    log.info("Batch process complete")
