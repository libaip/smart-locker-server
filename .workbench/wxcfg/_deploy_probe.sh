#!/bin/bash
# ============================================================
# [第二步E + 防呆3] 上 175：前端编号改服务端下发 + 切换前自动探活
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=probe_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
TOP="app.py wx_config.py wx_config_api.py"
STATIC="static/deposit.html static/wx_accounts_page.js"

echo "######################################################################"
echo "# 0) 106 自检"
echo "######################################################################"
python3 -m py_compile app.py wx_config.py wx_config_api.py && echo "  三个 py 语法通过"
node --check static/wx_accounts_page.js && echo "  JS 语法通过"
md5sum $TOP $STATIC | sed 's/^/  /'
echo "--- 175 现状 ---"
S175 "cd /home/ubuntu/smart-locker && md5sum app.py wx_config.py wx_config_api.py static/deposit.html static/wx_accounts_page.js"
echo

echo "######################################################################"
echo "# 1) 备份（175）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/routes && cp -a app.py wx_config.py wx_config_api.py backups/$BKNAME/ && cp -a static/deposit.html static/wx_accounts_page.js backups/$BKNAME/ && ls -1 backups/$BKNAME"
echo

echo "######################################################################"
echo "# 2) 推文件 + 核对"
echo "######################################################################"
scp -i ~/.ssh/prod_key app.py wx_config.py wx_config_api.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp 失败'; exit 1; }
scp -i ~/.ssh/prod_key static/deposit.html static/wx_accounts_page.js ubuntu@172.16.0.2:/home/ubuntu/smart-locker/static/ || { echo '[中止] scp 静态失败'; exit 1; }
FAILN=0
for f in $TOP $STATIC; do
  want=$(md5sum "$f" | awk '{print $1}')
  got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "  ✅ $f"; else echo "  ❌ $f 期望 $want 实得 $got"; FAILN=$((FAILN+1)); fi
done
if [ $FAILN -gt 0 ]; then echo "[中止] 文件不一致，不重载"; exit 1; fi
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile app.py wx_config.py wx_config_api.py && echo '  175 py 语法通过' && node --check static/wx_accounts_page.js 2>/dev/null || echo '  (175 没装 node，跳过 JS 检查，106 上已检过)'"
echo "--- 改后的 deposit.html 里还有没有写死的旧编号 ---"
S175 "cd /home/ubuntu/smart-locker && echo -n '  wxcabd4cbdb3096c4b 出现次数: '; grep -c 'wxcabd4cbdb3096c4b' static/deposit.html || true"
echo

echo "######################################################################"
echo "# 3) 重载 175"
echo "######################################################################"
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
sleep 8
echo

echo "######################################################################"
echo "# 4) 上用后验证"
echo "######################################################################"
echo "--- 4.1 存包页里的小程序编号是不是服务端下发的（走真实外网页面）---"
S175 "curl -sk 'https://locker.cqdyxl.com/store?v=9&cabinet_id=1' | grep -o '\"mp_appid\":[^,]*' | head -2"
S175 "echo -n '  页面里还有没有写死的旧编号: '; curl -sk 'https://locker.cqdyxl.com/store?v=9&cabinet_id=1' | grep -c 'wxcabd4cbdb3096c4b'"
echo "  （出现次数应该等于 1：就是服务端刚注入的那个；改造前这里会有写死的旧编号）"
echo
echo "--- 4.2 切换前自动探活：拿一个假编号去切，必须被拦下 ---"
S175 "bash -s" <<'R175'
TOKEN=$(sudo -u postgres psql -d smart_locker -tAc "select auth_token from admin_users where auth_token is not null and auth_token<>'' order by id limit 1" | tr -d ' \r\n')
H="Authorization: Bearer $TOKEN"
echo "  当前生效: $(curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/effective | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['mp']['appid'])")"
echo "  --- 建一个临时的假号（等一下要删掉）---"
NID=$(curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"acct_type":"mp","name":"临时探活测试号(会自动删除)","appid":"wxtmpprobe99","secret":"wrongsecret"}' \
  http://127.0.0.1:5001/api/wx-config/accounts | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
echo "  临时号 id=$NID"
echo "  --- 试着切过去（探活会失败 -> 必须被拒）---"
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"reason":"验证探活拦截"}' \
  "http://127.0.0.1:5001/api/wx-config/accounts/$NID/switch" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('    code=%s' % r.get('code'))
print('    message=%s' % r.get('message'))
print('    %s' % ('✅ 被拦下了' if r.get('code') != 200 else '❌ 居然让切了！'))
"
echo "  --- 生效号有没有被改动 ---"
curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/effective | python3 -c "
import sys,json;d=json.load(sys.stdin)['data'];print('    现在生效 =', d['mp']['appid'], '（应还是 wxcabd4cbdb3096c4b）')
"
echo "  --- 删掉临时号 ---"
curl -s -X DELETE -H "$H" "http://127.0.0.1:5001/api/wx-config/accounts/$NID" | python3 -c "import sys,json;r=json.load(sys.stdin);print('    ',r.get('message'))"
echo "  --- 剩下的账号 ---"
curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/accounts | python3 -c "
import sys, json
for a in json.load(sys.stdin)['data']:
    print('    id=%-2s %-24s 前缀=%-8s 生效=%s' % (a['id'], a['appid'], a.get('openid_prefix') or '(空)', a['is_active']))
"
R175
echo
echo "--- 4.3 业务冒烟 + 通知日志 ---"
S175 "for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js https://locker.cqdyxl.com/static/admin-v2.html; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '  --- 重载后报错扫描（应无内容）---'
sudo journalctl -u smart-locker --since '4 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -4
echo '  --- 近 6 分钟通知三个计数 ---'
for pat in '发送成功' '发送失败' '跳过公众号openid'; do
  n=\$(sudo journalctl -u smart-locker --since '6 min ago' --no-pager 2>/dev/null | grep -c \"\[subscribe_msg\] \$pat\")
  printf '    %-20s %s\n' \"\$pat\" \"\$n\"
done"
echo
echo "备份: backups/$BKNAME"
