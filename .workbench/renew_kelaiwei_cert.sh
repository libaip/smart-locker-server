#!/bin/bash
# ============================================================================
# kelaiwei.top 证书续期 -> 同步到 175 生产  (runbook 脚本, 2026-09-10 S123 建立)
# ----------------------------------------------------------------------------
# 背景(为什么需要这个脚本):
#   kelaiwei.top 的证书是在【106 旧机】上用 certbot --standalone 申请的,
#   106 的 certbot.timer 每天 2 次会自动续期(前提: DNS 仍指 106 + 106 的 80 端口空闲).
#   但【175 生产的 nginx 读的是 4 个静态文件】, 不会跟着 106 自动更新:
#       /etc/nginx/ssl/kelaiwei.top.pem       <- ECC fullchain
#       /etc/nginx/ssl/kelaiwei.top.key       <- ECC privkey
#       /etc/nginx/ssl/kelaiwei.top.rsa.pem   <- RSA fullchain
#       /etc/nginx/ssl/kelaiwei.top.rsa.key   <- RSA privkey
#   => 每次 106 续期后, 必须手动同步一次, 否则 175 上会静默过期.
#   (175 挂 ECC + RSA 两套: 手机/浏览器优先走 ECC, 只换 RSA 等于没换.)
#
# 当前有效期: 2026-09-10 签发, 2026-12-09 到期 (ECC=YE1, RSA=YR1)
#
# 用法 (在工作机按顺序执行; 175 一律用 ubuntu@175.178.156.121):
#   1) 送脚本并打包(在 106):
#        scp renew_kelaiwei_cert.sh 106.55.7.10:/tmp/
#        ssh 106.55.7.10 "bash /tmp/renew_kelaiwei_cert.sh pack"
#   2) 取回本地:
#        ssh 106.55.7.10 "base64 -w0 /tmp/kelaiwei_new.tgz" > cert.b64
#   3) 送到 175:
#        ssh ubuntu@175.178.156.121 "base64 -d > /tmp/kelaiwei_new.tgz" < cert.b64
#   4) 送脚本并部署(在 175):
#        scp renew_kelaiwei_cert.sh ubuntu@175.178.156.121:/tmp/
#        ssh ubuntu@175.178.156.121 "bash /tmp/renew_kelaiwei_cert.sh deploy"
#
# !! Windows PowerShell 5.1 陷阱(2026-09-10 踩过): Get-Content -Raw 用 ANSI(GBK)
#    解码 UTF-8 文件, 中文会被静默改坏, 而且改坏后的 md5 两边还能对上(假通过).
#    传文本文件必须走字节: $b=[IO.File]::ReadAllBytes($p);
#    $t=[Text.Encoding]::UTF8.GetString($b); 再 base64. 二进制(cert.b64)不受影响.
# ============================================================================
set -u

ACTION="${1:-help}"

# ---------------------------------------------------------------- pack (在 106)
do_pack() {
  echo "=== [106] 当前证书有效期 ==="
  for n in kelaiwei.top-ec kelaiwei.top; do
    printf "%-18s " "$n"
    sudo openssl x509 -in "/etc/letsencrypt/live/$n/fullchain.pem" -noout \
      -issuer -enddate -ext subjectAltName 2>&1 | tr '\n' ' '
    echo
  done

  echo
  echo "=== [106] 若已过期/未续, 先强制续期(standalone, 要求 80 端口空闲) ==="
  echo "(如 certbot.timer 已自动续好, 这步可跳过)"
  sudo ss -lntp | grep -q ':80 ' && { echo "!! 80 端口被占用, standalone 续期会失败(检查 nginx 是否被启动)"; exit 1; }
  sudo certbot renew --cert-name kelaiwei.top    --non-interactive 2>&1 | tail -5
  sudo certbot renew --cert-name kelaiwei.top-ec --non-interactive 2>&1 | tail -5

  echo
  echo "=== [106] 重新确认有效期 ==="
  sudo openssl x509 -in /etc/letsencrypt/live/kelaiwei.top-ec/fullchain.pem -noout -dates
  sudo openssl x509 -in /etc/letsencrypt/live/kelaiwei.top/fullchain.pem    -noout -dates

  echo
  echo "=== [106] 打包(必须 -h 解引用, live/ 下全是符号链接!) ==="
  sudo rm -f /tmp/kelaiwei_new.tgz
  sudo tar czhf /tmp/kelaiwei_new.tgz -C /etc/letsencrypt/live \
    kelaiwei.top-ec/fullchain.pem \
    kelaiwei.top-ec/privkey.pem \
    kelaiwei.top/fullchain.pem \
    kelaiwei.top/privkey.pem
  sudo chown ubuntu:ubuntu /tmp/kelaiwei_new.tgz
  ls -la /tmp/kelaiwei_new.tgz
  md5sum /tmp/kelaiwei_new.tgz
  echo "--- 包内文件(必须看到 4 个有实际大小的普通文件, 不是 0 字节的 symlink) ---"
  tar tzvf /tmp/kelaiwei_new.tgz
  echo
  echo ">> 下一步: ssh 106.55.7.10 \"base64 -w0 /tmp/kelaiwei_new.tgz\" > cert.b64"
}

