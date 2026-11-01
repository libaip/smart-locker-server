#!/bin/bash
# [S188 2026-09-15] 同步 static/admin-v2.html(线上) + 还原 templates 副本, 并验证后台页面红点
set -u
H175="ubuntu@175.178.156.121"
KEY="$HOME/.ssh/prod_key"
SSH175="ssh -i $KEY -o BatchMode=yes $H175"
RSH175="ssh -i $KEY -o BatchMode=yes"
cd /home/ubuntu/smart-locker || exit 1

echo "===== 1) 175 备份 ====="
$SSH175 'cd /home/ubuntu/smart-locker && TS=$(date +%Y%m%d_%H%M%S) && cp -a static/admin-v2.html backups/admin-v2-static.html.bak_s188_$TS && echo "备份戳=$TS"'

echo "===== 2) 同步 ====="
rsync -az -e "$RSH175" static/admin-v2.html "$H175:/home/ubuntu/smart-locker/static/admin-v2.html"
rsync -az -e "$RSH175" templates/admin-v2.html "$H175:/home/ubuntu/smart-locker/templates/admin-v2.html"

echo "===== 3) 两端 md5 ====="
for f in static/admin-v2.html templates/admin-v2.html; do
  A=$(md5sum "$f" | cut -d' ' -f1)
  B=$($SSH175 "md5sum /home/ubuntu/smart-locker/$f" | cut -d' ' -f1)
  if [ "$A" = "$B" ]; then echo "✅ $f  $A"; else echo "❌ $f  106=$A 175=$B"; fi
done

echo "===== 4) 页面是否带红点(5001/5002/对外域名) ====="
$SSH175 'for u in http://127.0.0.1:5001/ http://127.0.0.1:5002/; do echo "  $u -> menu-badge=$(curl -s $u | grep -c menu-badge) alarmBadge=$(curl -s $u | grep -c alarmBadge) loadAlarmPending=$(curl -s $u | grep -c loadAlarmPending)"; done'
echo "  对外: https://locker.cqdyxl.com/ -> menu-badge=$(curl -s https://locker.cqdyxl.com/ | grep -c menu-badge)"

