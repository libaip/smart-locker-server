#!/bin/bash
# [S226 2026-09-16] 同步结算页改动到 175 (静态文件, 不需要重载)
set -u
H175="ubuntu@175.178.156.121"
KEY="$HOME/.ssh/prod_key"
SSH175="ssh -i $KEY -o BatchMode=yes $H175"
cd /home/ubuntu/smart-locker || exit 1
F=static/admin-v2.html

echo "===== 1) 175 备份 ====="
$SSH175 'cd /home/ubuntu/smart-locker && TS=$(date +%Y%m%d_%H%M%S) && cp -a static/admin-v2.html backups/admin-v2-static.html.bak_s226_$TS && echo "备份戳=$TS"'

echo "===== 2) 同步 ====="
rsync -az -e "ssh -i $KEY -o BatchMode=yes" "$F" "$H175:/home/ubuntu/smart-locker/$F"

echo "===== 3) 两端 md5 ====="
A=$(md5sum "$F" | cut -d' ' -f1)
B=$($SSH175 "md5sum /home/ubuntu/smart-locker/$F" | cut -d' ' -f1)
[ "$A" = "$B" ] && echo "✅ $F  $A" || echo "❌ 106=$A 175=$B"

echo "===== 4) 页面是否带新逻辑 ====="
$SSH175 'for p in 5001 5002; do printf "  @%s autoPickRange=%s 提示=%s\n" $p "$(curl -s http://127.0.0.1:$p/admin-v2 | grep -c autoPickRange)" "$(curl -s http://127.0.0.1:$p/admin-v2 | grep -c 关于月份区间)"; done'

echo "===== 5) 服务健康 ====="
$SSH175 'systemctl is-active smart-locker smart-locker-admin; for p in 5001 5002; do printf "  /api/health @%s -> %s\n" $p "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$p/api/health)"; done'

