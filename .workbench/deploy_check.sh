#!/bin/bash
# ============================================
# deploy_check.sh - 部署前防覆盖校验（工作台核心）
# 用法：
#   bash .workbench/deploy_check.sh <文件1> [文件2] ...
#   bash .workbench/deploy_check.sh --force <文件1> ...
# 逻辑：
#   1. 开工登记时（claim.sh）已把目标文件的 md5 存到 .workbench/claims/<任务>.md5
#   2. 部署前重新计算服务器当前文件 md5
#   3. 不一致 = 开工后文件被别人动过 → 默认拦截；--force 显式放行
# ============================================
set -u
WB="/home/ubuntu/smart-locker/.workbench"
FORCE=0
FILES=()
for a in "$@"; do
  if [ "$a" = "--force" ]; then FORCE=1; else FILES+=("$a"); fi
done
if [ ${#FILES[@]} -eq 0 ]; then
  echo "用法: bash .workbench/deploy_check.sh [--force] <文件1> [文件2] ..."
  exit 1
fi

BLOCKED=0
for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then echo "⚠️  文件不存在: $f"; continue; fi
  NOW=$(md5sum "$f" | awk '{print $1}')
  # 找对应 claim 记录：按 "^MD5|<文件>|" 精确前缀匹配，避免子串误命中其他任务的文件
  CLAIM=""
  for c in "$WB"/claims/*.md5; do
    [ -f "$c" ] || continue
    if grep -q "^MD5|$f|" "$c" 2>/dev/null; then CLAIM="$c"; break; fi
  done
  if [ -z "$CLAIM" ]; then
    echo "ℹ️  $f 无开工登记记录（直接部署）"
    continue
  fi
  # 只取当前文件 f 自己的基线 md5（不能用 head -1，多文件任务会串行取错行）
  BASELINE=$(awk -F'|' -v ff="$f" '$1=="MD5" && $2==ff {print $3}' "$CLAIM" | head -1)
  if [ "$NOW" = "$BASELINE" ]; then
    echo "✅ $f 校验通过（未被他人修改）"
  else
    echo "❌ $f 开工后被修改过！可能覆盖他人工作。"
    echo "   开工时 md5: $BASELINE"
    echo "   当前   md5: $NOW"
    BLOCKED=1
  fi
done

if [ $BLOCKED -eq 1 ]; then
  if [ $FORCE -eq 1 ]; then
    echo ""
    echo ">>> --force 已确认，放行部署（自行承担覆盖风险）"
    exit 0
  else
    echo ""
    echo ">>> 已拦截：请先确认谁改过这些文件（看 TASKS.md / git log），确认后加 --force 放行"
    exit 2
  fi
fi
exit 0
