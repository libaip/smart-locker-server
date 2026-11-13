#!/usr/bin/env bash
# ============================================================
# 备用机一键同步脚本 (在 175.178.156.121 上运行)
# 用途: 主库 106.55.7.10 的【代码 + 配置】变更后, 手动同步到备用机
# 注意: 数据库数据由逻辑复制 lh_sub 自动实时同步, 本脚本不处理数据
# 用法: bash /home/ubuntu/smart-locker/scripts/sync_from_primary.sh
# 依赖: 备用机 ubuntu 可免密 ssh 主库(已配置), sudo 免密
# ============================================================
set -e
PRIMARY=ubuntu@106.55.7.10
TS=$(date +%Y%m%d_%H%M%S)
SSHOPT="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

echo "########## 1/6 代码树同步 (smart-locker, 按内容校验) ##########"
cd /home/ubuntu/smart-locker
EXCL=(--exclude=backup/ --exclude=backups/ --exclude=backup_v1.07_.tar.gz --exclude=backup_mp_openid_20260803.dump --exclude=backup_20260803_1600_login_fix.dump --exclude=backup_v1410_20260813_203157/ --exclude=backup_subscribe_page_20260814_071201/ --exclude=backup_20260710/ --exclude=backup_20260718_unionid/ --exclude=backup_20260729/ --exclude=myfix_*/ --exclude=logs/ --exclude=__pycache__/ --exclude='*.pyc' --exclude='*.log' --exclude='*.tar.gz' --exclude='*.dump' --exclude='*.db' --exclude='*.bak*' --exclude='.git/' --exclude='*.swp')
rsync -acv "${EXCL[@]}" $PRIMARY:/home/ubuntu/smart-locker/ /home/ubuntu/smart-locker/

echo "########## 2/6 nginx 配置 ##########"
scp -q $SSHOPT $PRIMARY:/etc/nginx/nginx.conf /tmp/nginx.conf
sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak.$TS
sudo mv /tmp/nginx.conf /etc/nginx/nginx.conf
scp -q $SSHOPT $PRIMARY:/etc/nginx/sites-enabled/locker-cqdyxl /tmp/locker-cqdyxl
sudo cp /etc/nginx/sites-enabled/locker-cqdyxl /etc/nginx/sites-enabled/locker-cqdyxl.bak.$TS
sudo mv /tmp/locker-cqdyxl /etc/nginx/sites-enabled/locker-cqdyxl
# 对齐主库: ws-direct 保持禁用(不参与加载)
if [ -f /etc/nginx/sites-enabled/ws-direct ]; then
  sudo mv /etc/nginx/sites-enabled/ws-direct /etc/nginx/sites-enabled/ws-direct.disabled
  echo "  -> ws-direct 已改为禁用"
fi
# 清理 sites-enabled 里遗留的 .bak(避免 nginx 重复加载)
sudo mkdir -p /etc/nginx/backups
sudo mv /etc/nginx/sites-enabled/*.bak* /etc/nginx/backups/ 2>/dev/null || true
sudo nginx -t && sudo systemctl reload nginx

echo "########## 3/6 systemd 服务 ##########"
for unit in smart-locker.service ws-proxy.service; do
  if scp -q $SSHOPT $PRIMARY:/etc/systemd/system/$unit /tmp/$unit 2>/dev/null; then
    if [ -f /etc/systemd/system/$unit ]; then sudo cp /etc/systemd/system/$unit /etc/systemd/system/$unit.bak.$TS; fi
    sudo mv /tmp/$unit /etc/systemd/system/$unit
    echo "  -> $unit 已更新"
  fi
done
sudo systemctl daemon-reload

echo "########## 4/6 .htpasswd (admin 认证) ##########"
scp -q $SSHOPT $PRIMARY:/etc/nginx/.htpasswd /tmp/htpasswd
sudo mv /tmp/htpasswd /etc/nginx/.htpasswd

echo "########## 5/6 crontab (standby 安全版) ##########"
crontab -l > /tmp/cron_before.bak.$TS 2>/dev/null || true
ssh $SSHOPT $PRIMARY crontab -l > /tmp/primary_cron.txt 2>/dev/null || echo "" > /tmp/primary_cron.txt
# standby 安全过滤: 注释会写库/退款/自动拉起应用的作业(故障切换前不要启用)
awk '{ if ($0 ~ /complaint_auto\.py|health_watchdog\.sh|check_identity_consistency\.py|pull_channel_balance\.py/) { print "# STANDBY-DISABLED(切换主备后再启用): " $0 } else { print $0 } }' /tmp/primary_cron.txt > /tmp/standby_cron.txt
{
  echo "# 本机为备用机(逻辑复制副本), crontab 由 sync_from_primary.sh 从主库过滤生成"
  echo "# 过滤规则: complaint_auto.py / health_watchdog.sh / check_identity_consistency.py / pull_channel_balance.py"
  cat /tmp/standby_cron.txt
} > /tmp/standby_cron_final.txt
crontab /tmp/standby_cron_final.txt
echo "  -> crontab 已安装(写库/退款/自拉起作业已注释), 原备份: /tmp/cron_before.bak.$TS"

echo "########## 6/6 Python 依赖检查 ##########"
if python3 -c "import flask, psycopg2, requests, cryptography, openpyxl, flask_login, websocket" 2>/dev/null; then
  echo "  -> 依赖 OK"
else
  echo "  !! 依赖缺失, 请手动: pip3 install --user openpyxl==3.1.5 flask-login==0.6.3 websocket-client==1.9.0 pillow==12.2.0 cryptography==49.0.0 requests==2.34.2"
fi

echo ""
echo "=========================================="
echo "同步完成。提醒:"
echo "  1. 数据库数据由逻辑复制自动同步, 无需本脚本处理"
echo "  2. 备用机应用服务保持未启动(只读副本); 故障切换时才 systemctl start smart-locker"
echo "  3. 故障切换后请人工复核 crontab 并启用被注释的作业"
echo "  4. 若主库新增了 Python 依赖, 需按 6/6 提示补装"
echo "=========================================="
