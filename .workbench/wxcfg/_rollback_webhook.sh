#!/bin/bash
# ============================================================
# [webhook 回滚] 撤掉"公众号事件不再当投诉"的修复：
#   恢复成老行为 —— 用户在公众号里点菜单/点订阅等任何动作，都生成一条投诉（并触发自动原路退款）
#   步骤：先把当前(修复版)存档 -> 用修复前的备份覆盖 -> 语法检查 -> 零停机重载 -> 核对
#   数据库不动。
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
TS=$(date +%Y%m%d_%H%M%S)
KEEP=/home/ubuntu/smart-locker/backups/webhookfix_kept_$TS

echo "== 1) 回滚前：175 现在装的是修复版（应含 _is_user_msg 2 处） =="
S175 "md5sum /home/ubuntu/smart-locker/routes/webhook.py; echo -n '  _is_user_msg 出现次数: '; grep -c _is_user_msg /home/ubuntu/smart-locker/routes/webhook.py"
echo
echo "== 2) 先把修复版存档（以后想再关掉，就用它覆盖回去，一键） =="
S175 "mkdir -p $KEEP/routes && cp -a /home/ubuntu/smart-locker/routes/webhook.py $KEEP/routes/webhook.py && md5sum $KEEP/routes/webhook.py"
echo
echo "== 3) 恢复老行为（用修复前的备份覆盖） =="
S175 "cp -a /home/ubuntu/smart-locker/backups/whfix_20260914_000114/routes/webhook.py /home/ubuntu/smart-locker/routes/webhook.py && md5sum /home/ubuntu/smart-locker/routes/webhook.py"
echo
echo "== 4) 语法检查 =="
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile routes/webhook.py && echo '  语法通过'"
echo
echo "== 5) 零停机重载 =="
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 12
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
echo
echo "== 6) 核对 =="
S175 "echo -n '  md5(应=5db259d2d4e920d592c6180d638543bd): '; md5sum /home/ubuntu/smart-locker/routes/webhook.py | awk '{print \$1}'; echo -n '  _is_user_msg 出现次数(应 0): '; grep -c _is_user_msg /home/ubuntu/smart-locker/routes/webhook.py; echo -n '  投诉创建条件那行长啥样: '; grep -n 'if _phone and _msg_id_val' /home/ubuntu/smart-locker/routes/webhook.py"
echo
echo "--- 业务冒烟 ---"
S175 "for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '  --- 重载后报错扫描（应无内容）---'
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -5"
echo
echo "修复版存档: $KEEP/routes/webhook.py"
echo "回滚完成。"
