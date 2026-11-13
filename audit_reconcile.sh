#!/bin/bash
# ============================================
# audit_reconcile.sh - 每日对账巡检（提现记录/订单退款/余额一致性）
# 用法: bash audit_reconcile.sh          # 跑一次，输出到 logs/audit_reconcile.log
# 建议 cron: 每天 0:40 执行（与提现批处理 0:30 错开）
# ============================================
cd /home/ubuntu/smart-locker
LOG=logs/audit_reconcile.log
TS=$(date '+%Y-%m-%d %H:%M:%S')
echo "===== 对账巡检 $TS =====" | tee -a "$LOG"

ISSUES=0

# --- 1. 重复记账：同一订单出现在多条 status=2 提现记录 ---
DUP=$(sudo -u postgres psql -d smart_locker -Atc "
SELECT count(*) FROM (
  SELECT oid FROM withdrawal_records w
  CROSS JOIN LATERAL jsonb_array_elements_text(w.order_ids::jsonb) AS oid
  WHERE w.status=2 GROUP BY oid HAVING count(*)>1
) d;")
echo "[1] 重复记账订单数: $DUP" | tee -a "$LOG"
if [ "${DUP:-0}" != "0" ]; then ISSUES=$((ISSUES+1)); fi

# --- 2. status=2 记录金额与订单退款合计不一致 ---
DIFF=$(sudo -u postgres psql -d smart_locker -Atc "
SELECT count(*) FROM (
  SELECT w.id
  FROM withdrawal_records w
  CROSS JOIN LATERAL jsonb_array_elements_text(w.order_ids::jsonb) AS oid
  LEFT JOIN orders o ON o.id::text = oid
  WHERE w.status=2 AND o.refund_time IS NOT NULL AND COALESCE(o.refund_amount,0)>0
  GROUP BY w.id, w.amount
  HAVING ABS(w.amount - SUM(LEAST(COALESCE(o.refund_amount,0), COALESCE(o.deposit_amount,0)))) > 0.01
) d;")
echo "[2] 提现记录金额不一致: $DIFF" | tee -a "$LOG"
if [ "${DIFF:-0}" != "0" ]; then ISSUES=$((ISSUES+1)); fi

# --- 3. 用户余额负数 ---
NEG=$(sudo -u postgres psql -d smart_locker -Atc "
SELECT count(*) FROM user_balances WHERE balance < -0.01;")
echo "[3] 余额负数用户: $NEG" | tee -a "$LOG"
if [ "${NEG:-0}" != "0" ]; then ISSUES=$((ISSUES+1)); fi

# --- 4. 订单已退款(refund_time有值)但字段不全 ---
INCOMP=$(sudo -u postgres psql -d smart_locker -Atc "
SELECT count(*) FROM orders
WHERE refund_time IS NOT NULL
  AND (refund_status IS DISTINCT FROM 'refunded' OR COALESCE(refund_amount,0)=0)
  AND refund_status NOT IN ('fee_refunded','balance_locked','success','refunding')
  AND NOT (refund_status='none' AND COALESCE(refund_amount,0)=0 AND created_at < '2026-07-01');")
echo "[4] 退款字段不全订单: $INCOMP" | tee -a "$LOG"
if [ "${INCOMP:-0}" != "0" ]; then ISSUES=$((ISSUES+1)); fi

# --- 5. 提现记录"假成功"(status=2但订单未退) ---
FAKE=$(sudo -u postgres psql -d smart_locker -Atc "
SELECT count(*) FROM withdrawal_records w
CROSS JOIN LATERAL jsonb_array_elements_text(w.order_ids::jsonb) AS oid
LEFT JOIN orders o ON o.id::text = oid
WHERE w.status=2 AND (o.refund_time IS NULL OR COALESCE(o.refund_amount,0)=0);")
echo "[5] 假成功提现记录: $FAKE" | tee -a "$LOG"
if [ "${FAKE:-0}" != "0" ]; then ISSUES=$((ISSUES+1)); fi

echo "===== 结束 异常项: $ISSUES/5 =====" | tee -a "$LOG"
exit 0
