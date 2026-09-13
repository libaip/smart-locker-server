#!/bin/bash
# ============================================================
# [T-1789308446] 第二次小修上 175：被拒的操作不记日志 + 清掉那条假记录
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=guard2_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }

echo "===== 1) 106 自检 ====="
python3 -m py_compile wx_config_api.py && echo "  ✅ 语法通过"
md5sum wx_config_api.py | sed 's/^/  106: /'
echo "  175 现状: $(S175 'md5sum /home/ubuntu/smart-locker/wx_config_api.py' | awk '{print $1}')"
echo
echo "===== 2) 备份 + 推文件（175）====="
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME && cp -a wx_config_api.py backups/$BKNAME/ && md5sum backups/$BKNAME/wx_config_api.py"
scp -i ~/.ssh/prod_key wx_config_api.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp 失败'; exit 1; }
want=$(md5sum wx_config_api.py | awk '{print $1}')
got=$(S175 'md5sum /home/ubuntu/smart-locker/wx_config_api.py' | awk '{print $1}')
[ "$want" = "$got" ] && echo "  ✅ md5 一致 ($want)" || { echo "  ❌ 不一致，不继续"; exit 1; }
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile wx_config_api.py && echo '  ✅ 175 上语法通过'"
echo
echo "===== 3) 重载 175 ====="
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); if [ \"\$a\" = \"200\" ]; then echo \"  就绪 5001=\$a\"; break; fi; sleep 2; done"
sleep 8
echo
echo "===== 4) 验证：被拒的操作不再产生日志 ====="
S175 "bash -s" <<'R175'
cd /home/ubuntu/smart-locker
TOKEN=$(sudo -u postgres psql -d smart_locker -tAc "select auth_token from admin_users where auth_token is not null and auth_token<>'' order by id limit 1" | tr -d ' \r\n')
H="Authorization: Bearer $TOKEN"
N1=$(sudo -u postgres psql -d smart_locker -tAc 'select count(*) from wx_switch_log' | tr -d ' \r\n')
echo "  操作前日志条数: $N1"
echo "  --- 再试一次「启用占位符号」（应被拒且不记日志）---"
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"active":true}' \
  http://127.0.0.1:5001/api/wx-config/accounts/5/toggle | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('    code=%s message=%s' % (r.get('code'), r.get('message')))
"
N2=$(sudo -u postgres psql -d smart_locker -tAc 'select count(*) from wx_switch_log' | tr -d ' \r\n')
echo "  操作后日志条数: $N2  $([ "$N1" = "$N2" ] && echo '✅ 没多记（修好了）' || echo '❌ 还是多记了')"
R175
echo
echo "===== 5) 清掉验证时留下的那条假日志（先备份内容）====="
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME && sudo -u postgres psql -d smart_locker -c \"select * from wx_switch_log\" > backups/$BKNAME/switch_log_before_delete.txt 2>&1 && cat backups/$BKNAME/switch_log_before_delete.txt"
echo "--- 只删这条精确匹配的假记录（id=1 且 reason='启用（手动）' 且 to_name 是占位号）---"
S175 "sudo -u postgres psql -d smart_locker -c \"delete from wx_switch_log where id=1 and reason='启用（手动）' and to_name='备用公众号-异主体(待注册)' and operator='local-admin'\""
S175 "echo -n '  删除后条数: '; sudo -u postgres psql -d smart_locker -tAc 'select count(*) from wx_switch_log'"
echo
echo "===== 6) 上线后整体验证 ====="
S175 "bash -s" <<'R1752'
cd /home/ubuntu/smart-locker
TOKEN=$(sudo -u postgres psql -d smart_locker -tAc "select auth_token from admin_users where auth_token is not null and auth_token<>'' order by id limit 1" | tr -d ' \r\n')
H="Authorization: Bearer $TOKEN"
echo "  --- 账号列表 usable 字段还在 ---"
curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/accounts | python3 -c "
import sys, json
for a in json.load(sys.stdin)['data']:
    print('    %-24s 生效=%s usable=%s' % (a['appid'], a['is_active'], a.get('usable')))
"
echo "  --- 试切占位符号（仍必须被拒）---"
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"reason":"复验"}' \
  http://127.0.0.1:5001/api/wx-config/accounts/2/switch | python3 -c "
import sys, json
r = json.load(sys.stdin); print('    code=%s %s' % (r.get('code'), r.get('message')))
"
echo "  --- 生效账号没变 ---"
curl -s -H "$H" http://127.0.0.1:5001/api/wx-config/effective | python3 -c "
import sys, json
d = json.load(sys.stdin)['data']
print('    小程序=%s(%s)  公众号=%s(%s)' % (d['mp']['appid'], d['mp']['source'], d['oa']['appid'], d['oa']['source']))
"
echo "  --- 探活两个真账号 ---"
for id in 1 4; do
  curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"mode":"real"}' \
    "http://127.0.0.1:5001/api/wx-config/accounts/$id/probe" | python3 -c "
import sys, json
r = json.load(sys.stdin)['data']; print('    id=%s %s -> %s' % (r.get('account_id'), r.get('name'), r.get('detail')))
"
done
echo "  --- 日志条数（应为 0）: $(sudo -u postgres psql -d smart_locker -tAc 'select count(*) from wx_switch_log') ---"
echo "  --- 业务冒烟 ---"
for p in 5001 5002; do printf '    127.0.0.1:%s/api/health -> ' $p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js; do printf '    %s -> ' $u; curl -sk -o /dev/null -w '%{http_code}\n' $u; done
echo "  --- 重载后报错扫描 ---"
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -4
echo "    （上面没内容就是干净的）"
R1752
echo
echo "备份: backups/$BKNAME"
