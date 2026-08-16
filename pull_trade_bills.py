#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日拉取微信支付交易账单，落库供支付渠道统计使用。

用法：
    python3 pull_trade_bills.py --mch 1749404244 --start 2026-08-14 --end 2026-08-15
    python3 pull_trade_bills.py                       # 全部有证书商户，补到昨日
    python3 pull_trade_bills.py --days 3              # 全部有证书商户，最近3天
"""
import argparse
import base64
import gzip
import os
import sys
import time
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS wechat_trade_bills (
    id BIGSERIAL PRIMARY KEY,
    mch_id VARCHAR(32) NOT NULL,
    bill_date DATE NOT NULL,
    trade_time VARCHAR(32),
    app_id VARCHAR(64),
    wx_order_no VARCHAR(64),
    out_trade_no VARCHAR(64),
    user_id VARCHAR(64),
    trade_type VARCHAR(32),
    trade_state VARCHAR(32),
    bank_type VARCHAR(32),
    currency VARCHAR(16),
    settled_amount NUMERIC(12,2) DEFAULT 0,
    coupon_amount NUMERIC(12,2) DEFAULT 0,
    wx_refund_no VARCHAR(64),
    out_refund_no VARCHAR(64),
    refund_amount NUMERIC(12,2) DEFAULT 0,
    recharge_refund_amount NUMERIC(12,2) DEFAULT 0,
    refund_type VARCHAR(32),
    refund_state VARCHAR(32),
    product_name TEXT,
    merchant_data TEXT,
    fee NUMERIC(12,4) DEFAULT 0,
    fee_rate VARCHAR(16),
    order_amount NUMERIC(12,2) DEFAULT 0,
    apply_refund_amount NUMERIC(12,2) DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wechat_trade_bills
    ON wechat_trade_bills (mch_id, bill_date, out_trade_no, COALESCE(out_refund_no, ''));
"""


