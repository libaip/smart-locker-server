#!/bin/bash
TOKEN="43993c7f92d14ebd8762dc08d34e6151"
LOG=/home/ubuntu/smart-locker/logs/health.log
mkdir -p /home/ubuntu/smart-locker/logs
DATA=""
for cf in /home/ubuntu/smart-locker/cert/*_cert.pem; do
  mch=$(basename "$cf" _cert.pem)
  exp=$(openssl x509 -in "$cf" -noout -enddate 2>/dev/null | cut -d= -f2)
  d=$(openssl x509 -in "$cf" -noout -checkend $((30*86400)) 2>/dev/null && echo "OK" || echo "即将过期")
  DATA="${DATA}[${mch}]${exp}(${d})\n"
done
disk=$(df -h / | tail -1 | awk "{print \$5}" 2>/dev/null)
mem=$(free -m 2>/dev/null | grep Mem | awk "{printf \"%d%%\", \$3/\$2*100}")
DATA="${DATA}\n磁盘:${disk} 内存:${mem}"
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5001/ 2>/dev/null || echo "DOWN")
DATA="${DATA} 服务:${code}"
curl -s -X POST "https://www.pushplus.plus/send" -H "Content-Type: application/json" --data "{\"token\":\"$TOKEN\",\"title\":\"每日巡检报告\",\"content\":\"$DATA\"}" >/dev/null 2>&1
echo "[$(date "+%Y-%m-%d %H:%M:%S")] sent" >> $LOG
