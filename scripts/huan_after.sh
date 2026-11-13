#!/bin/bash
# 换小程序 · 第2步：上线后马上复查（只看最近 2 小时，结果推到你微信）
# 用法： bash scripts/huan_after.sh
cd /home/ubuntu/smart-locker || exit 1
echo "=================================================="
echo "  换小程序 · 第2步：上线后复查（近 2 小时）"
echo "=================================================="
python3 scripts/daily_health.py --hours 2 --sample 15 --push
RC=$?
echo ""
echo "=================================================="
if [ $RC -eq 0 ]; then
  echo "  >>> 结论：新小程序跑得正常"
else
  echo "  >>> 结论：有异常，看上面标 ❌ 的那几项"
fi
echo "=================================================="
exit $RC
