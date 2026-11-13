#!/usr/bin/env python3
"""
独立 WebSocket 代理服务（端口 5004）
"""
import json
import gevent
import logging
import time
import os
import sys
import urllib.parse
from datetime import datetime

DB_CONF = {'host':'127.0.0.1','port':6432,'dbname':'smart_locker','user':'locker_admin','password':'locker_pass_2024'}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ws_proxy")

device_connections = {}
device_traffic = {}

lock_results_buffer = []

MAX_RESULT_BUFFER = 5000
HEARTBEAT_TIMEOUT = 120
KEEPALIVE_INTERVAL = 5

# 单条常驻连接复用，带连接/语句超时，断线自动重连
_db_conn = None

def _get_db_conn():
    global _db_conn
    if _db_conn is not None:
        try:
            if _db_conn.closed:
                _db_conn = None
        except Exception:
            _db_conn = None
    if _db_conn is None:
        import psycopg2
        _db_conn = psycopg2.connect(connect_timeout=3, **DB_CONF)
    return _db_conn

def _close_db_conn():
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception:
            pass
        _db_conn = None

def _db_exec(sql, params):
    try:
        co = _get_db_conn()
        cu = co.cursor()
        cu.execute("SET LOCAL statement_timeout = 5000")
        cu.execute(sql, params)
        co.commit()
        cu.close()
        return True
    except Exception as e:
        _close_db_conn()
        logger.error(f"[DB] {e}")
        return False

