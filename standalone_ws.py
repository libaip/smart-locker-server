#!/usr/bin/env python3
import os, sys, json, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler
import gevent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WS-Daemon] %(levelname)s: %(message)s")
logger = logging.getLogger("ws_daemon")
connected_devices = {}

def handle_ws(environ, start_response):
    ws = environ.get("wsgi.websocket")
    if not ws:
        start_response("400 Bad Request", []); return [b"WS required"]
    device_id = ""
    for p in environ.get("QUERY_STRING","").split("&"):
        if p.startswith("device_id="): device_id = p.split("=",1)[1]; break
    if not device_id: ws.close(); return []
    logger.info(f"[连接] {device_id}")
    old = connected_devices.get(device_id)
    if old and old is not ws:
        try: old.close()
        except: pass
    connected_devices[device_id] = ws
    try:
        ws.send(json.dumps({"type":"register_ack","device_id":device_id}))
        while not ws.closed:
            msg = ws.receive()
            if msg is None: break
            try:
                d = json.loads(msg)
                if d.get("type")=="heartbeat": ws.send(json.dumps({"type":"heartbeat_ack"}))
                elif d.get("type") in ("lock_result","open_lock_result"):
                    logger.info(f"[结果] {device_id} board={d.get('board_no')} lock={d.get('lock_no')} ok={d.get('success')}")
            except: pass
    except Exception as e:
        logger.error(f"[断开异常] {device_id}: {e}")
    finally:
        if connected_devices.get(device_id) is ws: del connected_devices[device_id]
        logger.info(f"[断开] {device_id}")
    return []

def handle_api(environ, start_response):
    if environ["REQUEST_METHOD"]!="POST" and environ.get("PATH_INFO","")!="/status":
        start_response("405",[]); return b""
    path = environ.get("PATH_INFO","")
    if path=="/send": return api_send(environ,start_response)
    if path=="/status":
        data=json.dumps({"devices":list(connected_devices.keys()),"count":len(connected_devices)})
        start_response("200 OK",[("Content-Type","application/json")])
        return [data.encode()]
    start_response("404",[]); return b""

def api_send(environ, start_response):
    try:
        length=int(environ.get("CONTENT_LENGTH",0))
        body=json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
        did=body.get("device_id"); cmd=body.get("command",{})
        if not did or not cmd:
            start_response("400",[("Content-Type","application/json")])
            return [json.dumps({"success":False}).encode()]
        sent=False
        if did in connected_devices:
            try:
                connected_devices[did].send(json.dumps(cmd))
                sent=True
                logger.info(f"[发送] {did} type={cmd.get('type','')}")
            except Exception:
                if connected_devices.get(did) is not None: del connected_devices[did]
        start_response("200 OK",[("Content-Type","application/json")])
        return [json.dumps({"success":sent}).encode()]
    except Exception as e:
        logger.error(f"[API] {e}")
        start_response("500",[("Content-Type","application/json")])
        return [json.dumps({"success":False,"error":str(e)}).encode()]

def main():
    ws=pywsgi.WSGIServer(("0.0.0.0",5003),handle_ws,handler_class=WebSocketHandler)
    api=pywsgi.WSGIServer(("127.0.0.1",5004),handle_api)
    logger.info("独立WS服务: 5003(设备) 5004(API)")
    gevent.joinall([gevent.spawn(ws.serve_forever),gevent.spawn(api.serve_forever)])

if __name__=="__main__": main()