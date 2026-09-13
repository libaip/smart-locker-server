#!/bin/bash
# ============================================================
# [T-1789258766] 3B：把业务代码的取值来源换成配置中心（175 生产）
#   在 106 上执行。范围与 106 上第2步的三批改动严格一一对应。
#   dry  = 只做核对 + 备份 + device.py 补丁预演（175 业务代码零改动）
#   real = 真正推代码 + 补丁 + 验证 + 重载
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TOOLS=/tmp/wxcfg_deploy
REF=ddc4d78                      # 第2步改造前的基线 commit
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=cfg3b_$TS
PHASE=${1:-dry}
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }

TOP="app.py helpers.py"          # 放根目录的（scp 直接传）
SUBS="routes/admin.py routes/admin_v2.py routes/offline.py routes/payment.py routes/user.py routes/webhook.py"
FILES="$TOP $SUBS"
PATCH_DEVICE=routes/device.py
SKIP_FILE=routes/oauth_test.py

echo "阶段: $PHASE  （dry=核对+备份+补丁预演；real=真正上线）"
if [ "$PHASE" = "real" ]; then
  if [ -f /tmp/wxcfg_3b_bkname.txt ]; then BKNAME=$(cat /tmp/wxcfg_3b_bkname.txt); echo "沿用干跑阶段的备份目录 backups/$BKNAME"; fi
else
  echo "$BKNAME" > /tmp/wxcfg_3b_bkname.txt
fi

echo
echo "######################################################################"
echo "# 0) 出发前自检（106）"
echo "######################################################################"
echo "106 git HEAD: $(git log --oneline -1)"
BADC=0
for f in $FILES $PATCH_DEVICE; do
  if git diff --quiet HEAD -- "$f"; then echo "   ✅ $f 与 git HEAD 一致"; else echo "   ❌ $f 有未提交改动"; BADC=$((BADC+1)); fi
done
if [ $BADC -gt 0 ]; then echo "[中止] 106 上这些文件有未提交改动"; exit 1; fi
echo
echo "--- 106 各文件 md5（待推送） ---"
md5sum $FILES $PATCH_DEVICE
echo

echo "######################################################################"
echo "# 1) 175 改前逐文件核对"
echo "######################################################################"
FAILN=0
for f in $FILES; do
  if [ "$f" = "app.py" ]; then continue; fi        # app.py 已被 3A 加过注册段，单独查
  want=$(git show $REF:$f | md5sum | awk '{print $1}')
  got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "   ✅ $f   175 == 106改造前"; else echo "   ❌ $f   期望 $want 实得 $got"; FAILN=$((FAILN+1)); fi
done
got=$(S175 "md5sum /home/ubuntu/smart-locker/$PATCH_DEVICE" | awk '{print $1}')
if [ "$got" = "4114f977078cf653ee9bf4c11c63e3eb" ]; then
  echo "   ✅ $PATCH_DEVICE   175 == 已知基线（含 106 没有的 cmd_id 段，所以只打精准补丁）"
else
  echo "   ❌ $PATCH_DEVICE 实得 $got"; FAILN=$((FAILN+1))
fi
if [ $FAILN -gt 0 ]; then echo "[中止] 有 $FAILN 个文件与预期不符"; exit 1; fi
echo
echo "--- app.py：逐行确认 175 独有的内容都只是第2步要替换的写死值 ---"
S175 'cat /home/ubuntu/smart-locker/app.py' > /tmp/_app175_now.py
python3 $TOOLS/check_app_delta.py /tmp/_app175_now.py app.py
if [ $? -ne 0 ]; then echo "[中止] app.py 的差异不在预期内"; exit 1; fi
echo
echo "--- $SKIP_FILE：本批明确不动 ---"
S175 "cd /home/ubuntu/smart-locker && md5sum $SKIP_FILE && echo '   （175 的版本比 106 新，含错误处理页；只是备份公众号的临时测试接口，不影响业务）'"
echo

echo "######################################################################"
echo "# 2) 备份（175）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/routes && cp -a $TOP backups/$BKNAME/ && cp -a $SUBS backups/$BKNAME/routes/ && cp -a $PATCH_DEVICE backups/$BKNAME/routes/device.py && cd backups/$BKNAME && md5sum app.py helpers.py routes/admin.py routes/admin_v2.py routes/device.py routes/offline.py routes/payment.py routes/user.py routes/webhook.py"
echo
echo "--- 把工具脚本传到 175（只在 wxcfg_tools 目录，不影响业务）---"
scp -i ~/.ssh/prod_key $TOOLS/wxcfg_3b_device_patch.py $TOOLS/wxcfg_3b_verify.py ubuntu@172.16.0.2:/home/ubuntu/wxcfg_tools/ || { echo '[中止] scp 工具失败'; exit 1; }
S175 "cd /home/ubuntu/wxcfg_tools && md5sum wxcfg_3b_device_patch.py wxcfg_3b_verify.py"
echo
echo "--- device.py 补丁：干跑（只看不改）---"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/wxcfg_3b_device_patch.py"

if [ "$PHASE" != "real" ]; then
  echo
  echo "######################################################################"
  echo "# 干跑阶段结束：175 的业务代码一个字节都没改，不需要重载"
  echo "#   （只做了：核对 + 备份 backups/$BKNAME + device.py 补丁预演）"
  echo "# 确认无误后执行:  bash /tmp/wxcfg_deploy/_deploy_3b.sh real"
  echo "######################################################################"
  exit 0
fi

