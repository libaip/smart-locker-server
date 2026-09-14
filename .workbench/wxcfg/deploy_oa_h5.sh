#!/bin/bash
# ============================================================
# [2026-09-14] 上 175：功能②（小程序发不出去 -> 改发公众号）+ 功能①（H5 只给走 H5 的用户弹公众号订阅）
#   顺序：106 自检 -> 175 改前状态 + 一致性校验 -> 备份 -> 推代码 -> 核对 -> 重载 -> 冒烟
#   注意：H5 那个弹层由后台设置 oa_subscribe_enabled 控制，本次部署【不改设置】，
#         所以部署完线上行为与之前一致，等老板手机就绪再打开开关做验证。
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BK=backups/oa_h5_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
FILES="helpers.py routes/user.py static/deposit.html"
A_HELPERS=$1     # 106 上 helpers.py 改前备份
A_USER=$2        # 106 上 routes/user.py 改前备份
A_H5=$3          # 106 上 static/deposit.html 改前备份

echo "== 1) 106 md5（改后） =="
md5sum $FILES | sed 's/^/   /'
echo "== 1b) 106 改前备份 md5（应与 175 当前一致） =="
md5sum "$A_HELPERS" "$A_USER" "$A_H5" | sed 's/^/   /'
PRE_H=$(md5sum "$A_HELPERS" | awk '{print $1}')
PRE_U=$(md5sum "$A_USER" | awk '{print $1}')
PRE_D=$(md5sum "$A_H5" | awk '{print $1}')

echo
echo "== 2) 175 改前状态 =="
S175 "cd /home/ubuntu/smart-locker && md5sum $FILES"
GOT_H=$(S175 "md5sum /home/ubuntu/smart-locker/helpers.py" | awk '{print $1}')
GOT_U=$(S175 "md5sum /home/ubuntu/smart-locker/routes/user.py" | awk '{print $1}')
GOT_D=$(S175 "md5sum /home/ubuntu/smart-locker/static/deposit.html" | awk '{print $1}')
echo "   一致性校验:"
[ "$GOT_H" = "$PRE_H" ] && echo "     ✅ helpers.py 与 106 改前一致" || { echo "     ⚠️ helpers.py 不一致: 175=$GOT_H 106改前=$PRE_H"; }
[ "$GOT_U" = "$PRE_U" ] && echo "     ✅ routes/user.py 与 106 改前一致" || { echo "     ⚠️ user.py 不一致: 175=$GOT_U 106改前=$PRE_U"; }
[ "$GOT_D" = "$PRE_D" ] && echo "     ✅ static/deposit.html 与 106 改前一致" || { echo "     ⚠️ deposit.html 不一致: 175=$GOT_D 106改前=$PRE_D"; }
echo -n "     公众号预设开关 oa_subscribe_enabled: "
S175 "python3 -c \"import sys; sys.path.insert(0,'/home/ubuntu/smart-locker'); from helpers import get_setting; print(repr(get_setting('oa_subscribe_enabled','false')))\""
echo -n "     兜底开关 oa_notify_fallback_enabled: "
S175 "python3 -c \"import sys; sys.path.insert(0,'/home/ubuntu/smart-locker'); from helpers import get_setting; print(repr(get_setting('oa_notify_fallback_enabled','(未设置->默认true)')))\""

echo
echo "== 3) 备份（175） =="
S175 "cd /home/ubuntu/smart-locker && mkdir -p $BK/routes $BK/static && cp -a helpers.py $BK/ && cp -a routes/user.py $BK/routes/ && cp -a static/deposit.html $BK/static/ && ls -l $BK $BK/routes $BK/static"

echo
echo "== 4) 推代码 =="
scp -i ~/.ssh/prod_key helpers.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp helpers 失败'; exit 1; }
tar cf - routes/user.py static/deposit.html | S175 "cd /home/ubuntu/smart-locker && tar xf -" || { echo '[中止] tar 失败'; exit 1; }
FAIL=0
for f in $FILES; do
  want=$(md5sum "$f" | awk '{print $1}')
  got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "   ✅ $f"; else echo "   ❌ $f 期望 $want 实得 $got"; FAIL=$((FAIL+1)); fi
done
[ $FAIL -gt 0 ] && { echo '[中止] 文件不一致，不重载'; exit 1; }
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile helpers.py routes/user.py && echo '   175 语法通过'"

echo
echo "== 5) 零停机重载 =="
S175 "curl -s -o /dev/null -w '   重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '   已发 HUP: '\$MPIDS"
sleep 12
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"   就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"

echo
echo "== 6) 上线后核对 =="
S175 "echo -n '   兜底代码在? _OA_SUB_TPL: '; grep -c _OA_SUB_TPL /home/ubuntu/smart-locker/helpers.py; echo -n '   状态接口在? oa-subscribe-status: '; grep -c 'oa-subscribe-status' /home/ubuntu/smart-locker/routes/user.py; echo -n '   H5 新弹层在? oaSubPrompt: '; grep -c oaSubPrompt /home/ubuntu/smart-locker/static/deposit.html"
echo "   --- /store 页面(SSR 后) ---"
S175 "curl -s 'http://127.0.0.1:5001/store?device=101117' | grep -o 'oaSubPrompt\|oaSubWrap\|OA_SUBSCRIBE_ENABLED = [a-z]*' | sort | uniq -c"
echo "   --- 订阅状态接口 ---"
S175 "curl -s 'http://127.0.0.1:5001/api/user/oa-subscribe-status?phone=18888889999'; echo"
echo "   --- 外网页面 ---"
S175 "for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store; do printf '   %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done"
echo "   --- 重载后报错扫描(应无内容) ---"
S175 "sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -3"
echo
echo "备份: 175 $BK    (还原: 用 $BK 里的 3 个文件覆盖回去 + 重载)"
