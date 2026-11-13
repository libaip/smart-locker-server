#!/bin/bash
export PATH=/home/ubuntu/smart-locker/venv/bin:/usr/bin:/usr/local/bin:
cd /home/ubuntu/smart-locker
sudo fuser -k 5001/tcp 2>/dev/null
sleep 1
source venv/bin/activate 2>/dev/null
nohup /usr/bin/python3 /home/ubuntu/.local/bin/gunicorn --env GUNICORN_WORKER_ID=1 --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 8 --timeout 120 --max-requests 5000 --max-requests-jitter 500 --bind 127.0.0.1:5001 app:app > /tmp/gunicorn.log 2>&1 &
echo 'Started: '07/03/2026 12:57:31
