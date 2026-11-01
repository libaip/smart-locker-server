#!/bin/bash
# 工作台强制规则：commit message 必须带会话编号（by S<n>）
# 安装后由 pre-commit 调用；也可作为 pre-push 钩子
MSG_FILE="$1"
if [ ! -f "$MSG_FILE" ]; then
  exit 0
fi
MSG=$(cat "$MSG_FILE")
if ! echo "$MSG" | grep -qE 'by[ _]S[0-9]+'; then
  echo ""
  echo "======================================================"
  echo "❌ 提交被拦截（工作台规则）"
  echo "   commit message 必须带会话编号：by S1 / by_S2 ..."
  echo ""
  echo "   示例：git commit -m 'fix: xxx by S2'"
  echo "   编号来源：bash .workbench/claim.sh 自动分配"
  echo "======================================================"
  exit 1
fi
exit 0
