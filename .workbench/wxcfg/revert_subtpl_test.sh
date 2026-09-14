#!/bin/bash
# [实验] 还原 175 上的"寄存成功"临时改动（从备份还原 + 重载 + 核对）
set -u
cd /home/ubuntu/smart-locker || exit 1
python3 /home/ubuntu/wxcfg_tools/patch_subtpl_test.py /home/ubuntu/smart-locker --revert || { echo '[中止] 还原失败'; exit 1; }
echo "--- 重载 ---"
MPIDS=$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && $2==1 {print $1}')
for p in $MPIDS; do sudo kill -HUP "$p"; done
echo "  已发 HUP: $MPIDS"
sleep 12
for i in $(seq 1 15); do
  a=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health)
  b=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health)
  if [ "$a" = "200" ] && [ "$b" = "200" ]; then echo "  就绪 5001=$a 5002=$b"; break; fi
  sleep 2
done
echo "--- 接口返回(应恢复 3 条) ---"
curl -s http://127.0.0.1:5001/api/user/subscribe-templates | python3 -m json.tool
