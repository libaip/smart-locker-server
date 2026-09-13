#!/bin/bash
# ============================================================
# [提现直退-回退] 把 175 恢复成上线前那一版（2026-09-13 23:36 时的状态）
#   只换回 3 个文件，不动数据库；回退源=上线时自动做的备份
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
BK=backups/mpindirect_20260914_003703
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }

echo "== 1) 175 现在装的是（要撤掉的）这版 =="
S175 "cd /home/ubuntu/smart-locker && md5sum helpers.py routes/user.py routes/admin_v2.py"
echo
echo "== 2) 安全前提：确认没有任何一笔真的走了直退（必须 0） =="
S175 "sudo -u postgres psql -d smart_locker -tAc \"select count(*) from withdrawal_records where approver in ('mp_in_direct','公众号入口直退')\""
echo
echo "== 3) 回退（从上线时备份覆盖回去） =="
S175 "cd /home/ubuntu/smart-locker && cp -a $BK/helpers.py helpers.py && cp -a $BK/routes/user.py routes/user.py && cp -a $BK/routes/admin_v2.py routes/admin_v2.py && md5sum helpers.py routes/user.py routes/admin_v2.py"
echo
echo "== 4) 175 上语法检查 =="
S175 "cd /home/ubuntu/smart-locker && for f in helpers.py routes/user.py routes/admin_v2.py; do python3 -m py_compile \$f || echo \"  ❌ 语法不过: \$f\"; done; echo '  三个文件语法通过'"
echo
echo "== 5) 零停机重载 =="
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 12
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
echo
echo "== 6) 回退后核对 =="
S175 "cd /home/ubuntu/smart-locker && echo -n '  helpers.py 还有 has_mp_menu_entry 吗(应 0): '; grep -c 'def has_mp_menu_entry' helpers.py; echo -n '  admin_v2.py 还有 mp_in_direct 吗(应 0): '; grep -c 'mp_in_direct' routes/admin_v2.py; echo -n '  user.py 还有 mp_in_direct 吗(应 0): '; grep -c 'mp_in_direct' routes/user.py; echo -n '  必退分支是否恢复成只认白名单(应 1): '; grep -c \"approver = 'whitelist_auto'\" routes/admin_v2.py"
echo
echo "--- 业务冒烟 ---"
S175 "for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store https://locker.cqdyxl.com/static/wx_accounts_page.js; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '  --- 重载后报错扫描（应无内容）---'
sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -5
echo '  --- 待处理提现单（387 条手工审批的应不受影响）---'
sudo -u postgres psql -d smart_locker -c \"select coalesce(approver,'(空)') as 审批人, count(*) from withdrawal_records where status=0 group by 1 order by 2 desc limit 5\""
echo
echo "回退完成。"
