#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日拉取各微信支付商户号资金账单，保存基本户日终余额。

用法：
    python3 pull_channel_balance.py                 # 拉取昨天
    python3 pull_channel_balance.py --date 2026-08-15
    python3 pull_channel_balance.py --backfill 90   # 从昨天往前找，每个号命中最近一张账单即停
"""
import argparse
import base64
import hashlib
import os
import sys
import time
from datetime import date, timedelta
from urllib.parse import urlparse

import psycopg2
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS payment_channel_balance (
    id BIGSERIAL PRIMARY KEY,
    mch_id VARCHAR(32) NOT NULL,
    balance_date DATE NOT NULL,
    account_type VARCHAR(16) NOT NULL DEFAULT 'BASIC',
    balance NUMERIC(14, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mch_id, balance_date, account_type)
);
"""


def _log(msg):
    print('%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


def _connect():
    return psycopg2.connect(config.DATABASE_URL, connect_timeout=8)


def _sign_auth(mch_id, cert_serial, key_path, method, url_path, body=''):
    timestamp = str(int(time.time()))
    nonce = os.urandom(16).hex()
    message = '%s\n%s\n%s\n%s\n%s\n' % (method, url_path, timestamp, nonce, body)
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


def fetch_bill(mch_id, cert_serial, key_path, bill_date, account_type='BASIC'):
    """拉取某商户某日资金账单，返回 (ok, bytes 或错误文本)。"""
    url_path = '/v3/bill/fundflowbill?bill_date=%s&account_type=%s' % (bill_date, account_type)
    resp = _get(
        'https://api.mch.weixin.qq.com' + url_path,
        _sign_auth(mch_id, cert_serial, key_path, 'GET', url_path),
    )
    if resp.status_code != 200:
        return False, resp.text[:300]
    info = resp.json()
    download_url = info.get('download_url')
    if not download_url:
        return False, 'no download_url'
    pu = urlparse(download_url)
    sign_path = pu.path + ('?' + pu.query if pu.query else '')
    resp2 = _get(download_url, _sign_auth(mch_id, cert_serial, key_path, 'GET', sign_path))
    if resp2.status_code != 200:
        return False, 'download HTTP %s: %s' % (resp2.status_code, resp2.text[:200])
    data = resp2.content
    expect_hash = info.get('hash_value')
    if expect_hash and hashlib.sha1(data).hexdigest() != expect_hash.lower():
        return False, 'sha1 mismatch'
    return True, data


def parse_fundflow_balance(data):
    """解析资金账单，返回最后一条明细的 (账户结余, 记账时间)。"""
    text = data.decode('utf-8-sig', errors='replace')
    lines = text.splitlines()
    if not lines:
        return None, None
    header = [f.strip('`').strip() for f in lines[0].split(',')]
    try:
        balance_idx = header.index('账户结余(元)')
    except ValueError:
        return None, None
    last_balance = None
    last_time = None
    for line in lines[1:]:
        if not line.startswith('`'):
            continue
        # 微信资金账单每行格式为 `字段1,`字段2,...（反引号在字段前，逗号分隔）
        fields = [f.lstrip('`').strip() for f in line.split(',')]
        if len(fields) <= balance_idx:
            continue
        # 跳过末尾的汇总行：首列不是 YYYY-MM-DD 时间
        first = fields[0]
        if len(first) < 10 or first[4:5] != '-':
            continue
        val = fields[balance_idx]
        if val:
            try:
                last_balance = float(val)
                last_time = first
            except ValueError:
                continue
    return last_balance, last_time


def upsert_balance(conn, mch_id, bill_date, balance, account_type='BASIC'):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO payment_channel_balance (mch_id, balance_date, account_type, balance)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (mch_id, balance_date, account_type)
            DO UPDATE SET balance=EXCLUDED.balance, updated_at=now()
        """, (mch_id, bill_date, account_type, balance))
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='账单日期 YYYY-MM-DD，默认昨天')
    parser.add_argument('--backfill', type=int, default=0, help='从昨天往前最多找 N 天，命中即停')
    args = parser.parse_args()

    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT mch_id, cert_serial_no FROM payment_channels
            WHERE channel_type='wechat' AND mch_id IS NOT NULL AND mch_id != ''
              AND cert_serial_no IS NOT NULL AND cert_serial_no != ''
            ORDER BY id
        """)
        channels = [(str(r[0]), r[1]) for r in cur.fetchall()]
    _log('channels=%d' % len(channels))

    today = date.today()
    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.backfill:
        dates = [today - timedelta(days=i) for i in range(1, args.backfill + 1)]
    else:
        dates = [today - timedelta(days=1)]

    ok_count = 0
    no_bill_count = 0
    for mch_id, cert_serial in channels:
        key_path = os.path.join(os.path.dirname(config.WX_KEY_PATH), mch_id + '_key.pem')
        if not os.path.exists(key_path):
            _log('SKIP no key %s' % mch_id)
            continue
        hit = False
        for bill_date in dates:
            try:
                ok, data = fetch_bill(mch_id, cert_serial, key_path, bill_date.isoformat())
            except Exception as e:
                _log('ERR %s %s %r' % (mch_id, bill_date, e))
                continue
            if not ok:
                if 'NO_STATEMENT_EXIST' in str(data):
                    continue
                _log('FAIL %s %s %s' % (mch_id, bill_date, data))
                continue
            balance, biz_time = parse_fundflow_balance(data)
            if balance is None:
                _log('PARSE_NONE %s %s' % (mch_id, bill_date))
                continue
            upsert_balance(conn, mch_id, bill_date.isoformat(), round(balance, 2))
            ok_count += 1
            hit = True
            _log('OK %s %s balance=%s' % (mch_id, bill_date, balance))
            if args.backfill:
                break
        if not hit:
            no_bill_count += 1
    _log('DONE ok=%d no_bill=%d' % (ok_count, no_bill_count))
    conn.close()


if __name__ == '__main__':
    main()
