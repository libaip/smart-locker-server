#!/bin/bash
# ============================================================
# [T-1789308446] 防呆补丁上 175 生产
#   改 4 个文件：wx_config.py / wx_config_api.py / static/wx_accounts_page.js+.css
#   全部是"配置中心自己"的文件，不碰任何业务取值路径
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=guard_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
FILES="wx_config.py wx_config_api.py"
STATICS="static/wx_accounts_page.js static/wx_accounts_page.css"

echo "######################################################################"
echo "# 0) 106 自检"
echo "######################################################################"
echo "106 git HEAD: $(git log --oneline -1)"
for f in $FILES $STATICS; do
  git diff --quiet HEAD -- "$f" && echo "  ⚠️  $f 与 git 尚未提交（本批次改的，正常）" || true
done
md5sum $FILES $STATICS | sed 's/^/  /'
python3 -m py_compile wx_config.py wx_config_api.py && echo "  ✅ py 语法通过"
node --check static/wx_accounts_page.js && echo "  ✅ js 语法通过"
echo
echo "--- 175 现状（应该是补丁前的老版本）---"
S175 "cd /home/ubuntu/smart-locker && md5sum wx_config.py wx_config_api.py static/wx_accounts_page.js static/wx_accounts_page.css && echo '  含 GUARD 标记数: ' && grep -c 'GUARD-20260913' wx_config.py || true"
echo

echo "######################################################################"
echo "# 1) 备份（175）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/static && cp -a wx_config.py wx_config_api.py backups/$BKNAME/ && cp -a static/wx_accounts_page.js static/wx_accounts_page.css backups/$BKNAME/static/ && cd backups/$BKNAME && md5sum wx_config.py wx_config_api.py static/wx_accounts_page.js static/wx_accounts_page.css"
echo

echo "######################################################################"
echo "# 2) 推文件 + 核对 md5"
echo "######################################################################"
scp -i ~/.ssh/prod_key wx_config.py wx_config_api.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp 失败'; exit 1; }
scp -i ~/.ssh/prod_key static/wx_accounts_page.js static/wx_accounts_page.css ubuntu@172.16.0.2:/home/ubuntu/smart-locker/static/ || { echo '[中止] scp 静态文件失败'; exit 1; }
FAILN=0
for f in $FILES $STATICS; do
  want=$(md5sum "$f" | awk '{print $1}')
  got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "  ✅ $f"; else echo "  ❌ $f 期望 $want 实得 $got"; FAILN=$((FAILN+1)); fi
done
if [ $FAILN -gt 0 ]; then echo "[中止] 有文件不一致，不重载"; exit 1; fi
echo
echo "--- 175 上语法检查 ---"
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile wx_config.py wx_config_api.py && echo '  ✅ py 语法通过' && node --check static/wx_accounts_page.js && echo '  ✅ js 语法通过' && grep -c 'GUARD-20260913' wx_config.py"
echo

echo "######################################################################"
echo "# 3) 重载 175（零停机）"
echo "######################################################################"
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
sleep 8
echo

echo "######################################################################"
echo "# 4) 生产上验证防呆（关键：切假号必须被拒、生效账号不能动）"
echo "######################################################################"
S175 "bash -s" <<'R175'
cd /home/ubuntu/smart-locker
TOKEN=$(sudo -u postgres psql -d smart_locker -tAc "select auth_token from admin_users where auth_token is not null and auth_token<>'' order by id limit 1" | tr -d ' \r\n')
H="Authorization: Bearer $TOKEN"

echo "--- 1) 账号列表带上了 usable ---"
curl -s -H "$H" 'http://127.0.0.1:5001/api/wx-config/accounts' | python3 -c "
import sys, json
d = json.load(sys.stdin)['data']
for a in d:
    print('    [%s] %-26s %-24s 生效=%s usable=%s %s' % (a['acct_type'], a['name'], a['appid'], a['is_active'], a.get('usable'), a.get('usable_reason') or ''))
"

echo "--- 2) 切换前生效账号 ---"
BEFORE=$(curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/effective | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['mp']['appid'],d['oa']['appid'])")
echo "    $BEFORE"

