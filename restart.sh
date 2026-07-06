pkill -9 -f "gunicorn"
sleep 3
cd /home/ubuntu/smart-locker
nohup /usr/bin/python3 /home/ubuntu/.local/bin/gunicorn --env GUNICORN_WORKER_ID=1 --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 2 --timeout 120 --bind 127.0.0.1:5001 app:app > /tmp/g3.log 2>&1 &
echo "DONE"