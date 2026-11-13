#!/bin/bash
# [S856-20261011] 重启【两个】服务，确保新代码真正生效。
#
# 背景（踩过的坑）：
#   本机有两个 gunicorn 服务：
#     · smart-locker.service        业务（柜机/小程序）      端口 5001
#     · smart-locker-admin.service  后台管理(/api/admin/*)   端口 5002
#   nginx 把 /api/admin/ 与 /api/admin_v2/ 全部转到 5002。
#   只重启 5001 -> 后台管理页面的改动【不会生效】（曾因此排查了 3 轮）。
#   另外原来的 kill -HUP（优雅重载）不彻底：旧 worker 被柜机长轮询/WebSocket
#   占住迟迟不退出，请求落到旧 worker -> 还是旧代码。所以这里用 systemctl restart。
#
# 用法：./restart.sh
set -u
cd /home/ubuntu/smart-locker || exit 1

echo "=== 重启 业务服务 (5001) ==="
sudo systemctl restart smart-locker
echo "=== 重启 后台管理服务 (5002) ==="
sudo systemctl restart smart-locker-admin

echo "=== 等待启动 ==="
sleep 6

RC=0
for p in 5001 5002; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "http://127.0.0.1:$p/api/health" 2>/dev/null)
  echo "  :$p /api/health -> $code"
  [ "$code" = "200" ] || RC=1
done

echo "=== 进程状态 ==="
ps -eo pid,lstart,cmd 2>/dev/null | grep -E "gunicorn.*500[12]" | grep -v grep | sed 's/^/  /'

if [ "$RC" = "0" ]; then
  echo "✅ 两个服务都已重启，新代码生效"
else
  echo "❌ 有服务健康检查失败，请查看: sudo journalctl -u smart-locker -u smart-locker-admin -n 50"
fi
exit $RC
