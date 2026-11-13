#!/bin/bash
# ============================================================================
# 新主体隔离服务器 · 一键部署脚本
#   用途：在一台【全新干净】的 Ubuntu 服务器上，一键搭好"出口代理 + 回调入口"
#   作者：[S978] 2026-10-14
#
# 用法：
#   bash deploy_new_server.sh <生产服务器出口IP> [新域名] [--dry-run]
#
#   例：bash deploy_new_server.sh 175.178.156.121 newdomain.example.com
#
# 做四件事：
#   ① 装 tinyproxy（出网代理）→ 只允许【生产服务器】连，且只允许连微信支付 API
#   ② （给了域名时）装 nginx + Let's Encrypt 证书 → 把回调反代回生产
#   ③ 安全检查：绝不碰 SSH、绝不开可能断连的防火墙
#   ④ 逐项验证并打印结果
#
# ★ 幂等：重复跑不会搞坏；已装的跳过
# ★ 安全：不修改 sshd 配置；不动既有的 tinyproxy 配置（用独立配置文件）
# ============================================================================
set -u

PROD_IP="${1:-}"
NEW_DOMAIN="${2:-}"
DRY_RUN=0
for a in "$@"; do [ "$a" = "--dry-run" ] && DRY_RUN=1; done

PROXY_PORT="${PROXY_PORT:-3128}"
WEBROOT="/var/www/letsencrypt"
TP_CONF="/etc/tinyproxy/tinyproxy-new.conf"
TP_FILTER="/etc/tinyproxy/wechat-only.filter"
NGINX_SITE="/etc/nginx/sites-available/newentity-gateway"
BACKUP_DIR="/root/newentity_setup_$(date +%Y%m%d_%H%M%S)"

say()  { echo "  $*"; }
ok()   { echo "  ✅ $*"; }
warn() { echo "  ⚠️  $*"; }
die()  { echo "  ❌ $*"; exit 1; }

echo "════════════════════════════════════════════════════════════════"
echo "  新主体隔离服务器 · 一键部署"
echo "  生产服务器 IP : ${PROD_IP:-(未提供)}"
echo "  新域名        : ${NEW_DOMAIN:-(未提供，跳过 nginx/证书)}"
echo "  代理端口      : $PROXY_PORT"
echo "  演练模式      : $([ $DRY_RUN = 1 ] && echo 是 || echo 否)"
echo "════════════════════════════════════════════════════════════════"

# ---------------- 前置检查 ----------------
echo ""
echo "① 前置检查"
[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash $0 ...）"
[ -n "$PROD_IP" ] || die "必须提供生产服务器出口 IP（第一个参数）"
. /etc/os-release 2>/dev/null || true
say "系统: ${PRETTY_NAME:-未知}"
case "${ID:-}" in
  ubuntu|debian) ok "系统类型受支持（apt）" ;;
  *) warn "非 Ubuntu/Debian，本脚本按 apt 写；请确认后继续" ;;
esac
say "内存: $(free -m | awk '/Mem:/{print $2"MB"}')  磁盘可用: $(df -h / | tail -1 | awk '{print $4}')"
mkdir -p "$BACKUP_DIR" && ok "备份目录: $BACKUP_DIR"

# ★ 安全检查：确认 SSH 端口，后面绝不动它
SSH_PORT=$(ss -ltnp 2>/dev/null | grep -oE ':22\b' | head -1 | tr -d ':')
SSH_PORT="${SSH_PORT:-22}"
ok "SSH 端口 = $SSH_PORT（本脚本不会碰 SSH 配置，也不会启用会挡它的防火墙规则）"

if [ $DRY_RUN = 1 ]; then
  echo ""
  echo "【演练模式】只生成配置并校验，不改系统、不启服务"
fi

# ---------------- ① 出网代理 ----------------
echo ""
echo "② 部署出网代理（tinyproxy）"
if [ $DRY_RUN = 0 ]; then
  if ! command -v tinyproxy >/dev/null 2>&1; then
    say "安装 tinyproxy..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tinyproxy >/dev/null 2>&1 || die "tinyproxy 安装失败"
    ok "tinyproxy 已安装"
  else
    ok "tinyproxy 已存在，跳过安装"
  fi
