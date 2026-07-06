import subprocess, sys
p = subprocess.Popen(
    [sys.executable, '/home/ubuntu/.local/bin/gunicorn',
     '--workers', '2',
     '--bind', '127.0.0.1:5001',
     '--worker-class', 'geventwebsocket.gunicorn.workers.GeventWebSocketWorker',
     '--timeout', '120',
     'app:app'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True
)
print('OK')
