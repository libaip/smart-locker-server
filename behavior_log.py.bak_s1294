# -*- coding: utf-8 -*-
"""[S1053] 用户行为埋点 —— 精确到每个用户，永久保留

设计要点（避免拖慢主流程）：
  ① 只记录【白名单】动作（查余额/查订单/提现/取件/存件/登录/投诉...）
  ② 写【内存队列】，后台线程每 5 秒或攒够 200 条批量 INSERT
  ③ 队列满则丢弃（绝不阻塞请求）
  ④ 全程 try/except，任何异常都不影响主流程
  ⑤ 手机号自动从 openid 解析（带缓存，避免每次都查库）

用法（在 app.py 里）：
    import behavior_log
    behavior_log.install(app)      # 挂在 app 上
"""
import threading
import time
import logging
from collections import deque

logger = logging.getLogger(__name__)

# ---------------- 配置 ----------------
MAX_QUEUE = 20000            # 队列上限，超过就丢（保护内存）
BATCH_SIZE = 200             # 每批最多写多少条
FLUSH_INTERVAL = 5           # 秒；每 5 秒刷一次

# 白名单：路径片段 -> action 名
#   ★ 只记录"用户侧"的关键动作，管理端/设备端不记
ACTION_MAP = [
    ('/api/user/balance',              'balance'),      # 查余额
    ('/api/user/orders',               'orders'),       # 查订单
    ('/api/user/order-detail',         'order_detail'), # 订单详情
    ('/api/user/wallet/transactions',  'transactions'), # 交易流水
    ('/api/user/wallet/withdrawals',   'withdraw_list'),# 提现记录
    ('/api/user/withdraw',             'withdraw'),     # 发起提现
    ('/api/user/help-refund',          'help_refund'),  # 求助退款
    ('/api/user/info',                 'user_info'),    # 用户信息
    ('/api/user/subscribe-templates',  'sub_templates'),# 订阅模板
    ('/api/retrieve/verify',           'retrieve'),     # 取件验证
    ('/api/deposit/get-pay-params',    'deposit_pay'),  # 存件支付
    ('/api/wx/login',                  'login'),        # 登录
    ('/api/cabinets/by-mainboard',     'cabinet_scan'), # 扫码/柜机
    ('/api/active-orders/by-device',   'active_orders'),# 活动订单
    ('/api/complaints',                'complaint'),    # 提交投诉
]

_queue = deque()
_lock = threading.Lock()
_started = False
_stats = {'in': 0, 'out': 0, 'drop': 0, 'err': 0}

# openid -> phone 缓存（避免每次查库）
_oid2ph = {}
_oid2ph_lock = threading.Lock()
_OID2PH_MAX = 50000


def _match_action(path):
    for frag, act in ACTION_MAP:
        if path.startswith(frag):
            return act
    return None


def _resolve_phone(openid):
    """openid -> 手机号（带缓存）。查不到返回空。"""
    if not openid:
        return ''
    with _oid2ph_lock:
        if openid in _oid2ph:
            return _oid2ph[openid]
    ph = ''
    try:
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT user_phone FROM orders
                     WHERE (mp_openid=%s OR openid=%s) AND COALESCE(user_phone,'')<>''
                     ORDER BY id DESC LIMIT 1""", (openid, openid))
        row = c.fetchone()
        if row:
            ph = (dict(row).get('user_phone') or '')
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        ph = ''
    with _oid2ph_lock:
        if len(_oid2ph) < _OID2PH_MAX:
            _oid2ph[openid] = ph
    return ph


def record(path, openid='', phone='', detail='', ts=None):
    """记录一条行为（非阻塞，失败静默）"""
    try:
        act = _match_action(path or '')
        if not act:
            return
        with _lock:
            if len(_queue) >= MAX_QUEUE:
                _stats['drop'] += 1
                return
            _queue.append((phone or '', openid or '', act, (path or '')[:120],
                           (detail or '')[:200], ts or time.time()))
            _stats['in'] += 1
    except Exception:
        _stats['err'] += 1


def _flush_once():
    """把队列里的一批写库"""
    batch = []
    with _lock:
        while _queue and len(batch) < BATCH_SIZE:
            batch.append(_queue.popleft())
    if not batch:
        return 0
    # 补手机号
    rows = []
    for (ph, oid, act, path, detail, ts) in batch:
        if not ph and oid:
            ph = _resolve_phone(oid)
        rows.append((ph, oid, act, path, detail,
                     time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))))
    try:
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        # 批量插入（psycopg2 execute_values 不一定可用，用手工多值）
        vals = ','.join(['(%s,%s,%s,%s,%s,%s)'] * len(rows))
        flat = []
        for r in rows:
            flat.extend(r)
        c.execute("""INSERT INTO user_behavior_log
                     (openid, user_phone, action, path, method, ip, created_at, source)
                     VALUES """ + vals, tuple(flat))
        conn.commit()
        try:
            conn.close()
        except Exception:
            pass
        with _lock:
            _stats['out'] += len(rows)
        return len(rows)
    except Exception as e:
        _stats['err'] += 1
        if _stats['err'] % 50 == 1:
            logger.warning('[behavior_log] 写库失败: %s', str(e)[:160])
        return 0


def _worker():
    while True:
        try:
            n = _flush_once()
            if n == 0:
                time.sleep(FLUSH_INTERVAL)
        except Exception:
            time.sleep(FLUSH_INTERVAL)


def stats():
    with _lock:
        return dict(_stats), len(_queue)


def install(app):
    """把埋点挂到 Flask app 上"""
    global _started
    try:
        from flask import request as _rq

        @app.before_request
        def _beh_before():
            try:
                # 只处理白名单路径，尽早返回
                p = _rq.path or ''
                if not p.startswith('/api/'):
                    return None
                if not _match_action(p):
                    return None
                # 记到 request 上下文，after_request 里写
                _rq._beh_path = p
                _rq._beh_openid = (_rq.args.get('openid') or '')[:64]
                _rq._beh_t0 = time.time()
            except Exception:
                pass
            return None

        @app.after_request
        def _beh_after(resp):
            try:
                p = getattr(_rq, '_beh_path', None)
                if not p:
                    return resp
                oid = getattr(_rq, '_beh_openid', '') or ''
                # 从响应里尽量不改；detail 记状态码 + 耗时
                t0 = getattr(_rq, '_beh_t0', 0)
                cost = int((time.time() - t0) * 1000) if t0 else 0
                record(p, openid=oid, detail='%s %dms' % (resp.status_code, cost))
            except Exception:
                pass
            return resp

        if not _started:
            t = threading.Thread(target=_worker, daemon=True, name='behavior_log')
            t.start()
            _started = True
            logger.info('[behavior_log] 埋点已启用（队列上限 %d，每 %ds 刷库）', MAX_QUEUE, FLUSH_INTERVAL)
    except Exception as e:
        logger.warning('[behavior_log] 挂载失败: %s', e)
