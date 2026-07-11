#!/bin/bash
cd /home/ubuntu/smart-locker
PID=$(pgrep -f 'gunicorn.*bind 127.0.0.1:5001' | sort -n | head -1)
if [ -n "$PID" ]; then
    kill -HUP $PID
    echo "Graceful reload (HUP sent to PID $PID)"
else
    nohup /usr/bin/python3 /home/ubuntu/.local/bin/gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 8 --timeout 120 --bind 127.0.0.1:5001 --error-logfile /home/ubuntu/smart-locker/gunicorn.log --access-logfile /home/ubuntu/smart-locker/gunicorn-access.log app:app > /dev/null 2>&1 &
    sleep 2
    echo "Fresh start"
fi