echo "--- 3) 试切到占位符备用号 id=2（必须被拒）---"
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"reason":"上线验证-应被拒绝"}' \
  http://127.0.0.1:5001/api/wx-config/accounts/2/switch | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('    HTTP code=%s message=%s' % (r.get('code'), r.get('message')))
print('    %s' % ('✅ 被拒绝了' if r.get('code') != 200 else '❌ 居然让切了！'))
"
echo "--- 4) 启用占位符号 id=5（必须被拒）---"
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"active":true}' \
  http://127.0.0.1:5001/api/wx-config/accounts/5/toggle | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('    HTTP code=%s message=%s' % (r.get('code'), r.get('message')))
print('    %s' % ('✅ 被拒绝了' if r.get('code') != 200 else '❌ 居然让启用了！'))
"
echo "--- 5) 切换后生效账号（必须和切换前一模一样）---"
AFTER=$(curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/effective | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['mp']['appid'],d['oa']['appid'])")
echo "    $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then echo "    ✅ 生效账号没被动过"; else echo "    ❌ 生效账号被改了！"; fi

echo "--- 6) 探活 id=3（科莱智，密钥为空：必须跳过、fail_count 不变）---"
FC_BEFORE=$(sudo -u postgres psql -d smart_locker -tAc "select fail_count from wx_accounts where id=3" | tr -d ' \r\n')
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"mode":"real"}' \
  http://127.0.0.1:5001/api/wx-config/accounts/3/probe | python3 -c "
import sys, json
r = json.load(sys.stdin)['data']
print('    skipped=%s detail=%s' % (r.get('skipped'), r.get('detail')))
"
FC_AFTER=$(sudo -u postgres psql -d smart_locker -tAc "select fail_count from wx_accounts where id=3" | tr -d ' \r\n')
echo "    fail_count: 改前=$FC_BEFORE 改后=$FC_AFTER  $([ "$FC_BEFORE" = "$FC_AFTER" ] && echo '✅ 没被写脏' || echo '❌ 被写了')"

echo "--- 7) 对着当前生效号模拟失败（必须被拒）---"
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"times":1}' \
  http://127.0.0.1:5001/api/wx-config/accounts/1/simulate-fail | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('    HTTP code=%s message=%s' % (r.get('code'), r.get('message')))
print('    %s' % ('✅ 被拒绝了' if r.get('code') != 200 else '❌ 居然让模拟了！'))
"

echo "--- 8) 切换/降级日志（应该还是 0 条：本次没真切过）---"
echo "    条数: $(sudo -u postgres psql -d smart_locker -tAc 'select count(*) from wx_switch_log')"

echo "--- 9) 两个真账号探活（确认凭据照旧可用）---"
for id in 1 4; do
  curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"mode":"real"}' \
    "http://127.0.0.1:5001/api/wx-config/accounts/$id/probe" | python3 -c "
import sys, json
r = json.load(sys.stdin)['data']
print('    id=%s %s -> ok=%s %s' % (r.get('account_id'), r.get('name'), r.get('ok'), r.get('detail')))
"
done

echo "--- 10) 后台页面按钮已灰掉（页面里应该有 :disabled 判断）---"
curl -s http://127.0.0.1:5001/static/wx_accounts_page.js | grep -c 'a.usable===false'
echo "--- 11) 业务不受影响 ---"
for p in 5001 5002; do printf '    127.0.0.1:%s/api/health -> ' $p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js https://locker.cqdyxl.com/static/admin-v2.html; do printf '    %s -> ' $u; curl -sk -o /dev/null -w '%{http_code}\n' $u; done
echo "--- 12) 重载后日志报错扫描（应无内容）---"
sudo journalctl -u smart-locker --since '4 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|error|exception' | grep -v '注册失败' | tail -6
echo "    （上面没内容就是干净的）"
R175
echo
echo "######################################################################"
echo "# 防呆补丁上线结束"
echo "# 回滚: cd /home/ubuntu/smart-locker && cp -a backups/$BKNAME/wx_config.py . && cp -a backups/$BKNAME/wx_config_api.py . && cp -a backups/$BKNAME/static/*.js backups/$BKNAME/static/*.css static/ && (重载)"
echo "######################################################################"