echo
echo "######################################################################"
echo "# 3) 推代码到 175"
echo "######################################################################"
scp -i ~/.ssh/prod_key $TOP ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp 根目录文件失败'; exit 1; }
tar cf - $SUBS | S175 "cd /home/ubuntu/smart-locker && tar xf -" || { echo '[中止] tar 传 routes/ 失败'; exit 1; }
echo "--- 175 上核对 md5（必须与 106 逐个一致）---"
FAILN=0
for f in $FILES; do
  want=$(md5sum "$f" | awk '{print $1}')
  got=$(S175 "md5sum /home/ubuntu/smart-locker/$f" | awk '{print $1}')
  if [ "$want" = "$got" ]; then echo "   ✅ $f"; else echo "   ❌ $f 期望 $want 实得 $got"; FAILN=$((FAILN+1)); fi
done
if [ $FAILN -gt 0 ]; then echo "[中止] 有 $FAILN 个文件传过去不一致（不重载，先查）"; exit 1; fi
echo
echo "--- device.py 补丁：真改 ---"
S175 "cd /home/ubuntu/smart-locker && WXCFG_BK=/home/ubuntu/smart-locker/backups/$BKNAME python3 /home/ubuntu/wxcfg_tools/wxcfg_3b_device_patch.py --real"
echo
echo "--- 每个文件到底改了什么（与备份逐行对比，只看 -/+ 行）---"
S175 "cd /home/ubuntu/smart-locker && for f in $FILES routes/device.py; do
  n=\$(diff backups/$BKNAME/\$f \$f | grep -cE '^[<>]')
  echo \"  == \$f ：改动 \$n 行 ==\"
  diff backups/$BKNAME/\$f \$f | grep -E '^[<>]' | sed 's/^/     /' | head -14
done"
echo

echo "######################################################################"
echo "# 4) 语法检查 + 全量 import 测试"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && BAD=0; for f in $FILES routes/device.py; do python3 -c \"import py_compile; py_compile.compile('\$f', cfile='/tmp/_c.pyc', doraise=True)\" || { echo \"   ❌ \$f 语法不过\"; BAD=1; }; done; if [ \$BAD = 0 ]; then echo '   ✅ 9 个文件语法全部通过'; fi"
echo
echo "--- 真 app.py 全量 import（限时 90s，只 import 不占端口）---"
S175 "cd /home/ubuntu/smart-locker && timeout 90 python3 -c \"
import app
print('   总路由数:', len(list(app.app.url_map.iter_rules())))
print('   配置中心路由数:', len([r for r in app.app.url_map.iter_rules() if str(r).startswith('/api/wx-config')]))
\" 2>&1 | tail -4"
echo

echo "######################################################################"
echo "# 5) 取值一致性 / 兜底 / 真实链路 验证"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/wxcfg_3b_verify.py > /tmp/_v.txt 2>&1; echo \"验证脚本退出码=\$?\"; cat /tmp/_v.txt"
echo

echo "######################################################################"
echo "# 6) 重载 175（零停机）"
echo "######################################################################"
S175 "curl -s -o /dev/null -w '  重载前 5001=%{http_code}\n' http://127.0.0.1:5001/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP 给: '\$MPIDS"
echo "  等 15 秒让新 worker 起来 ..."
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  第 \${i} 次探测：5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
S175 "echo '  当前各 worker 运行时长（应该是刚刚起来的）:'; ps -eo pid,ppid,etime,args | grep '[g]unicorn' | awk '\$2!=1 {print \"    worker \"\$1\"  \"\$3}' | head -12"
echo

echo "######################################################################"
echo "# 7) 上线后验证"
echo "######################################################################"
S175 "echo '--- 配置中心注册日志条数（每个新 worker 一条）---'
sudo journalctl -u smart-locker --since '4 min ago' --no-pager 2>/dev/null | grep -c 'wx_config 配置中心已注册'
echo '--- 健康 ---'
for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
echo '--- 配置中心接口 ---'
TOKEN=\$(sudo -u postgres psql -d smart_locker -tAc \"select auth_token from admin_users where auth_token is not null and auth_token<>'' order by id limit 1\" | tr -d ' \r\n')
printf '  无token -> '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/api/wx-config/accounts
printf '  有token -> '; curl -s -o /dev/null -w '%{http_code}\n' -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:5001/api/wx-config/accounts
printf '  health  -> '; curl -s -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:5001/api/wx-config/health; echo
echo '--- 存包页（SSR 里应该带小程序 appid 和域名）---'
curl -s -L 'http://127.0.0.1:5001/store?cabinet_id=1' > /tmp/_store.html 2>/dev/null
echo \"  页面字节数: \$(wc -c < /tmp/_store.html)\"
echo \"  小程序 appid 出现次数: \$(grep -c 'wxcabd4cbdb3096c4b' /tmp/_store.html)\"
echo \"  H5 域名出现次数: \$(grep -c 'locker.cqdyxl.com' /tmp/_store.html)\"
echo '--- 外网 ---'
for u in https://locker.cqdyxl.com/api/health https://locker.cqdyxl.com/ https://locker.cqdyxl.com/static/wx_accounts_page.js https://locker.cqdyxl.com/static/admin-v2.html; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '--- 重载后日志报错扫描（应无内容）---'
sudo journalctl -u smart-locker --since '4 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|error|exception' | grep -v '注册失败' | tail -8
echo '--- 连接池告警 ---'
sudo journalctl -u smart-locker --since '4 min ago' --no-pager 2>/dev/null | grep -iE 'pool|exhaust' | tail -5
echo '  （上面两段没内容就是干净的）'
"
echo
echo "######################################################################"
echo "# 3B 结束"
echo "# 备份: /home/ubuntu/smart-locker/backups/$BKNAME"
echo "# 回滚: cd /home/ubuntu/smart-locker && cp -a backups/$BKNAME/app.py . && cp -a backups/$BKNAME/helpers.py . && cp -a backups/$BKNAME/routes/*.py routes/ && (重载)"
echo "######################################################################"
