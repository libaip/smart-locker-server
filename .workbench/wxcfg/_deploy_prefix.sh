#!/bin/bash
# ============================================================
# [第二步·openid 前缀] 上 175 生产
#   顺序：备份 -> 数据库补列并填前缀 -> 推代码 -> 重载 -> 验证（含影子比对）
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=prefix_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
FILES="wx_config.py wx_config_api.py helpers.py routes/payment.py routes/admin_v2.py routes/user.py"

echo "######################################################################"
echo "# 0) 106 自检"
echo "######################################################################"
for f in $FILES; do python3 -m py_compile "$f" || { echo "  ❌ $f 语法不过"; exit 1; }; done
echo "  六个文件语法通过"
md5sum $FILES | sed 's/^/  /'
echo
echo "--- 175 改前状态 ---"
S175 "cd /home/ubuntu/smart-locker && md5sum wx_config.py wx_config_api.py helpers.py routes/payment.py routes/admin_v2.py routes/user.py"
S175 "echo -n '  openid_prefix 列存在吗(1=存在): '; sudo -u postgres psql -d smart_locker -tAc \"select count(*) from information_schema.columns where table_name='wx_accounts' and column_name='openid_prefix'\""
echo

echo "######################################################################"
echo "# 1) 备份（175：6 个文件 + wx_accounts 表数据）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/routes && cp -a wx_config.py wx_config_api.py helpers.py backups/$BKNAME/ && cp -a routes/payment.py routes/admin_v2.py routes/user.py backups/$BKNAME/routes/ && sudo -u postgres psql -d smart_locker -c \"\\\\copy (select * from wx_accounts) to '/tmp/wx_accounts_before.csv' with csv header\" >/dev/null && sudo cp /tmp/wx_accounts_before.csv backups/$BKNAME/ && sudo chown ubuntu:ubuntu backups/$BKNAME/wx_accounts_before.csv && ls -1 backups/$BKNAME backups/$BKNAME/routes && echo '--- 改前账号表 ---' && cat backups/$BKNAME/wx_accounts_before.csv"
echo

echo "######################################################################"
echo "# 2) 数据库：补列 + 填前缀（幂等，只按 appid 精确匹配）"
echo "######################################################################"
S175 "sudo -u postgres psql -d smart_locker -c \"alter table wx_accounts add column if not exists openid_prefix varchar(16) default ''\""
S175 "sudo -u postgres psql -d smart_locker -c \"update wx_accounts set openid_prefix='ooTcRx' where appid='wxcabd4cbdb3096c4b' and coalesce(openid_prefix,'')=''\"; sudo -u postgres psql -d smart_locker -c \"update wx_accounts set openid_prefix='oLhbm2' where appid='wxd85204d0ec930d46' and coalesce(openid_prefix,'')=''\"; sudo -u postgres psql -d smart_locker -c \"update wx_accounts set openid_prefix='oWrA8' where appid='wx57eaea52dcfff4e8' and coalesce(openid_prefix,'')=''\""
echo "--- 确认 ---"
S175 "sudo -u postgres psql -d smart_locker -c \"select id, acct_type, appid, openid_prefix, is_active from wx_accounts order by id\""
echo

echo "######################################################################"
echo "# 3) 推代码 + 核对"
echo "######################################################################"
scp -i ~/.ssh/prod_key wx_config.py wx_config_api.py helpers.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp 失败'; exit 1; }
tar cf - routes/payment.py routes/admin_v2.py routes/user.py | S175 "cd /home/ubuntu/smart-locker && tar xf -" || { echo '[中止] tar 失败'; exit 1; }
scp -i ~/.ssh/prod_key /tmp/wxcfg_deploy/verify_prefix_175.py ubuntu@172.16.0.2:/home/ubuntu/wxcfg_tools/ || { echo '[中止] scp 验证脚本失败'; exit 1; }
FAILN=0
for f in $FILES; do
  want=$(md5sum "$f" | awk '{print $1}')
  got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "  ✅ $f"; else echo "  ❌ $f 期望 $want 实得 $got"; FAILN=$((FAILN+1)); fi
done
if [ $FAILN -gt 0 ]; then echo "[中止] 文件不一致，不重载"; exit 1; fi
S175 "cd /home/ubuntu/smart-locker && for f in $FILES; do python3 -m py_compile \$f || echo \"  ❌ \$f 语法不过\"; done; echo '  175 上语法检查完毕'"
echo

echo "######################################################################"
echo "# 4) 重载 175（零停机）"
echo "######################################################################"
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
sleep 8
echo

echo "######################################################################"
echo "# 5) 上线后验证（跑 175 上的验证脚本）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/verify_prefix_175.py 2>&1 | grep -v '\[helpers\] INFO' | grep -v '\[启动\]'"
echo
echo "--- 接口 + 业务冒烟 ---"
S175 "TOKEN=\$(sudo -u postgres psql -d smart_locker -tAc \"select auth_token from admin_users where auth_token is not null and auth_token<>'' order by id limit 1\" | tr -d ' \r\n'); curl -s -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:5001/api/wx-config/accounts | python3 -c \"
import sys, json
for a in json.load(sys.stdin)['data']:
    print('  %-24s 前缀=%-8s 生效=%s usable=%s' % (a['appid'], a.get('openid_prefix') or '(空)', a['is_active'], a.get('usable')))
\""
S175 "for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '  --- 重载后报错扫描（应无内容）---'
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -5
echo '  --- 最近 3 分钟通知日志（看有没有异常变化）---'
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep '\[subscribe_msg\]' | tail -6 | cut -c1-140"
echo
echo "备份: backups/$BKNAME"
