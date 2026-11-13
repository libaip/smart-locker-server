#!/bin/bash
# 随时体检（默认最近 24 小时，结果推到你微信）
# 用法： bash scripts/health_now.sh        # 近 24 小时
#        bash scripts/health_now.sh 6      # 近 6 小时
cd /home/ubuntu/smart-locker || exit 1
H="${1:-24}"
echo "=================================================="
echo "  体检（近 $H 小时）"
echo "=================================================="
python3 scripts/daily_health.py --hours "$H" --sample 20 --push
RC=$?
echo ""
echo "=================================================="
if [ $RC -eq 0 ]; then
  echo "  >>> 结论：全部正常"
else
  echo "  >>> 结论：有异常，看上面标 ❌ 的那几项"
fi
echo "=================================================="
exit $RC
