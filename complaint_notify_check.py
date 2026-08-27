# -*- coding: utf-8 -*-
"""
投诉通知URL定期检查脚本
- 遍历所有启用且有证书的商户号
- 若缺 cert.pem 但存在 cert.p12, 自动提取(密码=商户号, legacy模式)
- 查询投诉通知回调URL, 未配置或配置不对则自动修复为系统通知地址
- 结果输出到 logs/complaint_notify_check.log
"""
import sys, os, subprocess, base64, json, time, random, requests, logging
sys.path.insert(0, '/home/ubuntu/smart-locker')
os.chdir('/home/ubuntu/smart-locker')
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

SRC = '/home/ubuntu/smart-locker'
NOTIFY_URL = 'https://locker.cqdyxl.com/api/admin_v2/wechat-complaint/notify'

logging.basicConfig(
    filename=os.path.join(SRC, 'logs', 'complaint_notify_check.log'),
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('notify_check')


def ensure_cert_pem(cert_name):
    """若缺 _cert.pem 但存在 _cert.p12, 自动提取(密码=商户号, legacy模式)"""
    cert_pem = os.path.join(SRC, 'cert', cert_name + '_cert.pem')
    cert_p12 = os.path.join(SRC, 'cert', cert_name + '_cert.p12')
    if os.path.exists(cert_pem) and os.path.getsize(cert_pem) > 0:
        return cert_pem, True
    if not os.path.exists(cert_p12):
        return None, False
    # 尝试提取: 先 legacy(新openssl), 再默认
    for args in (['-legacy'], []):
        cmd = ['openssl', 'pkcs12'] + args + ['-in', cert_p12, '-nokeys', '-clcerts',
               '-passin', 'pass:' + cert_name, '-out', cert_pem]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if os.path.exists(cert_pem) and os.path.getsize(cert_pem) > 0:
                # 校验提取出的证书可用
                chk = subprocess.run(['openssl', 'x509', '-in', cert_pem, '-noout', '-serial'],
                                     capture_output=True, text=True, timeout=15)
                if chk.returncode == 0 and '=' in chk.stdout:
                    log.info('商户 %s 已从p12自动提取cert.pem', cert_name)
                    return cert_pem, True
        except Exception as e:
            log.warning('商户 %s p12提取异常: %s', cert_name, e)
    return None, False


def sign_req(method, url_path, body_str, mch_id, key_path, cert_path):
    with open(key_path) as f:
        pk = serialization.load_pem_private_key(f.read().encode(), password=None, backend=default_backend())
    r = subprocess.run(["openssl", "x509", "-in", cert_path, "-serial", "-noout"], capture_output=True, text=True)
    serial = r.stdout.strip().split("=")[1]
    ts = str(int(time.time()))
    nonce = str(int(time.time())) + str(random.randint(1000, 9999))
    msg = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body_str}\n"
    sig = base64.b64encode(pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    auth = f'WECHATPAY2-SHA256-RSA2048 mchid="{mch_id}",nonce_str="{nonce}",timestamp="{ts}",serial_no="{serial}",signature="{sig}"'
    return {"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"}


def query_notify_url(mch_id, key_path, cert_path):
    h = sign_req("GET", "/v3/merchant-service/complaint-notifications", "", mch_id, key_path, cert_path)
    r = requests.get(f"https://api.mch.weixin.qq.com/v3/merchant-service/complaint-notifications", headers=h, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:300]


def create_notify_url(mch_id, key_path, cert_path):
    body = json.dumps({"url": NOTIFY_URL}, separators=(",", ":"))
    h = sign_req("POST", "/v3/merchant-service/complaint-notifications", body, mch_id, key_path, cert_path)
    r = requests.post(f"https://api.mch.weixin.qq.com/v3/merchant-service/complaint-notifications", data=body, headers=h, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:300]


def main():
    import psycopg2, psycopg2.extras
    from config import DATABASE_URL
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, mch_id, cert_name FROM payment_channels WHERE is_active=1 AND cert_name IS NOT NULL AND cert_name != '' ORDER BY id")
    channels = cur.fetchall()
    conn.close()

    ok_cnt, fixed_cnt, skip_cnt, err_cnt = 0, 0, 0, 0
    for ch in channels:
        mch_id = ch['mch_id']
        cert_name = ch['cert_name']
        key_path = os.path.join(SRC, 'cert', cert_name + '_key.pem')
        cert_path, has_cert = ensure_cert_pem(cert_name)
        if not has_cert or not os.path.exists(key_path):
            skip_cnt += 1
            log.warning('商户 %s 证书文件缺失(key或pem/p12), 跳过', mch_id)
            continue
        try:
            sc, qj = query_notify_url(mch_id, key_path, cert_path)
            if sc == 200 and isinstance(qj, dict) and qj.get('url'):
                cur_url = qj['url']
                if cur_url == NOTIFY_URL:
                    ok_cnt += 1
                else:
                    log.warning('商户 %s 通知URL不符: %s -> 修复为 %s', mch_id, cur_url, NOTIFY_URL)
                    sc2, rj = create_notify_url(mch_id, key_path, cert_path)
                    if sc2 == 200:
                        fixed_cnt += 1
                        log.info('商户 %s 通知URL已修复', mch_id)
                    else:
                        err_cnt += 1
                        log.error('商户 %s 修复失败: %s %s', mch_id, sc2, json.dumps(rj, ensure_ascii=False)[:200])
            elif sc == 404:
                log.info('商户 %s 未配置通知URL, 自动创建', mch_id)
                sc2, rj = create_notify_url(mch_id, key_path, cert_path)
                if sc2 == 200:
                    fixed_cnt += 1
                    log.info('商户 %s 通知URL已创建', mch_id)
                else:
                    err_cnt += 1
                    log.error('商户 %s 创建失败: %s %s', mch_id, sc2, json.dumps(rj, ensure_ascii=False)[:200])
            else:
                err_cnt += 1
                log.error('商户 %s 查询异常 status=%s %s', mch_id, sc, json.dumps(qj, ensure_ascii=False)[:200])
        except Exception as e:
            err_cnt += 1
            log.error('商户 %s 异常: %s', mch_id, e)

    log.info('巡检完成: 正常=%s 修复=%s 跳过=%s 异常=%s', ok_cnt, fixed_cnt, skip_cnt, err_cnt)
    print('done ok=%s fixed=%s skip=%s err=%s' % (ok_cnt, fixed_cnt, skip_cnt, err_cnt))


if __name__ == '__main__':
    main()
