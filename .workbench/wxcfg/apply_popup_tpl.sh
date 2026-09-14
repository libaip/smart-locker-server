#!/bin/bash
# [正式改动] 还原实验 -> 打正式补丁 -> 零停机重载 -> 核对
set -u
cd /home/ubuntu/smart-locker || exit 1

echo "--- 1) 先把实验改动还原(不重载) ---"
python3 /home/ubuntu/wxcfg_tools/patch_subtpl_test.py /home/ubuntu/smart-locker --revert || exit 1

echo "--- 2) 打正式补丁 ---"
python3 /home/ubuntu/wxcfg_tools/patch_popup_tpl.py /home/ubuntu/smart-locker --real || exit 1

echo "--- 3) 零停机重载 ---"
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

echo "--- 4) 接口返回(应只有 2 条不同模板: 退款成功 + 押金退还) ---"
curl -s http://127.0.0.1:5001/api/user/subscribe-templates | python3 -m json.tool

echo "--- 5) 确认发送开关(应为 False) ---"
grep -n "_SEND_STORAGE_SUCCESS_NOTIFY" routes/payment.py

echo "--- 6) 报错扫描(应无内容) ---"
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -3
echo "完成。还原正式改动: python3 /home/ubuntu/wxcfg_tools/patch_popup_tpl.py /home/ubuntu/smart-locker --revert 然后重载"
