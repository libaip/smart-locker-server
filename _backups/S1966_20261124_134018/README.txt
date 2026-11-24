S1966 渠道加权+限量改造 备份
时间: 2026-11-24 13:40:18
目的: sequential 模式加分层权重 + 每小时/每日限额

改动内容:
  1. payment_channels 加 hourly_limit 字段 (double precision, default 0)
  2. helpers.py select 渠道的 sequential 分支重写:
     - 统计今日/本小时成功单数
     - 过滤超 daily_limit / hourly_limit 的号
     - 按 rotation_index 分组，组内按 weight 加权随机
     - 全部超量降级选今日最少用量
  3. 配置: 198(蓝) weight=1, 200(辰) weight=9, 各 hourly=15 daily=250

回滚方法:
  1. 代码: cp /home/ubuntu/smart-locker/_backups/S1966_20261124_134018/helpers.py /home/ubuntu/smart-locker/helpers.py
  2. 字段: ALTER TABLE payment_channels DROP COLUMN IF EXISTS hourly_limit;
  3. 配置: psql -d smart_locker -f /home/ubuntu/smart-locker/_backups/S1966_20261124_134018/payment_channels_data.sql (需先TRUNCATE)
  4. 重启: sudo kill -HUP $(pgrep -f 'gunicorn.*5001' | head -1)
