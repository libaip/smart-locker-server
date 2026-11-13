#!/bin/bash
# 微信投诉三段式流程验证脚本 (2026-08-19)
# 用法: ./verify_complaint_flow.sh [订单号]
set -u
ORDER_NO="${1:-}"
echo "==================== 1. 回调日志（最近15分钟） ===================="
journalctl -u smart-locker --since "15 minutes ago" 2>/dev/null | grep "wechat_complaint_notify" | tail -15
echo
echo "==================== 2. 调度器处理日志（最近15分钟） ===================="
journalctl -u smart-locker --since "15 minutes ago" 2>/dev/null | grep "complaint_scheduler" | tail -15
echo
echo "==================== 3. 5分钟cron兜底登记日志（最近15分钟） ===================="
tail -30 /home/ubuntu/smart-locker/logs/complaint_auto.log
echo
if [ -n "$ORDER_NO" ]; then
  echo "==================== 4. 订单与投诉状态 ===================="
  PGPASSWORD=locker_pass_2024 psql -h 127.0.0.1 -p 6432 -U locker_admin -d smart_locker <<SQL
SELECT id, order_no, status, refund_status, refund_id, refund_time, deposit_amount
FROM orders WHERE order_no='$ORDER_NO';
SELECT id, wx_complaint_id, order_no, status, refund_status, refund_fail_reason, refund_retry,
       reply, reply_time, created_at,
       EXTRACT(EPOCH FROM (reply_time - created_at)) AS reply_delay_sec,
       EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_sec
FROM complaints WHERE order_no='$ORDER_NO' OR wx_complaint_id LIKE '%$ORDER_NO%' ORDER BY id DESC;
SQL
fi
echo "==================== 完成 ===================="
echo "预期时间线: 投诉 -> 秒级入库+首响回复(status=1) -> 满5分钟退款 -> 到账通知 -> 结案(status=3)"
echo "退款失败3次 -> 转人工(status=2)，不再自动结案"
