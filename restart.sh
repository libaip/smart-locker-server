#!/bin/bash
cd /home/ubuntu/smart-locker

PID=$(pgrep -f 'gunicorn.*app:app' | sort -n | head -1)
if [ -n "$PID" ]; then
    kill -HUP $PID
    echo "Graceful reload (HUP sent to PID $PID)"
else
    nohup /usr/bin/python3 /home/ubuntu/.local/bin/gunicorn --env GUNICORN_WORKER_ID=1 --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 2 --timeout 120 --bind 127.0.0.1:5001 app:app > /tmp/g3.log 2>&1 &
    sleep 2
    echo "Fresh start"
fi
