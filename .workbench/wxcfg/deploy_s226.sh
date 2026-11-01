#!/bin/bash
# [S226 2026-09-15] 同步手动审批修复到 175 并零停机重载
set -u
H175="ubuntu@175.178.156.121"
KEY="$HOME/.ssh/prod_key"
SSH175="ssh -i $KEY -o BatchMode=yes $H175"
cd /home/ubuntu/smart-locker || exit 1
F="routes/admin_v2.py"

echo "===== 1) 175 备份 ====="
$SSH175 'cd /home/ubuntu/smart-locker && TS=$(date +%Y%m%d_%H%M%S) && cp -a routes/admin_v2.py backups/admin_v2.py.bak_s226_$TS && echo "备份戳=$TS"'

echo "===== 2) 同步 ====="
rsync -az -e "ssh -i $KEY -o BatchMode=yes" "$F" "$H175:/home/ubuntu/smart-locker/$F"

echo "===== 3) 两端 md5 ====="
A=$(md5sum "$F" | cut -d' ' -f1)
B=$($SSH175 "md5sum /home/ubuntu/smart-locker/$F" | cut -d' ' -f1)
[ "$A" = "$B" ] && echo "✅ $F  $A" || echo "❌ 106=$A 175=$B"

echo "===== 4) 175 py_compile ====="
$SSH175 'cd /home/ubuntu/smart-locker && python3 -m py_compile routes/admin_v2.py && echo COMPILE_OK'

echo "===== 5) 关键改动到位 ====="
$SSH175 'cd /home/ubuntu/smart-locker && grep -c "自己收款的商户号" routes/admin_v2.py; grep -c "GREATEST(COALESCE(refund_amount,0), %s)" routes/admin_v2.py; grep -c "后台审批部分退款失败" routes/admin_v2.py'

echo "===== 6) 无感重载 ====="
$SSH175 'sudo -n kill -HUP 1736960 1736981 && echo HUP_SENT'
sleep 12

echo "===== 7) 验收 ====="
$SSH175 'systemctl is-active smart-locker smart-locker-admin; for p in 5001 5002; do printf "  /api/health @%s -> %s\n" $p "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$p/api/health)"; done; printf "  提现列表接口(未登录401=路由在) -> %s\n" "$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:5001/api/admin/withdrawal/approve)"'

echo "===== 8) 重载后异常 ====="
$SSH175 'sudo -n journalctl -u smart-locker -u smart-locker-admin --since "5 min ago" --no-pager | grep -icE "traceback|syntaxerror|importerror" || echo 0'