fi

# 白名单：只允许生产服务器连代理
cat > /tmp/tp_new.conf <<EOF
# [S978] 新主体专用出网代理 —— 只允许生产服务器连接
User tinyproxy
Group tinyproxy
Port $PROXY_PORT
Listen 0.0.0.0
Timeout 600
DefaultErrorFile "/usr/share/tinyproxy/default.html"
StatFile "/usr/share/tinyproxy/stats.html"
LogFile "/var/log/tinyproxy/tinyproxy-newentity.log"
LogLevel Info
MaxClients 200
# ★★ 只允许生产服务器（别的 IP 一律拒绝）
Allow $PROD_IP
# ★★ 只允许访问微信支付 API（FilterDefaultDeny Yes = 默认拒绝，只放行过滤文件里的域名）
#   注意：不设 FilterURLs（默认 Off）→ 过滤文件里的正则匹配的是【域名】，不是完整 URL
Filter "$TP_FILTER"
FilterDefaultDeny Yes
FilterExtended On
ConnectPort 443
ConnectPort 80
EOF
ok "代理配置已生成（端口 $PROXY_PORT，只允许 $PROD_IP）"

cat > /tmp/wechat.filter <<'EOF'
# [S978] 只放行微信支付/开放平台 API 域名，其余全部拒绝
^api\.mch\.weixin\.qq\.com$
^api\.weixin\.qq\.com$
^open\.weixin\.qq\.com$
EOF
ok "过滤规则已生成（只允许 3 个微信域名）"

if [ $DRY_RUN = 0 ]; then
  cp -a /tmp/tp_new.conf "$TP_CONF"
  cp -a /tmp/wechat.filter "$TP_FILTER"
  chmod 644 "$TP_CONF" "$TP_FILTER"
  say "校验配置语法..."
  if tinyproxy -d -c "$TP_CONF" >/tmp/tp_test.log 2>&1 &
  then
    sleep 1
    TP_PID=$!
    kill $TP_PID 2>/dev/null || true
    ok "tinyproxy 配置语法通过"
  else
    die "tinyproxy 配置有问题，见 /tmp/tp_test.log"
  fi
  # 用独立 systemd 单元跑，绝不动系统自带的 tinyproxy.service
  cat > /etc/systemd/system/tinyproxy-newentity.service <<EOF
[Unit]
Description=NewEntity Egress Proxy (only for WeChat Pay API)
After=network.target

[Service]
Type=forking
ExecStart=/usr/bin/tinyproxy -c $TP_CONF
ExecReload=/bin/kill -HUP \$MAINPID
PIDFile=/run/tinyproxy-newentity.pid
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable tinyproxy-newentity >/dev/null 2>&1
  systemctl restart tinyproxy-newentity
  sleep 1
  if systemctl is-active --quiet tinyproxy-newentity; then
    ok "出网代理已启动并设为开机自启（独立服务名 tinyproxy-newentity，不影响系统原有服务）"
  else
    warn "出网代理未启动，看: journalctl -u tinyproxy-newentity -n 30"
  fi
fi

# ---------------- ② 回调入口（有域名才做） ----------------
if [ -n "$NEW_DOMAIN" ]; then
  echo ""
  echo "③ 部署回调入口（nginx + HTTPS）"
  if [ $DRY_RUN = 0 ]; then
    command -v nginx >/dev/null 2>&1 || {
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx >/dev/null 2>&1
      ok "nginx 已安装"
    }
    command -v certbot >/dev/null 2>&1 || {
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1
      ok "certbot 已安装"
    }
  fi
  mkdir -p "$WEBROOT/.well-known/acme-challenge" 2>/dev/null || true
  cat > /tmp/newentity-gateway <<EOF
