"""锁控板控制测试脚本 - 直接通过WebSocket发开门指令"""
import sys
import time

sys.path.insert(0, '/home/ubuntu/smart-locker')

from app import app
from helpers import send_open_lock

DEVICE_ID = '123456'
BOARD_NO = 1     # 你功版小1，对应板1
LOCK_NO = 1      # 测第1个格子
PROTOCOL = 'YBM'
ORDER_ID = f'test_{int(time.time())}'

with app.app_context():
    print(f"️ 发开门指令: device={DEVICE_ID}, board={BOARD_NO}, lock={LOCK_NO}")
    print(f"   protocol={PROTOCOL}, order_id={ORDER_ID}")
    print()

    result = send_open_lock(DEVICE_ID, BOARD_NO, LOCK_NO, PROTOCOL, ORDER_ID)

    if result is True:
        print("️ 指巻 已通过WebSocket发出，等待锁控板响应...")
    elif result is False:
        print("️  设备不在线！APK未连接WebSocket")
    else:
        print(f"⎉️ 返回: {result}")