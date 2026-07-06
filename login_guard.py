import time
_attempts={}
MAX=20
LOCK=60
def check_rate(ip):
    now=time.time()
    if ip in _attempts:
        c,t=_attempts[ip]
        if c>=MAX:
            if now-t<LOCK: return False,int(LOCK-(now-t))
            del _attempts[ip]
    return True,0
def fail(ip):
    now=time.time()
    if ip not in _attempts: _attempts[ip]=[1,now]
    else: _attempts[ip][0]+=1
def ok(ip): _attempts.pop(ip,None)
def left(ip): return MAX-_attempts[ip][0] if ip in _attempts else MAX
