#!/bin/bash
# [B 方案] 175 上恢复"公众号事件不再建投诉"的修复版 webhook.py + 重载 + 核对
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BK=backups/whfix_back_$TS
echo "== 1) 改前 =="
md5sum routes/webhook.py
mkdir -p $BK/routes && cp -a routes/webhook.py $BK/routes/
echo "   备份: $BK/routes/webhook.py"
echo "== 2) 覆盖成修复版(来自 106 的 git b643d5e) =="
cp -a /tmp/webhook_fixed.py routes/webhook.py
echo -n "   改后 md5(应 c457111f...): "; md5sum routes/webhook.py | cut -c1-34
python3 -m py_compile routes/webhook.py && echo "   语法通过"
echo -n "   _is_user_msg 出现次数(应 2): "; grep -c _is_user_msg routes/webhook.py
echo "== 3) 重载 =="
MPIDS=$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && $2==1 {print $1}')
for p in $MPIDS; do sudo kill -HUP "$p"; done
echo "   已发 HUP: $MPIDS"
sleep 12
for i in $(seq 1 20); do
  a=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health)
  b=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health)
  if [ "$a" = "200" ] && [ "$b" = "200" ]; then echo "   就绪 5001=$a 5002=$b"; break; fi
  sleep 2
done
echo "== 4) 报错扫描(应无内容) =="
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -3
echo "备份: $BK"
