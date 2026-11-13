#!/bin/bash
TOKEN="43993c7f92d14ebd8762dc08d34e6151"
LOG=/home/ubuntu/smart-locker/logs/watchdog.log
mkdir -p /home/ubuntu/smart-locker/logs
LOGS=$(journalctl -u smart-locker --since="5 minutes ago" --no-pager 2>/dev/null)
ERRORS=$(echo "$LOGS" | grep -ci "error|exception|traceback")
PAYFAIL=$(echo "$LOGS" | grep -c "退款失败|支付异常")
NAMEERR=$(echo "$LOGS" | grep -c "is not defined")
ALERTS=""
if [ "$ERRORS" -gt 50 ]; then ALERTS="${ALERTS}
【错误暴增】5分钟内${ERRORS}条错误"
fi
if [ "$NAMEERR" -gt 10 ]; then ALERTS="${ALERTS}
【变量未定义】${NAMEERR}次"
fi
if [ "$PAYFAIL" -gt 5 ]; then ALERTS="${ALERTS}
【支付异常】${PAYFAIL}次"
fi
if [ -n "$ALERTS" ]; then
  curl -s -X POST "https://www.pushplus.plus/send" -H "Content-Type: application/json" -d "{\"token\":\"$TOKEN\",\"title\":\"系统告警\",\"content\":\"$ALERTS\"}" >/dev/null 2>&1
fi
echo "[$(date "+%Y-%m-%d %H:%M:%S")] E=$ERRORS P=$PAYFAIL N=$NAMEERR" >> $LOG
