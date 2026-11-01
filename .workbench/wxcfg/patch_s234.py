#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S234-20260918] routes/websocket.py: 设备发来的"任何消息"都刷新心跳（修"假离线"）

背景（2026-09-18 实测）：在线判定是"2 分钟内有心跳"，而心跳只在三处刷新：
  ① WS 连接/注册；② 设备主动发 type='heartbeat'；③ 设备轮询指令/查订单(HTTP)。
走长连接推送的柜机只在注册时刷过一次，之后不再发 heartbeat 消息 -> 被误判离线，
但它们其实完全在线：实测亿航驾校 A/D/E/F 柜今天还在收指令、回传开锁结果（D柜 64 条、A柜 20 条），
全站 6 台属于这种"假离线"（另有铜梁摩托车考场 A、双桥科目四 C区）。

改法：加 _touch_heartbeat()，并在"任何设备消息"入口调用：
  - 原始 WS 大循环（每一种消息类型都经过那里）
  - socketio 的 lock_result / force_update_ack 处理器（register/heartbeat 本来就刷）
带 30 秒节流，避免高频消息把数据库写爆；失败只告警，绝不影响主流程。
"""
import io
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'routes/websocket.py'

HELPER_ANCHOR = "def register_websocket_handlers(socketio):"

HELPER = '''# ============================================
# [S234-20260918] 设备"任何消息"都刷新心跳（修"假离线"）
# 在线判定 = last_heartbeat 在 2 分钟内；但走长连接的设备只在注册时刷过一次、
# 之后不再发 heartbeat 消息 -> 能收指令、能回传开锁结果，却被显示成离线。
# 这里在"任何设备消息"的入口刷新一次（30 秒节流防写爆），失败只告警。
# ============================================
_hb_touch_cache = {}


def _touch_heartbeat(device_id):
    if not device_id:
        return
    try:
        import time as _t
        _did = str(device_id)
        _now = _t.time()
        if _now - _hb_touch_cache.get(_did, 0) < 30:
            return
        _hb_touch_cache[_did] = _now
        from database import get_db
        _db = get_db()
        _db.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (_did,))
        _db.commit()
        _db.close()
    except Exception as _e:
        try:
            logger.warning(f'[WS] 刷新心跳失败: device={device_id}, {_e}')
        except Exception:
            pass


'''

LOCK_OLD = """    def ws_lock_result(data):
        \"\"\"开锁结果上报\"\"\"
        device_id = data.get('device_id') or data.get('deviceId')
        order_id = data.get('order_id')"""
LOCK_NEW = """    def ws_lock_result(data):
        \"\"\"开锁结果上报\"\"\"
        device_id = data.get('device_id') or data.get('deviceId')
        _touch_heartbeat(device_id)      # [S234] 收到开锁结果也算"活着"
        order_id = data.get('order_id')"""

ACK_OLD = """        device_id = data.get('device_id', '')
        accepted = data.get('accepted', False)"""
ACK_NEW = """        device_id = data.get('device_id', '')
        _touch_heartbeat(device_id)      # [S234] 升级确认也算"活着"
        accepted = data.get('accepted', False)"""

RAW_OLD = """                    data = json.loads(msg)
                    t = data.get('type', '')"""
RAW_NEW = """                    data = json.loads(msg)
                    t = data.get('type', '')
                    _touch_heartbeat(data.get('device_id') or device_id)   # [S234] 任何消息都刷新心跳"""


def main():
    s = io.open(P, encoding='utf-8').read()
    before = len(s)
    for tag, old, new in [
        ('1) 插入 _touch_heartbeat()', HELPER_ANCHOR, HELPER + HELPER_ANCHOR),
        ('2) lock_result 刷新', LOCK_OLD, LOCK_NEW),
        ('3) force_update_ack 刷新', ACK_OLD, ACK_NEW),
        ('4) 原始WS 任何消息刷新', RAW_OLD, RAW_NEW),
    ]:
        n = s.count(old)
        if n != 1:
            print('[FAIL] %s: 原文出现 %d 次（要求 1）' % (tag, n))
            sys.exit(2)
        s = s.replace(old, new, 1)
        print('  [ok] %s' % tag)
    if s.count('_touch_heartbeat') != 4:
        print('[FAIL] _touch_heartbeat 出现 %d 次（期望 4 = 定义1 + 调用3）' % s.count('_touch_heartbeat'))
        sys.exit(3)
    io.open(P, 'w', encoding='utf-8', newline='').write(s)
    print('  [DONE] %s: %d -> %d (%+d)' % (P, before, len(s), len(s) - before))


if __name__ == '__main__':
    main()