# -------------------------------------------------------------- deploy (在 175)
do_deploy() {
  TGZ="${2:-/tmp/kelaiwei_new.tgz}"
  TS=$(date +%Y%m%d_%H%M%S)
  BK="/home/ubuntu/backups/kelaiwei_ssl_$TS"
  STAGE="/tmp/kelaiwei_stage_$TS"

  [ -f "$TGZ" ] || { echo "!! 找不到 $TGZ"; exit 1; }

  echo "=== [175] 1. 备份现有 4 个文件 -> $BK ==="
  sudo mkdir -p "$BK"
  for f in kelaiwei.top.pem kelaiwei.top.key kelaiwei.top.rsa.pem kelaiwei.top.rsa.key; do
    sudo cp -a "/etc/nginx/ssl/$f" "$BK"/
  done
  sudo chown -R ubuntu:ubuntu "$BK"
  md5sum "$BK"/*
  echo "ROLLBACK: sudo cp -a $BK/* /etc/nginx/ssl/ && sudo systemctl reload nginx"

  echo
  echo "=== [175] 2. 解包 + 证书/私钥配对校验 ==="
  mkdir -p "$STAGE"
  tar xzf "$TGZ" -C "$STAGE"
  OK=1
  for p in "kelaiwei.top-ec/fullchain.pem:kelaiwei.top-ec/privkey.pem" \
           "kelaiwei.top/fullchain.pem:kelaiwei.top/privkey.pem"; do
    c="${p%%:*}"; k="${p##*:}"
    cm=$(openssl x509 -in "$STAGE/$c" -noout -pubkey | md5sum | awk '{print $1}')
    km=$(openssl pkey -in "$STAGE/$k" -pubout 2>/dev/null | md5sum | awk '{print $1}')
    if [ "$cm" = "$km" ]; then echo "OK   $c <-> $k 配对"; else echo "FAIL $c <-> $k 不配对"; OK=0; fi
  done
  echo "--- 新证书有效期(必须是未来的日期) ---"
  openssl x509 -in "$STAGE/kelaiwei.top-ec/fullchain.pem" -noout -issuer -dates
  openssl x509 -in "$STAGE/kelaiwei.top/fullchain.pem"    -noout -issuer -dates
  [ "$OK" = "1" ] || { echo "!! 配对校验失败, 未安装任何文件"; exit 1; }

  echo
  echo "=== [175] 3. 安装 ==="
  sudo install -o root -g root -m 644 "$STAGE/kelaiwei.top-ec/fullchain.pem" /etc/nginx/ssl/kelaiwei.top.pem
  sudo install -o root -g root -m 600 "$STAGE/kelaiwei.top-ec/privkey.pem"   /etc/nginx/ssl/kelaiwei.top.key
  sudo install -o root -g root -m 644 "$STAGE/kelaiwei.top/fullchain.pem"    /etc/nginx/ssl/kelaiwei.top.rsa.pem
  sudo install -o root -g root -m 600 "$STAGE/kelaiwei.top/privkey.pem"      /etc/nginx/ssl/kelaiwei.top.rsa.key
  sudo ls -la /etc/nginx/ssl/kelaiwei.top*

  echo
  echo "=== [175] 4. nginx -t (失败则自动回滚, 绝不 reload) ==="
  if ! sudo nginx -t; then
    echo "!!! nginx -t 失败 -> 回滚文件"
    sudo cp -a "$BK"/kelaiwei.top.pem /etc/nginx/ssl/kelaiwei.top.pem
    sudo cp -a "$BK"/kelaiwei.top.key /etc/nginx/ssl/kelaiwei.top.key
    sudo cp -a "$BK"/kelaiwei.top.rsa.pem /etc/nginx/ssl/kelaiwei.top.rsa.pem
    sudo cp -a "$BK"/kelaiwei.top.rsa.key /etc/nginx/ssl/kelaiwei.top.rsa.key
    exit 1
  fi

  echo
  echo "=== [175] 5. reload (不用 restart) ==="
  sudo systemctl reload nginx && echo "reload ok"
  sleep 2; systemctl is-active nginx

  echo
  echo "=== [175] 6. 验证 ==="
  echo "--- ECC 通道(TLS1.2 + ECDSA 套件) ---"
  echo | timeout 10 openssl s_client -connect 127.0.0.1:443 -servername kelaiwei.top \
    -tls1_2 -cipher 'ECDHE-ECDSA-AES256-GCM-SHA384' 2>/dev/null | openssl x509 -noout -issuer -dates
  echo "--- RSA 通道(TLS1.2 + RSA 套件) ---"
  echo | timeout 10 openssl s_client -connect 127.0.0.1:443 -servername kelaiwei.top \
    -tls1_2 -cipher 'AES256-GCM-SHA384' 2>/dev/null | openssl x509 -noout -issuer -dates
  echo "--- 生产健康(必须: locker 200 / cqdyxl 301) ---"
  for h in locker.cqdyxl.com cqdyxl.com kelaiwei.top; do
    printf "%-22s %s\n" "$h" "$(curl -o /dev/null -s -w '%{http_code}' -k -m 10 -H "Host: $h" https://127.0.0.1/)"
  done
}

case "$ACTION" in
  pack)   do_pack ;;
  deploy) do_deploy "$@" ;;
  *)      sed -n '2,25p' "$0" ;;
esac
