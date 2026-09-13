#!/bin/bash
# ============================================================
# [提现直退-20260914] 上 175 生产
#   顺序：106 自检 -> 175 改前状态 -> 备份 -> 推代码 -> 核对 md5 -> 重载 -> 验证
#   本改动【不动数据库】（表结构已有 approver / next_attempt_at / error_msg）
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=mpindirect_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
FILES="helpers.py routes/user.py routes/admin_v2.py"

echo "######################################################################"
echo "# 0) 106 自检"
echo "######################################################################"
for f in $FILES; do python3 -m py_compile "$f" || { echo "  ❌ $f 语法不过"; exit 1; }; done
echo "  三个文件语法通过"
md5sum $FILES | sed 's/^/  /'
echo
echo "--- 175 改前状态 ---"
S175 "cd /home/ubuntu/smart-locker && md5sum $FILES"
echo -n "  三个列都在吗(应为 3): "
S175 "sudo -u postgres psql -d smart_locker -tAc \"select count(*) from information_schema.columns where table_name='withdrawal_records' and column_name in ('approver','next_attempt_at','error_msg')\""
echo -n "  当前 approver='mp_in_direct' 的行数(应为 0): "
S175 "sudo -u postgres psql -d smart_locker -tAc \"select count(*) from withdrawal_records where approver='mp_in_direct'\""
echo "  --- 待处理(status=0)按审批人分布 ---"
S175 "sudo -u postgres psql -d smart_locker -c \"select coalesce(approver,'(空)') as 审批人, count(*) as 条数, sum(case when next_attempt_at is not null and next_attempt_at > now() then 1 else 0 end) as 未来重试数 from withdrawal_records where status=0 group by 1 order by 2 desc limit 12\""
echo

echo "######################################################################"
echo "# 1) 备份（175：3 个文件）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/routes && cp -a helpers.py backups/$BKNAME/ && cp -a routes/user.py routes/admin_v2.py backups/$BKNAME/routes/ && ls -l backups/$BKNAME backups/$BKNAME/routes"
echo

echo "######################################################################"
echo "# 2) 推代码 + 核对"
echo "######################################################################"
scp -i ~/.ssh/prod_key helpers.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp helpers.py 失败'; exit 1; }
tar cf - routes/user.py routes/admin_v2.py | S175 "cd /home/ubuntu/smart-locker && tar xf -" || { echo '[中止] tar 失败'; exit 1; }
scp -i ~/.ssh/prod_key /tmp/wxcfg_deploy/verify_wd_175.py ubuntu@172.16.0.2:/home/ubuntu/wxcfg_tools/ || { echo '[中止] scp 验证脚本失败'; exit 1; }
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
echo "# 3) 重载 175（零停机）"
echo "######################################################################"
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
sleep 8
echo

echo "######################################################################"
echo "# 4) 上线后验证（175 上只读脚本，不写任何测试数据）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/verify_wd_175.py 2>&1 | grep -vE '\[helpers\] INFO|\[启动\]'"
echo
echo "--- 接口 + 业务冒烟 ---"
S175 "for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '  --- 重载后报错扫描（应无内容）---'
sudo journalctl -u smart-locker --since '4 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception|has_mp_menu_entry' | tail -8
echo '  --- 最近 3 分钟提现批量任务日志 ---'
sudo journalctl -u smart-locker --since '6 min ago' --no-pager 2>/dev/null | grep -iE 'withdrawal|提现|必退|白名单' | tail -8 | cut -c1-160"
echo
echo "备份: backups/$BKNAME"