def _db_st(did, st):
    _db_exec("INSERT INTO devices (device_id,status,update_time) VALUES (%s,%s,NOW()) ON CONFLICT (device_id) DO UPDATE SET status=%s,update_time=NOW()", (did, st, st))
    if st != 'offline':
        _db_exec("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (did,))
    _record_device_alert(did, st)


# 设备上下线告警记录 -> 写入 SQLite locker.db 的 device_alerts 表(与告警管理共用)
_last_alert_state = {}

def _record_device_alert(did, st):
    """设备WS连接建立(online)/断开(offline)时记录告警, 仅状态变化时写, 防刷屏"""
    try:
        prev = _last_alert_state.get(did)
        if prev == st:
            return
        import sqlite3 as _sqlite3
        _db3 = _sqlite3.connect('/home/ubuntu/smart-locker/locker.db', timeout=10)
        _cur3 = _db3.cursor()
        _cur3.execute('''CREATE TABLE IF NOT EXISTS device_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            alert_type TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        if st == 'online':
            _msg = '设备WebSocket连接建立'
        else:
            _msg = '设备WebSocket断开连接'
        _cur3.execute('INSERT INTO device_alerts (device_id, alert_type, detail) VALUES (?,?,?)',
                      (str(did), st, _msg))
        _db3.commit()
        _db3.close()
        _last_alert_state[did] = st
        logger.info(f"[ALERT] 设备{did} 状态变化 -> {st}")
    except Exception as e:
        try:
            _db3.close()
        except Exception:
            pass
        logger.warning(f"[ALERT] 记录失败 {did} {st}: {e}")

def _update_version(device_id, version, version_code=0):
    _db_exec("UPDATE cabinets SET app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s", (version, version_code, device_id))

def _count_traffic(device_id, rx=0, tx=0, rx_msgs=0, tx_msgs=0):
    d = device_traffic.setdefault(device_id, {
        "rx_bytes": 0, "tx_bytes": 0, "rx_msgs": 0, "tx_msgs": 0
    })
    d["rx_bytes"] += rx
    d["tx_bytes"] += tx
    d["rx_msgs"] += rx_msgs
    d["tx_msgs"] += tx_msgs
    return d

def _ws_send(device_id, ws, payload):
    if isinstance(payload, str):
        _count_traffic(device_id, tx=len(payload.encode("utf-8")), tx_msgs=1)
    ws.send(payload)


def _ws_keepalive(ws, device_id):
    """每 5 秒向设备发一次心跳应答，防止 CDN/CLB 空闲回收长连接。"""
    while not ws.closed:
        gevent.sleep(KEEPALIVE_INTERVAL)
        if ws.closed:
            break
        try:
            _ws_send(device_id, ws, json.dumps({"type": "heartbeat_ack", "timestamp": int(time.time() * 1000)}))
        except Exception:
            break


def handle_ws(ws, device_id):
    """处理单个 WebSocket 连接"""
    device_connections[device_id] = ws
    device_traffic.setdefault(device_id, {
        "rx_bytes": 0, "tx_bytes": 0, "rx_msgs": 0, "tx_msgs": 0
    })
    _db_st(device_id, 'online')
    gevent.spawn(_ws_keepalive, ws, device_id)
    logger.info(f"[WS] 设备连接: {device_id}, 当前在线: {len(device_connections)}")
    
    try:
        while not ws.closed:
            try:
                with gevent.Timeout(HEARTBEAT_TIMEOUT):
                    message = ws.receive()
            except gevent.Timeout:
                logger.warning(f"[WS] 心跳超时，清理死连接: {device_id}")
                break
            if message is None:
                break
            _count_traffic(device_id, rx=len(message.encode("utf-8")), rx_msgs=1)
            try:
                msg = json.loads(message)
                t = msg.get("type", "")
                if t == "heartbeat":
                    try:
                        _ws_send(device_id, ws, json.dumps({"type": "heartbeat_ack", "timestamp": int(time.time() * 1000)}))
                    except:
                        pass
                elif t == "lock_result":
                    if len(lock_results_buffer) >= MAX_RESULT_BUFFER:
                        logger.warning(f"[WS] 开锁结果队列已满，丢弃新结果: {device_id}")
                    else:
                        lock_results_buffer.append((device_id, msg))
                elif t == "door_status_result":
                    _forward_door_status(device_id, msg)
                elif t == "register":
                    try:
                        logger.info(f"[WS_REGISTER] device={device_id}, msg={msg}")
                        _ws_send(device_id, ws, json.dumps({"type": "register_ack", "device_id": device_id}))
                        reg_ver = msg.get("version", "")
                        reg_code = msg.get("version_code", 0) or 0
                        if reg_ver:
                            _update_version(device_id, reg_ver, reg_code)
                    except:
                        pass
            except:
                pass
    except:
        pass
    finally:
        if device_id in device_connections and device_connections.get(device_id) is ws:
            del device_connections[device_id]
            _db_st(device_id, 'offline')
        logger.info(f"[WS] 设备断开: {device_id}, 当前在线: {len(device_connections)}")


def flush_lock_results():
    """定时将开锁结果转发给主服务"""
    while True:
        time.sleep(1)
        while lock_results_buffer:
            device_id, data = lock_results_buffer.pop(0)
            try:
                import urllib.request as req
                body = json.dumps({
                    "device_id": device_id,
                    "board_no": data.get("board_no"),
                    "lock_no": data.get("lock_no"),
                    "success": data.get("success", False),
                    "order_id": data.get("orderId") or data.get("order_id", ""),
                    "timestamp": data.get("timestamp", int(time.time() * 1000))
                }).encode()
                req.urlopen("http://127.0.0.1:5001/api/device/lock-result", data=body, timeout=5)
            except Exception as e:
                logger.error(f"[LOCK_RESULT] 转发失败: {e}")


def _forward_door_status(device_id, data):
    """将设备上报的柜门物理状态结果转发给主服务"""
    try:
        import urllib.request as req
        query_ok = bool(data.get("query_success"))
        body = json.dumps({
            "device_id": device_id,
            "request_id": data.get("request_id", ""),
            "board_no": data.get("board_no"),
            "lock_no": data.get("lock_no"),
            "is_open": bool(data.get("is_open", False)),
            "door_status": data.get("door_status", "unknown"),
            "status": data.get("status", "ok" if query_ok else "read_failed"),
            "query_success": query_ok
        }).encode()
        req.urlopen("http://127.0.0.1:5001/api/device/lock-status-report", data=body, timeout=5)
    except Exception as e:
        logger.error(f"[DOOR_STATUS] 转发失败: {e}")


def app(environ, start_response):
    """WSGI 应用"""
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET")
    
    # WebSocket - 从 QUERY_STRING 取 device_id
    if path.startswith("/ws") and method == "GET":
        qs = environ.get("QUERY_STRING", "")
        params = urllib.parse.parse_qs(qs)
        device_id = params.get("device_id", [""])[0]
        # 兜底：从路径取
        if not device_id:
            device_id = path.split("/ws/", 1)[1] if "/ws/" in path else ""
            device_id = device_id.split("?")[0] if "?" in device_id else device_id
        if not device_id:
            start_response("400 Bad Request", [])
            return [b"missing device_id"]
        ws = environ.get("wsgi.websocket")
        if ws:
            handle_ws(ws, device_id)
        return []
    
    # 发送指令接口
    if path == "/send" and method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH", 0))
            body = environ["wsgi.input"].read(length).decode() if length else "{}"
            data = json.loads(body)
            device_id = data.get("device_id")
            command = data.get("command")
            if not device_id or not command:
                start_response("200 OK", [("Content-Type", "application/json")])
                return [json.dumps({"success": False, "error": "missing params"}).encode()]
            ws = device_connections.get(device_id)
            if not ws or ws.closed:
                start_response("200 OK", [("Content-Type", "application/json")])
                return [json.dumps({"success": False, "error": "offline"}).encode()]
            # 同步发送（去掉 gevent.spawn 异步并发），保证逐门指令按 1,2,3... 顺序到达设备
            # 加 3 秒超时保护，避免设备 TCP 缓冲满导致卡死
            try:
                with gevent.Timeout(3):
                    _ws_send(device_id, ws, json.dumps(command))
            except gevent.Timeout:
                logger.warning(f"[SEND] 发送超时 device={device_id}")
                start_response("200 OK", [("Content-Type", "application/json")])
                return [json.dumps({"success": False, "error": "send timeout"}).encode()]
            start_response("200 OK", [("Content-Type", "application/json")])
            return [json.dumps({"success": True}).encode()]
        except Exception as e:
            start_response("200 OK", [("Content-Type", "application/json")])
            return [json.dumps({"success": False, "error": str(e)}).encode()]
    
    # 状态
    if path == "/status":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({
            "online_count": len(device_connections),
            "online_devices": list(device_connections.keys()),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }).encode()]

    # 设备收发字节计数（自进程启动累计）
    if path == "/traffic":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"devices": device_traffic}).encode()]
    
    if path == "/health":
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"OK"]
    
    # 设备在线列表（供主服务查询）
    if path == "/api/devices/online":
        import json as _json
        now = time.time()
        online = []
        for did, ws in device_connections.items():
            try:
                if not ws.closed:
                    online.append(did)
            except:
                pass
        start_response("200 OK", [("Content-Type", "application/json")])
        return [_json.dumps({"online_count": len(online), "devices": online}).encode()]
    
    start_response("404 Not Found", [])
    return [b"Not Found"]


if __name__ == "__main__":
    import threading
    threading.Thread(target=flush_lock_results, daemon=True).start()
    
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    
    port = 5004
    logger.info(f"启动 WS 代理服务, 端口 {port}")
    server = pywsgi.WSGIServer(("0.0.0.0", port), app, handler_class=WebSocketHandler)
    server.serve_forever()
