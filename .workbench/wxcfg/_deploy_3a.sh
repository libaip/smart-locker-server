#!/bin/bash
# ============================================================
# [T-1789258766] 3A：把配置中心"只加不改"地装到 175 生产（第1步，不做 3B）
#   在 106 上执行。所有对 175 的写操作都有备份、有校验、可回滚。
# ============================================================
set -u
cd /home/ubuntu/smart-locker || exit 1
TOOLS=/tmp/wxcfg_deploy
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=cfg3a_$TS
PHASE=${1:-dry}          # dry = 只跑到"干跑+备份+推文件"；real = 真改 + 灌种子 + 重载 + 验证
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }
echo "阶段: $PHASE  （dry=到干跑为止，生产零改动；real=真正上线）"
if [ "$PHASE" = "real" ]; then
  # 真改阶段复用一个固定备份目录名，保证回滚点和干跑阶段一致
  BKFILE=/tmp/wxcfg_3a_bkname.txt
  if [ -f "$BKFILE" ]; then BKNAME=$(cat "$BKFILE"); echo "沿用干跑阶段建的备份目录: backups/$BKNAME"; fi
else
  echo "$BKNAME" > /tmp/wxcfg_3a_bkname.txt
fi

echo "######################################################################"
echo "# 0) 出发前自检（106 源码）"
echo "######################################################################"
echo "106 git HEAD: $(git log --oneline -1)"
echo "待推送文件 md5:"
md5sum wx_config.py wx_config_api.py static/wx_accounts_page.js static/wx_accounts_page.css
echo "期望: ee63a44f2cadb34fce145245a28293e0  wx_config.py"
echo "      1e04d902485495555ab2cf95fce9f5bf  wx_config_api.py"
echo "      43149651ab66dd354e9c2a12e22f176b  static/wx_accounts_page.js"
echo "      55c92d3864cea93050b1a8c34f786e9d  static/wx_accounts_page.css"
for f in wx_config.py wx_config_api.py; do
  python3 -c "import py_compile; py_compile.compile('$f', cfile='/tmp/_c.pyc', doraise=True)" || { echo "[中止] $f 语法不过"; exit 1; }
done
for f in static/wx_accounts_page.js static/wx_accounts_page.css; do
  [ -s "$f" ] || { echo "[中止] $f 是空文件"; exit 1; }
done
grep -q 'wx-accounts' static/wx_accounts_page.js || { echo "[中止] 静态页面里没找到 wx-accounts"; exit 1; }
echo "两个 .py 语法通过；两个静态文件非空；静态页面里有 wx-accounts 组件"
echo "--- 逐个核对 md5（不一致就中止）---"
check_md5() {
  got=$(md5sum "$1" | awk '{print $1}')
  if [ "$got" = "$2" ]; then echo "   ✅ $1"; else echo "   ❌ $1 期望 $2 实得 $got"; exit 1; fi
}
check_md5 wx_config.py ee63a44f2cadb34fce145245a28293e0
check_md5 wx_config_api.py 1e04d902485495555ab2cf95fce9f5bf
check_md5 static/wx_accounts_page.js 43149651ab66dd354e9c2a12e22f176b
check_md5 static/wx_accounts_page.css 55c92d3864cea93050b1a8c34f786e9d
echo

echo "######################################################################"
echo "# 1) 从 106 的 app.py 抽出"注册段"源区域"
echo "######################################################################"
python3 - <<'PY'
A = "logger.error(f'[注册] 注册蓝图 {bp.name} 失败: {e}')"
C_MARK = "# 兼容微信支付投诉回调路径（商户1747572495配置的URL前缀不同）"
t = open('/home/ubuntu/smart-locker/app.py', encoding='utf-8').read()
assert t.count(A) == 1, 'A 锚点 %d 次' % t.count(A)
assert t.count(C_MARK) == 1, 'C 锚点 %d 次' % t.count(C_MARK)
i = t.index(A); a_end = t.index('\n', i + len(A)) + 1
j = t.index(C_MARK); c_start = t.rindex('\n', 0, j) + 1
region = t[a_end:c_start]
open('/tmp/wxcfg_block_region.txt', 'w', encoding='utf-8').write(region)
print('抽出 %d 行，起始行号 %d' % (region.rstrip('\n').count('\n') + 1, t[:a_end].count('\n') + 1))
for k, ln in enumerate(region.rstrip('\n').split('\n'), 1):
    print('   %2d| %s' % (k, ln))
