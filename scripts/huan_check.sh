#!/bin/bash
# 换小程序 · 第1步：上线前自检（结果会推到你微信）
# 用法： bash scripts/huan_check.sh            # 检查"当前生效"的小程序
#        bash scripts/huan_check.sh wx新的appid # 检查指定小程序
cd /home/ubuntu/smart-locker || exit 1
APPID="${1:-}"
echo "=================================================="
echo "  换小程序 · 第1步：上线前自检"
echo "=================================================="
if [ -n "$APPID" ]; then
  python3 scripts/switch_check.py --appid "$APPID" --push
else
  python3 scripts/switch_check.py --push
fi
RC=$?
echo ""
echo "=================================================="
if [ $RC -eq 0 ]; then
  echo "  >>> 结论：全绿，可以上线"
  echo "  >>> 上线后跑：bash scripts/huan_after.sh"
else
  echo "  >>> 结论：有红项，先别上线"
  echo "  >>> 上面标 ❌ 的每一条，就是要修的点"
fi
echo "=================================================="
exit $RC
