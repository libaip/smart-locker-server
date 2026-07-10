#!/bin/bash
# 连接池监控 + HTTP健康检查：异常自动重启
IDLE_IN_TX=$(PGPASSWORD=locker_pass_2024 psql -h 127.0.0.1 -U locker_admin -d smart_locker -t -c "SELECT count(*) FROM pg_stat_activity WHERE state='idle in transaction';" 2>/dev/null | tr -d ' ')
TOTAL=$(PGPASSWORD=locker_pass_2024 psql -h 127.0.0.1 -U locker_admin -d smart_locker -t -c "SELECT count(*) FROM pg_stat_activity;" 2>/dev/null | tr -d ' ')

# HTTP健康检查
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5001/admin-v2 2>/dev/null)

# 检查日志里是否有pool exhausted
POOL_ERR=$(sudo journalctl -u smart-locker --since '5 minutes ago' --no-pager 2>/dev/null | grep -c 'pool exhausted')

LOG=/home/ubuntu/smart-locker/pool_monitor.log
echo "$(date '+%Y-%m-%d %H:%M:%S') total=$TOTAL idle_in_tx=$IDLE_IN_TX http=$HTTP_CODE pool_err=$POOL_ERR" >> $LOG
tail -500 $LOG > $LOG.tmp && mv $LOG.tmp $LOG

RESTART=0
# 阈值触发
[ "$IDLE_IN_TX" -gt 5 ] 2>/dev/null && RESTART=1
[ "$TOTAL" -gt 80 ] 2>/dev/null && RESTART=1
# HTTP不通触发
[ "$HTTP_CODE" = "000" ] && RESTART=1
[ "$HTTP_CODE" = "502" ] && RESTART=1
# 日志有pool exhausted超过10次触发
[ "$POOL_ERR" -gt 10 ] 2>/dev/null && RESTART=1

if [ "$RESTART" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ALERT: auto-restart (total=$TOTAL idle_in_tx=$IDLE_IN_TX http=$HTTP_CODE pool_err=$POOL_ERR)" >> $LOG
    sudo systemctl restart smart-locker
fi