PY
md5sum /tmp/wxcfg_block_region.txt
echo

echo "######################################################################"
echo "# 2) 175 改前状态（记录 + 校验锚点）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && echo '--- 175 改前 md5 ---' && md5sum app.py static/admin-v2.html && echo '--- app.py 里 wx_config 引用(应为0) ---' && grep -c wx_config app.py; echo '--- 锚点计数(应各1) ---'; grep -c '注册蓝图 {bp.name} 失败' app.py; grep -c '兼容微信支付投诉回调路径' app.py; echo '--- admin-v2.html 锚点(应各1) ---'; grep -c 'static/js/vue.min.js' static/admin-v2.html; grep -c '</head>' static/admin-v2.html; grep -c \"payment-channels',label:'支付渠道'\" static/admin-v2.html; echo '--- 磁盘/服务 ---'; systemctl is-active smart-locker smart-locker-admin nginx; df -h / | tail -1"
echo

echo "######################################################################"
echo "# 3) 在 175 上做备份"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/static && cp -a app.py backups/$BKNAME/app.py && cp -a static/admin-v2.html backups/$BKNAME/static/admin-v2.html && cd backups/$BKNAME && md5sum app.py static/admin-v2.html && ls -la . static"
echo

echo "######################################################################"
echo "# 4) 推文件到 175"
echo "######################################################################"
S175 "mkdir -p /home/ubuntu/wxcfg_tools"
scp -i ~/.ssh/prod_key wx_config.py wx_config_api.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/ || { echo '[中止] scp 引擎文件失败'; exit 1; }
scp -i ~/.ssh/prod_key static/wx_accounts_page.js static/wx_accounts_page.css ubuntu@172.16.0.2:/home/ubuntu/smart-locker/static/ || { echo '[中止] scp 静态页面失败'; exit 1; }
scp -i ~/.ssh/prod_key /tmp/wxcfg_block_region.txt ubuntu@172.16.0.2:/tmp/wxcfg_block_region.txt || { echo '[中止] scp 注册段失败'; exit 1; }
scp -i ~/.ssh/prod_key $TOOLS/wxcfg_3a_patch.py $TOOLS/wxcfg_3a_seed.py $TOOLS/seed_prod.py ubuntu@172.16.0.2:/home/ubuntu/wxcfg_tools/ || { echo '[中止] scp 工具脚本失败'; exit 1; }
echo "--- 175 上核对 md5（必须与 106 一致） ---"
S175 "cd /home/ubuntu/smart-locker && md5sum wx_config.py wx_config_api.py static/wx_accounts_page.js static/wx_accounts_page.css && md5sum /tmp/wxcfg_block_region.txt && cd /home/ubuntu/wxcfg_tools && md5sum *.py"
echo

echo "######################################################################"
echo "# 5) 打补丁：先干跑，再真改"
echo "######################################################################"
echo "================ 干跑 ================"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/wxcfg_3a_patch.py"
echo
if [ "$PHASE" != "real" ]; then
  echo
  echo "######################################################################"
  echo "# 干跑阶段到此为止：175 上一个字节都没改"
  echo "#   （只新增了配置中心自己的文件，以及备份目录 backups/$BKNAME）"
  echo "#   业务代码 app.py / admin-v2.html 保持原样，服务不需要重载"
  echo "# 确认无误后执行:  bash /tmp/wxcfg_deploy/_deploy_3a.sh real"
  echo "######################################################################"
  exit 0
fi
echo "================ 真改 ================"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/wxcfg_3a_patch.py --real"
echo

