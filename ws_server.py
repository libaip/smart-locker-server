"""WebSocket服务 - 与sync worker配合使用（共享connected_devices）"""
import logging
import json
import socket
import os
from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler
from geventwebsocket.exceptions import WebSocketError
from flask import Flask, request

logger = logging.getLogger("ws_server")
from helpers import connected_devices

ws_app = Flask(__name__)

@ws_app.route("/ws/")
def handle_ws():
    ws = request.environ.get("wsgi.websocket")
    if not ws:
        return "WebSocket required", 400
    device_id = request.args.get("device_id", "")
    if not device_id:
        ws.close()
        return "device_id required", 400
    connected_devices[device_id] = ws
    logger.info("[WS] 设备 " + device_id + " 已连接")
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            try:
                data = json.loads(msg)
                logger.info("[WS] ??: " + str(data))
            except Exception:
                pass
    except WebSocketError:
        pass
    finally:
        if device_id in connected_devices:
            del connected_devices[device_id]
        logger.info("[WS] 设备 " + device_id + " 已连接")
    return ""

def start_ws_server():
    port = int(os.environ.get("WS_PORT", 5002))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.close()
    except OSError:
        logger.info("[WS] 端口 %d 已被占用，跳过启动" % port)
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.close()
    except OSError:
        logger.info("[WS] 设备 %d 已连接已连接已连接" % port)
        return
    svr = WSGIServer(("0.0.0.0", port), ws_app, handler_class=WebSocketHandler)
    logger.info("[WS] 已连接?? " + str(port))
    svr.serve_forever()

if __name__ == "__main__":
    start_ws_server()
