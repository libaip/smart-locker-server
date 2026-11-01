#!/bin/bash
# [S188 2026-09-15] 同步本次改动到 175: 备份 -> rsync/scp -> 两端 md5 核对 -> py_compile
set -u
H175="ubuntu@175.178.156.121"
KEY="$HOME/.ssh/prod_key"
SSH175="ssh -i $KEY -o BatchMode=yes $H175"
RSH175="ssh -i $KEY -o BatchMode=yes"
cd /home/ubuntu/smart-locker || exit 1

echo "===== 1) 175 当前 master 进程 ====="
$SSH175 'ps -eo pid,ppid,cmd | grep gunicorn | grep -v grep | head -n 4'

echo "===== 2) 175 备份(带时间戳) ====="
$SSH175 'cd /home/ubuntu/smart-locker && TS=$(date +%Y%m%d_%H%M%S) && mkdir -p backups && cp -a helpers.py backups/helpers.py.bak_s188_$TS && cp -a routes/user.py backups/user.py.bak_s188_$TS && cp -a routes/admin_v2.py backups/admin_v2.py.bak_s188_$TS && cp -a templates/admin-v2.html backups/admin-v2.html.bak_s188_$TS && echo "备份戳=$TS" && ls -la backups/*bak_s188_$TS'

echo "===== 3) 同步 ====="
if command -v rsync >/dev/null 2>&1; then
  echo "用 rsync"
  rsync -az -e "$RSH175" helpers.py "$H175:/home/ubuntu/smart-locker/helpers.py"
  rsync -az -e "$RSH175" routes/user.py routes/admin_v2.py "$H175:/home/ubuntu/smart-locker/routes/"
  rsync -az -e "$RSH175" templates/admin-v2.html "$H175:/home/ubuntu/smart-locker/templates/"
else
  echo "用 scp"
  scp -i "$KEY" -o BatchMode=yes helpers.py "$H175:/home/ubuntu/smart-locker/helpers.py"
  scp -i "$KEY" -o BatchMode=yes routes/user.py routes/admin_v2.py "$H175:/home/ubuntu/smart-locker/routes/"
  scp -i "$KEY" -o BatchMode=yes templates/admin-v2.html "$H175:/home/ubuntu/smart-locker/templates/"
fi

echo "===== 4) 两端 md5 核对 ====="
for f in helpers.py routes/user.py routes/admin_v2.py templates/admin-v2.html; do
  A=$(md5sum "$f" | cut -d' ' -f1)
  B=$($SSH175 "md5sum /home/ubuntu/smart-locker/$f" | cut -d' ' -f1)
  if [ "$A" = "$B" ]; then echo "✅ $f  $A"; else echo "❌ $f  106=$A  175=$B"; fi
done

echo "===== 5) 175 py_compile ====="
$SSH175 'cd /home/ubuntu/smart-locker && python3 -m py_compile helpers.py routes/user.py routes/admin_v2.py && echo COMPILE_OK'

echo "===== 6) 关键改动是否到位 ====="
$SSH175 'cd /home/ubuntu/smart-locker && grep -c settle_withdrawal_for_order helpers.py routes/user.py routes/admin_v2.py; grep -c "alarms/pending-count" routes/admin_v2.py; grep -c "menu-badge" templates/admin-v2.html'

