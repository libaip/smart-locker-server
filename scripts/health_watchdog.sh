#!/usr/bin/env bash
set -u

STATE=/tmp/health_watchdog_fail
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:5001/api/health || echo 000)

if [ "$CODE" = "200" ]; then
  rm -f "$STATE"
  exit 0
fi

N=0
if [ -f "$STATE" ]; then
  N=$(cat "$STATE" 2>/dev/null || echo 0)
fi
N=$((N + 1))
echo "$N" > "$STATE"

if [ "$N" -ge 3 ]; then
  logger -t health_watchdog "smart-locker health failed 3 times, restarting"
  rm -f "$STATE"
  sudo systemctl restart smart-locker
fi
