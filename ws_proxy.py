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

def _db_st(did,st):
    try:
        import psycopg2
        co = psycopg2.connect(**DB_CONF)
        cu = co.cursor()
        cu.execute("INSERT INTO devices (device_id,status,update_time) VALUES (%s,%s,NOW()) ON CONFLICT (device_id) DO UPDATE SET status=%s,update_time=NOW()", (did,st,st))
        cu.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (did,))
        co.commit()
        cu.close()
        co.close()
    except Exception as e:
        logger.error(f"[DB] {e}")

lock_results_buffer = []

def _update_version(device_id, version, version_code=0):
    try:
        import psycopg2
        co = psycopg2.connect(**DB_CONF)
        cu = co.cursor()
        cu.execute("UPDATE cabinets SET app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                   (version, version_code, device_id))
        co.commit()
        cu.close()
        co.close()
    except Exception as e:
        logger.error(f"[DB_VER] {e}")

def _update_version(device_id, version, version_code=0):
    try:
        import psycopg2
        co = psycopg2.connect(**DB_CONF)
        cu = co.cursor()
        cu.execute("UPDATE cabinets SET app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                   (version, version_code, device_id))
        co.commit()
        cu.close()
        co.close()
    except Exception as e:
        logger.error(f"[DB_VER] {e}")

def _update_version(device_id, version, version_code=0):
    try:
        import psycopg2
        co = psycopg2.connect(**DB_CONF)
        cu = co.cursor()
        cu.execute("UPDATE cabinets SET app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                   (version, version_code, device_id))
        co.commit()
        cu.close()
        co.close()
    except Exception as e:
        logger.error(f"[DB_VER] {e}")

def _update_version(device_id, version, version_code=0):
    try:
        import psycopg2
        co = psycopg2.connect(**DB_CONF)
        cu = co.cursor()
        cu.execute("UPDATE cabinets SET app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                   (version, version_code, device_id))
        co.commit()
        cu.close()
        co.close()
    except Exception as e:
        logger.error(f"[DB_VER] {e}")

def handle_ws(ws, device_id):
    """处理单个 WebSocket 连接"""
    device_connections[device_id] = ws
    _db_st(device_id, 'online')
    logger.info(f"[WS] 设备连接: {device_id}, 当前在线: {len(device_connections)}")
    
    try:
        while not ws.closed:
            message = ws.receive()
            if message is None:
                break
            try:
                msg = json.loads(message)
                t = msg.get("type", "")
                if t == "heartbeat":
                    try:
                        ws.send(json.dumps({"type": "heartbeat_ack", "timestamp": int(time.time() * 1000)}))
                    except:
                        pass
                elif t == "lock_result":
                    lock_results_buffer.append((device_id, msg))
                elif t == "register":
                    try:
                        logger.info(f"[WS_REGISTER] device={device_id}, msg={msg}")
                        ws.send(json.dumps({"type": "register_ack", "device_id": device_id}))
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
                    "order_id": data.get("order_id", ""),
                    "timestamp": data.get("timestamp", int(time.time() * 1000))
                }).encode()
                req.urlopen("http://127.0.0.1:5001/api/device/lock-result", data=body, timeout=5)
            except Exception as e:
                logger.error(f"[LOCK_RESULT] 转发失败: {e}")


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
            gevent.spawn(ws.send, json.dumps(command))
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