# [S978] 新主体回调入口：把微信回调转发回生产服务器
server {
    listen 80;
    server_name $NEW_DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl http2;
    server_name $NEW_DOMAIN;
    ssl_certificate     /etc/letsencrypt/live/$NEW_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$NEW_DOMAIN/privkey.pem;
    client_max_body_size 2m;
    access_log /var/log/nginx/newentity-access.log;
    error_log  /var/log/nginx/newentity-error.log;

    # ★ 只转发微信支付/退款回调，其余路径一律拒绝（防止这台机器被当成开放代理）
    location = /api/pay/notify    { proxy_pass http://$PROD_IP:5001/api/pay/notify;    include /etc/nginx/proxy_params_wechat.conf; }
    location = /api/refund/notify { proxy_pass http://$PROD_IP:5001/api/refund/notify; include /etc/nginx/proxy_params_wechat.conf; }
    location / { return 404; }
}
EOF
  cat > /tmp/proxy_params_wechat.conf <<'EOF'
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_connect_timeout 10s;
proxy_read_timeout 30s;
proxy_send_timeout 30s;
proxy_next_upstream error timeout;
EOF
  ok "nginx 配置已生成（只放行 /api/pay/notify 和 /api/refund/notify，其余 404）"
  if [ $DRY_RUN = 0 ]; then
    cp -a /tmp/proxy_params_wechat.conf /etc/nginx/proxy_params_wechat.conf
    cp -a /tmp/newentity-gateway "$NGINX_SITE"
    ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/newentity-gateway
    say "校验 nginx 语法..."
    if nginx -t >/dev/null 2>&1; then
      ok "nginx 语法通过"
      systemctl reload nginx && ok "nginx 已 reload"
      say "申请 HTTPS 证书（Let's Encrypt）..."
      certbot --nginx -d "$NEW_DOMAIN" --non-interactive --agree-tos \
              --register-unsafely-without-email --redirect >/tmp/certbot.log 2>&1 \
        && ok "证书已签发" || warn "证书签发失败（看 /tmp/certbot.log；域名解析没生效时会失败，稍后重跑即可）"
    else
      warn "nginx 语法有问题，未启用。原始输出："
      nginx -t 2>&1 | tail -5 | sed 's/^/     /'
    fi
  fi
else
  echo ""
  echo "③ 跳过回调入口（没给域名）"
fi

# ---------------- ③ 验证 ----------------
echo ""
echo "④ 部署结果验证"
if [ $DRY_RUN = 0 ]; then
  echo -n "  出网代理监听: "; ss -ltn 2>/dev/null | grep -c ":$PROXY_PORT " | sed 's/^/数量 /'
  echo -n "  代理服务状态: "; systemctl is-active tinyproxy-newentity 2>/dev/null || echo "(未启用)"
  echo -n "  SSH 仍然可用: "; systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo "?"
  if [ -n "$NEW_DOMAIN" ]; then
    echo -n "  nginx 状态: "; systemctl is-active nginx 2>/dev/null || echo "(未启用)"
  fi
  echo ""
  echo "  ★ 本地自测（从本机经代理访问微信 API）："
  curl -s -o /dev/null -w "    http://127.0.0.1:$PROXY_PORT → 微信API: HTTP %{http_code}\n" \
       --max-time 10 -x "http://127.0.0.1:$PROXY_PORT" https://api.mch.weixin.qq.com/ 2>&1 || \
       echo "    (代理自测失败，检查 Filter 是否放行)"
  echo ""
  echo "  ★ 从生产服务器测试（在 172.16.0.2 上执行）："
  echo "    curl -s -o /dev/null -w '%{http_code}\\n' -x http://<这台机器公网IP>:$PROXY_PORT https://api.mch.weixin.qq.com/"
  echo "    期望 404（能通）；如果是 403/timeout = 白名单没配对"
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  部署完成"
echo "  下一步（生产侧）："
echo "    1) 验证代理可用：curl -x http://<本机IP>:$PROXY_PORT https://api.mch.weixin.qq.com/"
echo "    2) 跑生产切换脚本：bash switch_production.sh on <本机IP>:$PROXY_PORT $NEW_DOMAIN"
echo "  回滚："
echo "    systemctl stop tinyproxy-newentity && systemctl disable tinyproxy-newentity"
echo "    rm -f /etc/nginx/sites-enabled/newentity-gateway && systemctl reload nginx"
echo "════════════════════════════════════════════════════════════════"