echo "######################################################################"
echo "# 6) 改后校验（175）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && echo '--- 语法检查 ---' && python3 -c \"import py_compile; py_compile.compile('app.py', cfile='/tmp/_a.pyc', doraise=True); print('app.py 语法 OK')\" && echo '--- 注册段计数(应为1) ---' && grep -c 'wx_config as _wxcfg' app.py && echo '--- admin 页面 ---' && grep -c 'wx_accounts_page.js' static/admin-v2.html && grep -c 'wx_accounts_page.css' static/admin-v2.html && grep -c \"label:'微信账号'\" static/admin-v2.html && echo '--- 与备份的差异行数 ---' && diff backups/$BKNAME/app.py app.py | grep -c '^>' && echo '--- 差异明细 ---' && diff -u backups/$BKNAME/app.py app.py | tail -30"
echo
echo "--- 导入真 app.py 试一下（限时 60s，只 import 不起服务） ---"
S175 "cd /home/ubuntu/smart-locker && timeout 60 python3 -c \"
import app
rules = [str(r) for r in app.app.url_map.iter_rules() if str(r).startswith('/api/wx-config')]
print('配置中心路由 %d 条:' % len(rules))
for r in sorted(rules): print('   ', r)
\" 2>&1 | tail -25"
echo

echo "######################################################################"
echo "# 7) 建表 + 灌种子（175 生产库）"
echo "######################################################################"
S175 "cd /home/ubuntu/smart-locker && python3 /home/ubuntu/wxcfg_tools/wxcfg_3a_seed.py 2>&1 | tail -80"
echo

echo "######################################################################"
echo "# 8) 重载 175（零停机 kill -HUP）"
echo "######################################################################"
echo "--- 重载前健康 ---"
S175 "curl -s -o /dev/null -w '5001=%{http_code} ' http://127.0.0.1:5001/api/health; curl -s -o /dev/null -w '5002=%{http_code}\n' http://127.0.0.1:5002/api/health"
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); echo \"主进程: \$MPIDS\"; for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '已发 HUP'"
echo "--- 等待新 worker 起来 ---"
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"第 \${i} 秒就绪 5001=\$a 5002=\$b\"; break; fi; sleep 1; done; ps -eo pid,ppid,etime,args | grep '[g]unicorn' | awk '\$2==1 {print \"  主进程 \"\$1\" 运行时长 \"\$3}'"
echo

echo "######################################################################"
echo "# 9) 上线后验证"
echo "######################################################################"
S175 "echo '--- 健康检查 ---'; for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
echo '--- 配置中心接口鉴权（不带 token 应为 401/403）---'
for p in 5001 5002; do printf '  无token -> '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/wx-config/accounts; done
echo '--- 带后台 token ---'
curl -s -H 'Authorization: Bearer a4c4e947a2c0bc1959e3fb64a8f02ee4' http://127.0.0.1:5001/api/wx-config/accounts | head -c 400; echo
echo '--- 静态页面 ---'
for f in wx_accounts_page.js wx_accounts_page.css; do printf '  /static/%s -> ' \$f; curl -s -o /dev/null -w '%{http_code} (%{size_download}字节)\n' http://127.0.0.1:5001/static/\$f; done
echo '--- 后台页面里菜单在不在 ---'
curl -s http://127.0.0.1:5001/static/admin-v2.html | grep -o \"label:'微信账号'\" | head -2
echo '--- 走 nginx 的外网地址 ---'
for u in https://locker.cqdyxl.com/api/health https://locker.cqdyxl.com/static/wx_accounts_page.js; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done
echo '--- 业务没受影响：首页/存包页 ---'
for u in / /store; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' https://locker.cqdyxl.com\$u; done
"
echo

echo "######################################################################"
echo "# 3A 结束（未做 3B：业务代码的取值来源还没换，行为与上线前完全一致）"
echo "# 备份目录: backups/$BKNAME"
echo "# 回滚: cp -a backups/$BKNAME/app.py app.py && cp -a backups/$BKNAME/static/admin-v2.html static/admin-v2.html"
echo "######################################################################"