def _log(msg):
    print('%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


def _connect():
    conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=8)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _sign_auth(mch_id, cert_serial, key_path, url_path):
    timestamp = str(int(time.time()))
    nonce = os.urandom(16).hex()
    message = 'GET\n%s\n%s\n%s\n\n' % (url_path, timestamp, nonce)
    with open(key_path, 'rb') as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    signature = base64.b64encode(
        private_key.sign(message.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
    ).decode('utf-8')
    return ('WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",timestamp="%s",'
            'serial_no="%s",signature="%s"' % (mch_id, nonce, timestamp, cert_serial, signature))


def _get(url, auth):
    return requests.get(
        url,
        headers={'Accept': 'application/json', 'Authorization': auth},
        timeout=30,
    )


def _num(x, default=0):
    try:
        return float(x)
    except Exception:
        return default


def _clean(x):
    x = (x or '').strip()
    return x or None


def fetch_trade_bill(mch_id, cert_serial, key_path, bill_date):
    """拉取某商户某日交易账单，返回 (ok, rows或错误信息)。"""
    url_path = '/v3/bill/tradebill?bill_date=%s&bill_type=ALL&mchid=%s' % (bill_date, mch_id)
    auth = _sign_auth(mch_id, cert_serial, key_path, url_path)
    resp = _get('https://api.mch.weixin.qq.com' + url_path, auth)
    if resp.status_code != 200:
        return False, resp.text[:300]
    info = resp.json()
    download_url = info.get('download_url')
    if not download_url:
        return False, 'no download_url'
    dl_path = download_url.split('api.mch.weixin.qq.com')[-1]
    dl = _get(download_url, _sign_auth(mch_id, cert_serial, key_path, dl_path))
    if dl.status_code != 200:
        return False, 'download HTTP %s' % dl.status_code
    raw = dl.content
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    text = raw.decode('gbk', errors='replace')
    lines = [line for line in text.splitlines() if line.strip()]
    rows = []
    for line in lines[1:-2]:
        cols = [x.strip('`,').strip() for x in line.split('`')]
        if len(cols) < 27 or not cols[1] or not cols[7]:
            continue
        rows.append(cols)
    return True, rows


def upsert_rows(conn, mch_id, bill_date, rows):
    with conn.cursor() as cur:
        for cols in rows:
            cur.execute("""
                INSERT INTO wechat_trade_bills (
                    mch_id, bill_date, trade_time, app_id, wx_order_no, out_trade_no, user_id,
                    trade_type, trade_state, bank_type, currency, settled_amount, coupon_amount,
                    wx_refund_no, out_refund_no, refund_amount, recharge_refund_amount,
                    refund_type, refund_state, product_name, merchant_data, fee, fee_rate,
                    order_amount, apply_refund_amount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (mch_id, bill_date, out_trade_no, COALESCE(out_refund_no, ''))
                DO UPDATE SET trade_time=EXCLUDED.trade_time, wx_order_no=EXCLUDED.wx_order_no,
                    trade_state=EXCLUDED.trade_state, settled_amount=EXCLUDED.settled_amount,
                    coupon_amount=EXCLUDED.coupon_amount, wx_refund_no=EXCLUDED.wx_refund_no,
                    refund_amount=EXCLUDED.refund_amount, refund_state=EXCLUDED.refund_state,
                    fee=EXCLUDED.fee, order_amount=EXCLUDED.order_amount, updated_at=now()
            """, (
                mch_id, bill_date, cols[1], _clean(cols[2]), cols[6], cols[7], _clean(cols[8]),
                _clean(cols[9]), _clean(cols[10]), _clean(cols[11]), _clean(cols[12]),
                _num(cols[13]), _num(cols[14]), _clean(cols[15]), _clean(cols[16]),
                _num(cols[17]), _num(cols[18]), _clean(cols[19]), _clean(cols[20]),
                cols[21], _clean(cols[22]), _num(cols[23]), _clean(cols[24]),
                _num(cols[25]), _num(cols[26]),
            ))
    conn.commit()


def channels_with_cert(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT mch_id, cert_serial_no, cert_name,
                   COALESCE((SELECT MIN(created_at)::date FROM orders WHERE payment_channel_id=pc.id), pc.created_at::date) AS min_date
            FROM payment_channels pc
            WHERE mch_id IS NOT NULL AND mch_id != ''
              AND cert_name IS NOT NULL AND cert_name != ''
              AND cert_serial_no IS NOT NULL AND cert_serial_no != ''
            ORDER BY pc.id
        """)
        return [dict(r) for r in cur.fetchall()]


def sync_mch(conn, mch_id, cert_serial, cert_name, start_date, end_date):
    key_path = '/home/ubuntu/smart-locker/cert/%s_key.pem' % cert_name
    if not os.path.exists(key_path):
        return {'mch_id': mch_id, 'synced': 0, 'errors': ['no key file: %s' % key_path]}
    synced = 0
    errors = []
    day = start_date
    while day <= end_date:
        ok, result = fetch_trade_bill(mch_id, cert_serial, key_path, day.isoformat())
        if ok:
            upsert_rows(conn, mch_id, day, result)
            synced += 1
            _log('synced %s %s rows=%s' % (mch_id, day.isoformat(), len(result)))
        else:
            errors.append('%s: %s' % (day.isoformat(), str(result)[:150]))
        day += timedelta(days=1)
    return {'mch_id': mch_id, 'synced': synced, 'errors': errors[:20]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mch', default='')
    ap.add_argument('--start', default='')
    ap.add_argument('--end', default='')
    ap.add_argument('--days', type=int, default=0)
    args = ap.parse_args()

    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()

    end_date = date.fromisoformat(args.end) if args.end else date.today()
    start_date = date.fromisoformat(args.start) if args.start else None

    channels = channels_with_cert(conn)
    if args.mch:
        channels = [ch for ch in channels if ch['mch_id'] == args.mch]

    total_ok = 0
    for ch in channels:
        if start_date is None:
            if args.days > 0:
                start = end_date - timedelta(days=args.days)
            else:
                start = ch.get('min_date') or (end_date - timedelta(days=3))
        else:
            start = start_date
        res = sync_mch(conn, ch['mch_id'], ch['cert_serial_no'], ch['cert_name'], start, end_date)
        total_ok += res['synced']
        _log('done %s synced_days=%s errors=%s' % (ch['mch_id'], res['synced'], res['errors'][:3]))
    _log('ALL_DONE total_synced_days=%s channels=%s' % (total_ok, len(channels)))
    conn.close()


if __name__ == '__main__':
    main()
