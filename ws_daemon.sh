#!/bin/bash
# 独立WebSocket服务启动脚本
PID_FILE=/tmp/standalone_ws.pid
LOG_FILE=/tmp/standalone_ws.log

start() {
    if [ -f "$PID_FILE" ]; then
        old_pid=$(cat "$PID_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "独立WS服务已在运行 (PID $old_pid)"
            return 0
        fi
    fi
    cd /home/ubuntu/smart-locker
    nohup /usr/bin/python3 standalone_ws.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "独立WS服务已启动 (PID $(cat $PID_FILE))"
}

stop() {
    if [ -f "$PID_FILE" ]; then
        kill $(cat "$PID_FILE") 2>/dev/null
        rm -f "$PID_FILE"
        echo "独立WS服务已停止"
    else
        echo "独立WS服务未运行"
    fi
}

status() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "独立WS服务运行中 (PID $pid)"
        else
            echo "PID文件存在但进程已死"
        fi
    else
        echo "独立WS服务未运行"
    fi
}

case "$1" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status) status ;;
    *) echo "用法: $0 {start|stop|restart|status}" ;;
esac
