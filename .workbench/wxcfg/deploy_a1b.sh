#!/bin/bash
# [A1-b] 上 175：app.py + static/deposit.html + 新增后台设置 mp_jump_max_retry=3
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BK=backups/a1b_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
A_APP=$1     # 106 上 app.py 改前备份
A_H5=$2      # 106 上 deposit.html 改前备份

echo "== 1) 一致性校验（175 现版 应等于 106 改前版） =="
PRE_A=$(md5sum "$A_APP" | awk '{print $1}'); PRE_H=$(md5sum "$A_H5" | awk '{print $1}')
GOT_A=$(S175 "md5sum /home/ubuntu/smart-locker/app.py" | awk '{print $1}')
GOT_H=$(S175 "md5sum /home/ubuntu/smart-locker/static/deposit.html" | awk '{print $1}')
[ "$GOT_A" = "$PRE_A" ] && echo "   ✅ app.py" || echo "   ⚠️ app.py 不一致: 175=$GOT_A 106改前=$PRE_A"
[ "$GOT_H" = "$PRE_H" ] && echo "   ✅ deposit.html" || echo "   ⚠️ deposit.html 不一致: 175=$GOT_H 106改前=$PRE_H"

echo "== 2) 备份(175) =="
S175 "cd /home/ubuntu/smart-locker && mkdir -p $BK/static && cp -a app.py $BK/ && cp -a static/deposit.html $BK/static/ && ls -l $BK $BK/static | head -8"

echo "== 3) 推代码 =="
scp -i ~/.ssh/prod_key app.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp app.py 失败'; exit 1; }
scp -i ~/.ssh/prod_key static/deposit.html ubuntu@172.16.0.2:/home/ubuntu/smart-locker/static/ || { echo '[中止] scp deposit.html 失败'; exit 1; }
FAIL=0
for f in app.py static/deposit.html; do
  want=$(md5sum "$f" | awk '{print $1}'); got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "   ✅ $f"; else echo "   ❌ $f"; FAIL=$((FAIL+1)); fi
done
[ $FAIL -gt 0 ] && { echo '[中止] 文件不一致，不重载'; exit 1; }
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile app.py && echo '   175 语法通过'"

echo "== 4) 建后台设置 mp_jump_max_retry=3（幂等） =="
S175 "PGPASSWORD=locker_pass_2024 psql -h 127.0.0.1 -U locker_admin -d smart_locker -c \"insert into system_settings (setting_key, setting_value, description) select 'mp_jump_max_retry','3','跳小程序最多点几次, 超过就放行网页支付' where not exists (select 1 from system_settings where setting_key='mp_jump_max_retry')\"; PGPASSWORD=locker_pass_2024 psql -h 127.0.0.1 -U locker_admin -d smart_locker -c \"select setting_key, setting_value from system_settings where setting_key='mp_jump_max_retry'\""

echo "== 5) 重载 =="
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '   已发 HUP: '\$MPIDS"
sleep 12
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"   就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"

echo "== 6) 核对 =="
S175 "echo -n '   H5 计数/放行代码: '; grep -c 'mpGateEscape\\|MP_RETRY_LIMIT' /home/ubuntu/smart-locker/static/deposit.html; echo -n '   app 注入占位符: '; grep -c 'mp_retry_limit' /home/ubuntu/smart-locker/app.py; echo -n '   弹层出口文案: '; grep -c '一直跳不过去' /home/ubuntu/smart-locker/static/deposit.html"
echo "   --- 渲染后的页面里，上限应被替换成 3（不再是占位符） ---"
S175 "curl -s 'http://127.0.0.1:5001/store?device=101117' | grep -o 'MP_RETRY_LIMIT = parseInt(\"[^\"]*\"' | head -2"
echo "   --- 接口冒烟 ---"
S175 "curl -s 'http://127.0.0.1:5001/api/health' | head -c 80; echo; curl -s 'http://127.0.0.1:5001/api/user/mp-entered?order_id=0'; echo"
echo "   --- 报错扫描(应无内容) ---"
S175 "sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -3"
echo
echo "备份: 175 $BK"
