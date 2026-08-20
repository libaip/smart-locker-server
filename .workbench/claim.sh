#!/bin/bash
# ============================================
# claim.sh - 开工登记（工作台）
# 用法：bash .workbench/claim.sh "改动说明" "文件1,文件2"
# 效果：
#   1. TASKS.md 追加一行任务（状态=进行中）
#   2. claims/ 下生成 md5 基线文件（deploy_check.sh 部署前比对用）
# ============================================
set -u
WB="/home/ubuntu/smart-locker/.workbench"
DESC="${1:?用法: claim.sh \"说明\" \"文件1,文件2\"}"
FILE_LIST="${2:?用法: claim.sh \"说明\" \"文件1,文件2\"}"
TS=$(date +%m-%d" "%H:%M)
TID="T-$(date +%s)"
mkdir -p "$WB/claims"

# 会话编号：按 TASKS.md 已有行数自动递增
N=$(grep -c "| T-" "$WB/TASKS.md" 2>/dev/null || echo 0)
SID="S$((N+1))"

# 1) 登记 TASKS.md
echo "| $TID | $SID | $FILE_LIST | 进行中 | $TS | | $DESC |" >> "$WB/TASKS.md"

# 2) 生成 md5 基线
MDF="$WB/claims/$TID.md5"
echo "TASK=$TID" > "$MDF"
echo "SID=$SID" >> "$MDF"
echo "DESC=$DESC" >> "$MDF"
echo "FILES=$FILE_LIST" >> "$MDF"
IFS=',' read -ra FARR <<< "$FILE_LIST"
for f in "${FARR[@]}"; do
  f=$(echo "$f" | xargs)
  if [ -f "$f" ]; then
    echo "MD5|$f|$(md5sum "$f" | awk '{print $1}')" >> "$MDF"
  else
    echo "MD5|$f|MISSING" >> "$MDF"
  fi
done

echo "✅ 已登记任务 $TID（$SID）: $DESC"
echo "   文件: $FILE_LIST"
echo "   注意: 部署前必须跑 bash .workbench/deploy_check.sh $FILE_LIST"
