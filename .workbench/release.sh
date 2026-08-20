#!/bin/bash
# ============================================
# release.sh - 任务完成登记（工作台）
# 用法：bash .workbench/release.sh <任务ID>
# 效果：把 TASKS.md 中该任务状态改为已完成（不删行，留历史）
# ============================================
set -u
WB="/home/ubuntu/smart-locker/.workbench"
TID="${1:-}"
if [ -z "$TID" ]; then
  echo "用法: release.sh <任务ID>   （TID 见 TASKS.md 或 claim.sh 输出）"
  exit 1
fi
TS=$(date +%m-%d" "%H:%M)
if grep -q "| $TID |" "$WB/TASKS.md"; then
  sed -i "s/| $TID |\(.*\)| 进行中 |\(.*\)| |/| $TID |\1| 已完成 |\2| $TS |/" "$WB/TASKS.md"
  echo "✅ $TID 已标记完成"
else
  echo "⚠️  找不到任务 $TID"
  exit 1
fi
