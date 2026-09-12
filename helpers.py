"""
智能寄存柜系统 - 共享辅助函数与全局状态
"""
import logging
import random
import string
import json
import hashlib
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
import time
from flask import session, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash

from config import (
    WX_MCH_ID, WX_API_KEY, WX_APP_ID, WX_MP_APP_ID, WX_MP_APP_SECRET,
    WX_CERT_PATH, WX_KEY_PATH, WX_PAY_NOTIFY_URL, WX_REFUND_NOTIFY_URL,
    ORDER_HIDE_SECRET
)
from database import get_db
from models import generate_order_no, generate_access_code

logger = logging.getLogger(__name__)

# ============================================
# ??????
# ============================================
connected_devices = {}         # WebSocket 已连接设备 {device_id: sid}
pending_lock_commands = {}

# 长轮询信号: 每个device_id一个Event，有新指令时set()
import threading as _th
_pending_cmd_events = {}
_pending_cmd_events_lock = _th.Lock()

def is_mp_openid(v):
    """判断是否为(新)小程序 openid：新 appid(伧置 wxcabd4cbdb3096c4b) 前缀 ooTcRx。
    公众号 openid(oLhbm2) 发不了订阅消息，会在此返回 False。
    """
    return bool(v) and str(v).startswith('ooTcRx')


def signal_pending_command(device_id):
    """通知等待中的长轮询请求：有新指令了"""
    with _pending_cmd_events_lock:
        evt = _pending_cmd_events.get(device_id)
        if evt:
            evt.set()

def get_pending_event(device_id):
    """获取(或创建)指定设备的等待事件"""
    with _pending_cmd_events_lock:
        if device_id not in _pending_cmd_events:
            _pending_cmd_events[device_id] = _th.Event()
        return _pending_cmd_events[device_id]

def clear_pending_event(device_id):
    """清除事件状态(在开始等待前调用)"""
    with _pending_cmd_events_lock:
        evt = _pending_cmd_events.get(device_id)
        if evt:
            evt.clear()     # 离线开锁指令队列 {device_id: [commands]}

# ============================================
# ????
# ============================================

def _get_device_protocol(device_id):
    """从cabinets表mainboard_source读取设备协议类型，默认YBM"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT mainboard_source FROM cabinets WHERE mainboard_device_id=%s', (str(device_id),))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logger.error(f'[协议查询] 失败: {e}')
    return 'YBM'



def _format_datetimes(obj):
    """Recursively convert datetime objects to YYYY-MM-DD HH:MM:SS strings"""
    if isinstance(obj, dict):
        return {k: _format_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_format_datetimes(item) for item in obj]
    elif hasattr(obj, 'strftime'):
        # datetime -> 'YYYY-MM-DD HH:MM:SS'; date(无hour) -> 'YYYY-MM-DD'，避免Flask序列化成HTTP日期
        if hasattr(obj, 'hour'):
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        return obj.strftime('%Y-%m-%d')
    return obj


def json_response(data=None, message='success', code=200, headers=None):
    """统一JSON响应格式"""
    resp = jsonify({'code': code, 'message': message, 'data': _format_datetimes(data)})
    resp.status_code = code
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    return resp


# ============================================
# ????
# ============================================
def get_setting(key, default=None):
    """获取系统设置"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT setting_value FROM system_settings WHERE setting_key = %s', (key,))
    result = cursor.fetchone()
    conn.close()
    return result['setting_value'] if result else default


def set_setting(key, value):
    """设置系统配置"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)', (key, str(value)))
    conn.commit()
    conn.close()


# ============================================
# ????
# ============================================
def is_mock_mode():
    """检查是否为模拟支付模式"""
    return get_setting('pay_mode', 'mock') == 'mock'


# ============================================
# ?????
# ============================================
def is_wechat_browser():
    """检查是否在微信浏览器中"""
    from flask import request
    user_agent = request.headers.get('User-Agent', '')
    return 'MicroMessenger' in user_agent


def is_mobile_browser():
    """检查是否在移动端浏览器中"""
    from flask import request
    user_agent = request.headers.get('User-Agent', '')
    mobile_keywords = ['Mobile', 'Android', 'iPhone', 'iPad', 'iPod', 'Windows Phone']
    return any(keyword in user_agent for keyword in mobile_keywords)


# ============================================
# ???????
# ============================================
def manage_user_tokens(cursor, user_type, user_id, token, max_tokens):
    """Insert token and enforce concurrent login limit"""
    cursor.execute('INSERT INTO user_tokens (user_type, user_id, token) VALUES (%s, %s, %s)', (user_type, user_id, token))
    cursor.execute('SELECT COUNT(*) as cnt FROM user_tokens WHERE user_type=%s AND user_id=%s', (user_type, user_id))
    count = cursor.fetchone()['cnt']
    if count > max_tokens:
        cursor.execute('DELETE FROM user_tokens WHERE id IN (SELECT id FROM user_tokens WHERE user_type=%s AND user_id=%s ORDER BY created_at ASC LIMIT %s)', (user_type, user_id, count - max_tokens))
    return token


def require_auth(f):
    """管理员权限验证 - 同时支持session cookie和Bearer token"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. Check Flask session first
        if 'admin_id' in session:
            return f(*args, **kwargs)
        # 2. Fall back to Bearer token
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            if token:
                try:
                    from database import get_db
                    db = get_db()
                    cursor = db.cursor()
                    cursor.execute('SELECT id, username, role FROM admin_users WHERE auth_token=%s', (token,))
                    user = cursor.fetchone()
                    db.close()
                    if user:
                        session['admin_id'] = user['id']
                        session['admin_username'] = user['username']
                        session['admin_role'] = user['role']
                        return f(*args, **kwargs)
                except Exception as e:
                    logger.error(f'Token auth failed: {e}')
        return json_response(message='未登录，请先登录', code=401)
    return decorated


def require_merchant_auth(f):
    """商家/代理商权限验证 - 同时支持session cookie和Bearer token"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. Check Bearer token first (overrides stale session cookies)
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            if token:
                try:
                    db = get_db()
                    cursor = db.cursor()
                    # Check user_tokens table first (supports concurrent logins)
                    try:
                        tok_row = cursor.execute('SELECT user_type, user_id FROM user_tokens WHERE token=%s', (token,)).fetchone()
                        if tok_row:
                            utype = tok_row['user_type']
                            uid = tok_row['user_id']
                            if utype == 'agent':
                                ag = cursor.execute('SELECT id, name, permissions FROM agents WHERE id=%s', (uid,)).fetchone()
                                if ag:
                                    session['agent_id'] = ag['id']; session['agent_name'] = ag['name']; session['is_agent'] = True
                                    session['permissions'] = json.loads(ag['permissions'] or '[]')
                                    db.close(); return f(*args, **kwargs)
                            elif utype == 'employee':
                                emp = cursor.execute('SELECT e.id, e.merchant_id, e.agent_id, e.name, e.permissions, m.name as merchant_name, a.name as agent_name FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id LEFT JOIN agents a ON e.agent_id=a.id WHERE e.id=%s', (uid,)).fetchone()
                                if emp:
                                    if emp['agent_id']:
                                        session['agent_id'] = emp['agent_id']; session['agent_name'] = emp['agent_name'] or emp['name']; session['is_agent'] = True
                                    else:
                                        session['merchant_id'] = emp['merchant_id']; session['merchant_name'] = emp['merchant_name'] or emp['name']
                                    session['employee_id'] = emp['id']; session['is_employee'] = True
                                    session['permissions'] = json.loads(emp['permissions'] or '[]')
                                    db.close(); return f(*args, **kwargs)
                            else:
                                mch = cursor.execute('SELECT id, name FROM merchants WHERE id=%s', (uid,)).fetchone()
                                if mch:
                                    session['merchant_id'] = mch['id']; session['merchant_name'] = mch['name']; session['is_agent'] = False
                                    db.close(); return f(*args, **kwargs)
                    except Exception as _ute:
                        logger.error(f'[user_tokens_auth] {_ute}')
                    # Check merchant table
                    row = cursor.execute('SELECT id, name, agent_id FROM merchants WHERE auth_token=%s', (token,)).fetchone()
                    if row:
                        session['merchant_id'] = row['id']
                        session['merchant_name'] = row['name']
                        session['is_agent'] = False
                        db.close()
                        return f(*args, **kwargs)
                    # Check agent table
                    row = cursor.execute('SELECT id, name, permissions FROM agents WHERE auth_token=%s', (token,)).fetchone()
                    if row:
                        session['agent_id'] = row['id']
                        session['agent_name'] = row['name']
                        session['is_agent'] = True
                        session['permissions'] = json.loads(row['permissions'] or '[]')
                        db.close()
                        return f(*args, **kwargs)
                    # Check employee table (before db.close())
                    try:
                        row = cursor.execute("SELECT e.id, e.merchant_id, e.agent_id, e.name, e.permissions, m.name as merchant_name, a.name as agent_name FROM employees e LEFT JOIN merchants m ON e.merchant_id = m.id LEFT JOIN agents a ON e.agent_id = a.id WHERE e.auth_token=%s", (token,)).fetchone()
                        if row:
                            if row['agent_id']:
                                session['agent_id'] = row['agent_id']; session['agent_name'] = row['agent_name'] or row['name']; session['is_agent'] = True
                            else:
                                session['merchant_id'] = row['merchant_id']
                                session['merchant_name'] = row['merchant_name'] or row['name']
                            session['employee_id'] = row['id']
                            session['is_employee'] = True
                            session['permissions'] = json.loads(row['permissions'] or '[]')
                            db.close()
                            return f(*args, **kwargs)
                    except Exception as e:
                        logger.error(f'[emp_auth] {e}')
                    db.close()
                except Exception as e:
                    logger.error(f'Auth failed: {e}')
        # 2. Fall back to session cookie
        if 'merchant_id' in session or 'agent_id' in session:
            return f(*args, **kwargs)
        return json_response(message='未登录，请先登录', code=401)
    return decorated


def require_agent_auth(f):
    """代理商权限验证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'agent_id' not in session:
            return json_response(message='未登录，请先登录', code=401)
        return f(*args, **kwargs)
    return decorated


def require_employee_auth(f):
    """员工权限验证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'employee_id' not in session:
            return json_response(message='未登录，请先登录', code=401)
        return f(*args, **kwargs)
    return decorated


# ============================================
# ??????
# ============================================
def should_hide_order(merchant_id, order_id, phone, hide_rate, whitelist, logic_mark=None, total_orders=0):
    """判断订单是否应对商家隐藏（确定性哈希）
    logic_mark: 'N'=手动恢复(不隐藏), 'Y'=手动隐藏, None=按hash计算
    """
    if logic_mark == 'N':
        return False
    if logic_mark == 'Y':
        return True
    if whitelist and phone in whitelist:
        return False
    if not hide_rate or hide_rate <= 0:
        return False
    if total_orders > 0 and order_id <= 40:
        return False
    hash_val = int(hashlib.md5(f"{merchant_id}_{order_id}_{ORDER_HIDE_SECRET}".encode()).hexdigest()[:8], 16)
    return (hash_val % 100) < hide_rate


def apply_order_auto_hide(cursor, order_id, cabinet_id, user_phone=None):
    """新订单创建后调用：按网点当前配置决定是否自动隐藏（固化 auto_hidden）。"""
    cursor.execute("""
        SELECT l.id AS location_id, l.merchant_id,
               COALESCE(l.hide_ratio, 0) AS hide_ratio,
               COALESCE(l.hide_start_orders, 0) AS hide_start_orders,
               l.whitelist_phones
        FROM cabinets cb
        JOIN locations l ON cb.location_id = l.id
        WHERE cb.id = %s
    """, (cabinet_id,))
    loc = cursor.fetchone()
    if not loc or loc['merchant_id'] is None or loc['hide_ratio'] <= 0:
        return
    if user_phone and loc['whitelist_phones']:
        whitelist = [x.strip() for x in (loc['whitelist_phones'] or '').split(',') if x.strip()]
        if user_phone in whitelist:
            return
    if loc['hide_start_orders'] > 0:
        # 2026-08-18 需求：当天前 N 单不隐藏，第 N+1 单起按比例隐藏（原来按网点累计，偃师等累计远超 N 导致每天第1单就开始隐藏）
        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM orders o
            JOIN cabinets cb ON o.cabinet_id = cb.id
            WHERE cb.location_id = %s AND o.created_at >= CURRENT_DATE
        """, (loc['location_id'],))
        if cursor.fetchone()['cnt'] <= loc['hide_start_orders']:
            return
    cursor.execute("""
        UPDATE orders SET auto_hidden = 1
        WHERE id = %s AND should_hide_by_hash(%s, %s, %s)
    """, (order_id, loc['merchant_id'], order_id, loc['hide_ratio']))


def filter_duplicate_users(orders, days, limit):
    """过滤高频用户的订单"""
    if not days or not limit or limit <= 0:
        return orders
    cutoff = datetime.now() - timedelta(days=days)
    user_counts = {}
    for o in orders:
        phone = o.get('user_phone') or o.get('phone')
        store_time = o.get('store_time') or o.get('created_at')
        if phone and store_time:
            try:
                if isinstance(store_time, str):
                    store_time = datetime.strptime(store_time[:19], '%Y-%m-%d %H:%M:%S')
                if store_time >= cutoff:
                    user_counts[phone] = user_counts.get(phone, 0) + 1
            except Exception:
                pass
    heavy_users = {phone for phone, count in user_counts.items() if count > limit}
    return [o for o in orders if (o.get('user_phone') or o.get('phone')) not in heavy_users]


# ============================================
# WebSocket 开锁指令
# ============================================
def supersede_force_update_cmds(cursor, device_id):
    """作废该设备旧的 force_update 待执行指令，避免挡住新版本推送"""
    cursor.execute(
        "UPDATE pending_lock_cmds SET delivered=1, status='cancelled' "
        "WHERE device_id=%s AND (delivered=0 OR status='pending') AND strpos(command,'force_update')>0",
        (device_id,)
    )


def send_open_lock(device_id, board_no, lock_no, protocol=None, order_id='', slot_number=None, slot_label=None, skip_dedup=False, require_online=False, manual=False):
    """
    发送开锁指令 - 支持原始WebSocket + Socket.IO + HTTP轮询兜底
    """
    if require_online:
        _hb = None
        _c = None
        try:
            from database import get_db as _gdb
            _c = _gdb()
            _cur = _c.cursor()
            _cur.execute("SELECT last_heartbeat FROM cabinets WHERE mainboard_device_id=%s", (device_id,))
            _r = _cur.fetchone()
            if _r:
                _hb = _r['last_heartbeat']
        except Exception:
            pass
        finally:
            if _c is not None:
                try:
                    _c.close()
                except Exception:
                    pass
        try:
            if not is_device_online(device_id, _hb):
                logger.info(f'[SEND_LOCK] 设备离线，拒绝发送: device_id={device_id}')
                return False
        except Exception as _oe:
            logger.warning(f'[SEND_LOCK] 在线校验失败(继续发送): {_oe}')
    # 防重1（快速路径）：同一 order_id 60秒内，内存级防重（仅同worker有效）
    _now = time.time()
    if not skip_dedup and order_id and order_id in _last_open_lock_time:
        if _now - _last_open_lock_time[order_id] < 60:
            logger.info(f'[SEND_LOCK] 内存防重跳过: order_id={order_id}, {_now - _last_open_lock_time[order_id]:.1f}s ago')
            return True
    # 防重2（跨worker）：数据库级检查同一 order_id 60秒内是否已创建命令
    if not skip_dedup and order_id:
        try:
            import psycopg2 as _psycopg2
            from config import DATABASE_URL as _SL_DB
            _chk_conn = _psycopg2.connect(_SL_DB, connect_timeout=3)
            _chk_cur = _chk_conn.cursor()
            _chk_cur.execute("SELECT COUNT(*) FROM pending_lock_cmds WHERE order_id = %s AND created_at > NOW() - interval '60 seconds'", (order_id,))
            _dup_count = _chk_cur.fetchone()[0]
            _chk_cur.close()
            _chk_conn.close()
            if _dup_count > 0:
                logger.info(f'[SEND_LOCK] DB防重跳过: order_id={order_id}, found {_dup_count} recent cmds')
                return True
        except Exception as _chk_e:
            logger.warning(f'[SEND_LOCK] DB防重检查失败(继续执行): {_chk_e}')
        _last_open_lock_time[order_id] = _now
    # 自动从数据库解析协议类型
    if protocol is None:
        protocol = _get_device_protocol(device_id)
    logger.info(f'[SEND_LOCK] device={device_id}, protocol={protocol}, id(pending)={id(pending_lock_commands)}, keys_before={list(pending_lock_commands.keys())}')
    _cmd_order_id = ('manual_' + str(order_id)) if manual and order_id else order_id
    command = {
        'type': 'open_lock',
        'device_id': device_id,
        'deviceId': device_id,
        'board_no': board_no,
        'boardNo': board_no,
        'lock_no': lock_no,
        'lockNo': lock_no,
        'protocol': protocol,
        'order_id': _cmd_order_id,
        'orderId': str(_cmd_order_id) if _cmd_order_id else '',
        'slot_number': slot_number or 0,
        'slot_label': slot_label or '',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'cmd_id': f"cmd_{int(time.time()*1000000)}",
        'cmd_id': f"cmd_{int(time.time()*1000000)}",
    }
    # 先发WebSocket（不依赖DB，即使DB锁住也能秒开）
    _ws_sent = False
    if device_id in connected_devices:
        ws = connected_devices[device_id]
        try:
            import gevent
            with gevent.Timeout(3):
                ws.send(json.dumps(command))
            _ws_sent = True
            logger.info(f"[WS-DIRECT] open_lock sent immediately: device={device_id}, board={board_no}, lock={lock_no}")
            if device_id in pending_lock_commands:
                pending_lock_commands[device_id] = [cmd for cmd in pending_lock_commands[device_id] if cmd.get("lock_no") != lock_no or cmd.get("board_no") != board_no]
        except Exception as e:
            logger.error(f"[WS-DIRECT] send failed, queue fallback: {e}")
            if device_id not in pending_lock_commands:
                pending_lock_commands[device_id] = []
            pending_lock_commands[device_id].append(command)
    # 尝试独立WebSocket服务(设备连接独立WS时使用)
    if not _ws_sent:
        import urllib.request as _req, json as _json
        for _retry in range(3):
            try:
                _body = _json.dumps({"device_id": device_id, "command": command}).encode()
                _r = _req.urlopen("http://127.0.0.1:5004/send", data=_body, timeout=2)
                if _json.loads(_r.read()).get("success"):
                    _ws_sent = True
                    logger.info(f"[WS-DAEMON] open_lock sent via daemon (retry={_retry}): device={device_id}, board={board_no}, lock={lock_no}")
                    break
            except Exception:
                pass
            if _retry < 2:
                time.sleep(1)

    
    # 内存队列兜底（仅在WS发送失败时使用）
    if not _ws_sent:
        if device_id not in pending_lock_commands:
            pending_lock_commands[device_id] = []
        if command not in pending_lock_commands[device_id]:
            pending_lock_commands[device_id].append(command)
    
    # 已通过WS/daemon发送成功的指令, 标记delivered=1, 避免HTTP轮询重复下发
    _delivered = 1 if _ws_sent else 0
    _sl_conn = None
    try:
        import psycopg2
        from config import DATABASE_URL as _SL_DB
        _sl_conn = psycopg2.connect(_SL_DB, connect_timeout=5)
        _sl_cur = _sl_conn.cursor()
        _sl_cur.execute("INSERT INTO pending_lock_cmds (device_id, board_no, lock_no, protocol, order_id, command, delivered) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (device_id, board_no, lock_no, protocol, order_id, json.dumps(command), _delivered))
        _sl_cur.close()
        _sl_conn.commit()
        _sl_conn.close()
        # 无论WS是否发送成功，都通知设备来轮询（WS可能丢包）
        signal_pending_command(device_id)
    except Exception as _e:
        logger.error(f"[DB] 存储pending_lock失败: {_e}")
    finally:
        if _sl_conn:
            try: _sl_conn.close()
            except: pass
            _sl_conn = None
    try:
        import psycopg2
        from config import DATABASE_URL as _SL_DB2
        _sl_conn2 = psycopg2.connect(_SL_DB2, connect_timeout=5)
        _sl_cur2 = _sl_conn2.cursor()
        _sl_cur2.execute("INSERT INTO door_records (device_id, board_no, lock_no, order_id, open_type) VALUES (%s,%s,%s,%s,%s)",
                     (device_id, board_no, lock_no, str(order_id) if order_id else "", protocol or "remote"))
        _sl_cur2.close()
        _sl_conn2.commit()
        _sl_conn2.close()
    except Exception as _e3:
        logger.error(f"[DB] 存储door_record失败: {_e3}")
    finally:
        if _sl_conn2:
            try: _sl_conn2.close()
            except: pass
            _sl_conn2 = None
    return True


def send_open_all(device_id, protocol=None):
    if protocol is None:
        protocol = _get_device_protocol(device_id)
    """Send open-all command via WebSocket"""
    command = {
        'type': 'open_lock',
        'openAll': True,
        'device_id': device_id,
        'protocol': protocol,
        'order_id': '',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    if device_id not in pending_lock_commands:
        pending_lock_commands[device_id] = []
    pending_lock_commands[device_id].append(command)
    # 只推送一次（同步），避免设备收到重复全开指令
    try:
        import urllib.request as _req
        import json as _json
        _body = _json.dumps({'device_id': device_id, 'command': command}).encode()
        _req.urlopen('http://127.0.0.1:5004/send', data=_body, timeout=3)
        logger.info("[WS-DAEMON] open_all sent via daemon: " + str(device_id))
    except Exception as e:
        logger.error(f'[send_open_all] {e}')

    if device_id in connected_devices:
        ws = connected_devices[device_id]
        if hasattr(ws, 'send') and not getattr(ws, 'closed', True):
            try:
                ws.send(json.dumps(command))
                logger.info("[RawWS] open_all: " + str(device_id))
                return True
            except Exception as e:
                logger.error("[RawWS] open_all failed: " + str(e))
        elif isinstance(ws, str):
            try:
                from flask import current_app
                socketio = current_app.extensions.get('socketio')
                if socketio:
                    socketio.emit('open_lock', command, room=ws, namespace='/')
                    return True
            except:
                pass
    logger.info("[Queue] open_all queued: " + str(device_id))
    return True


def send_open_lock_list(device_id, doors, protocol=None, order_id='', require_online=False):
    """方案C: 列表开门 - 单命令携带门列表, 设备按序逐门开锁, 避免逐条推送乱序/丢失
    doors: [(board_no, lock_no), ...]
    """
    if not doors:
        return False
    if protocol is None:
        try:
            protocol = _get_device_protocol(device_id)
        except Exception:
            protocol = 'YBM'
    if require_online:
        try:
            from database import get_db as _gdb
            _c = _gdb(); _cur = _c.cursor()
            _cur.execute("SELECT last_heartbeat FROM cabinets WHERE mainboard_device_id=%s", (device_id,))
            _r = _cur.fetchone(); _c.close()
            if _r and not is_device_online(device_id, _r['last_heartbeat']):
                logger.info(f'[SEND_LOCK_LIST] 设备离线: device_id={device_id}')
                return False
        except Exception:
            pass
    import urllib.request as _req
    import json as _json
    cmd_id = f"list_{int(time.time()*1000000)}"
    command = {
        'type': 'open_lock_list',
        'device_id': device_id,
        'cmd_id': cmd_id,
        'protocol': protocol,
        'order_id': order_id or '',
        'doors': [{'board_no': int(b), 'lock_no': int(l)} for b, l in doors],
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    logger.info(f'[SEND_LOCK_LIST] device={device_id}, doors={len(doors)}, protocol={protocol}, cmd_id={cmd_id}')
    logger.info(f'[SEND_LOCK_LIST] doors前12={doors[:12]}')
    # 1) ws_proxy 同步推送(首选)
    _ws_sent = False
    for _retry in range(3):
        try:
            _body = _json.dumps({'device_id': device_id, 'command': command}).encode()
            _r = _req.urlopen('http://127.0.0.1:5004/send', data=_body, timeout=3)
            if _json.loads(_r.read()).get('success'):
                _ws_sent = True
                logger.info(f'[SEND_LOCK_LIST] ws_proxy sent (retry={_retry}): device={device_id}')
                break
        except Exception:
            pass
        if _retry < 2:
            time.sleep(0.5)
    # 2) DB pending 兜底(设备轮询拾取)
    if not _ws_sent:
        try:
            from database import get_db as _gdb
            _c = _gdb(); _cur = _c.cursor()
            _cur.execute('SELECT id FROM cabinets WHERE mainboard_device_id=%s', (device_id,))
            _cab = _cur.fetchone()
            if _cab:
                _cur.execute("INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, status, delivered) VALUES (%s,%s,%s,'pending',0)",
                             (device_id, _cab['id'], _json.dumps(command)))
                _c.commit()
                logger.info(f'[SEND_LOCK_LIST] pending queued: device={device_id}')
            _c.close()
            _ws_sent = True
        except Exception as e:
            logger.error(f'[SEND_LOCK_LIST] pending fallback failed: {e}')
    return _ws_sent


# ============================================
# 支付相关 - 延迟导入避免循环
# ============================================
def _get_payment_channel(channel_id=None, exclude_channel_id=None):
    """获取支付渠道（支持严格轮转和加权随机）"""
    conn = get_db()
    cursor = conn.cursor()
    if channel_id:
        cursor.execute('SELECT * FROM payment_channels WHERE id = %s', (channel_id,))
        ch = cursor.fetchone()
        conn.close()
        return dict(ch) if ch else None
    cursor.execute('SELECT * FROM payment_channels WHERE is_active = 1')
    channels = cursor.fetchall()
    if not channels:
        conn.close()
        return None
    # 如果有排除的渠道，过滤掉
    if exclude_channel_id:
        channels = [ch for ch in channels if ch['id'] != exclude_channel_id]
        if not channels:
            conn.close()
            return None
    # 读取轮转模式
    rotation_mode = 'round_robin'
    try:
        cursor.execute('SELECT setting_value FROM system_settings WHERE setting_key = %s', ('channel_rotation_mode',))
        row = cursor.fetchone()
        if row and row[0]:
            rotation_mode = row[0]
    except Exception:
        pass

    # ====== Sequential mode: one at a time, failover on block ======
    if rotation_mode == 'sequential':
        if exclude_channel_id:
            cursor.execute('SELECT * FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) AND id != %s ORDER BY rotation_index ASC LIMIT 1', (exclude_channel_id,))
        else:
            cursor.execute('SELECT * FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) ORDER BY rotation_index ASC LIMIT 1')
        ch = cursor.fetchone()
        conn.close()
        if ch:
            selected = dict(ch)
            logger.info('[channel-sequential] current: %s (id=%d)' % (selected.get('name',''), selected['id']))
            return selected
        logger.error('[channel-sequential] no channel!')
        return None
    if rotation_mode == 'round_robin':
        # 真轮询：选last_used_at最早的，保证每个商户依次使用
        from datetime import datetime as _dt; selected = min(channels, key=lambda ch: ch['last_used_at'] or _dt(1970,1,1))
        logger.info(f"[渠道轮转-轮转模式] 选中: {selected['name']} (id={selected['id']}, last_used={selected['last_used_at']})")
    else:
        # 加权随机
        weights = []
        for ch in channels:
            base_weight = ch['weight'] or 1
            inverse_factor = 1.0 / (1 + (ch['total_amount'] or 0) / 1000)
            weights.append(base_weight * inverse_factor)
        selected = random.choices(list(channels), weights=weights, k=1)[0]
        logger.info(f"[渠道轮转-随机模式] 选中: {selected['name']} (id={selected['id']})")
    conn.close()
    return dict(selected)


def select_payment_channel(exclude_channel_id=None):
    """选择支付渠道（加权随机轮换）
    exclude_channel_id: 排除的渠道ID，用于故障切换时跳过当前失败的渠道
    """
    return _get_payment_channel(exclude_channel_id=exclude_channel_id)


def update_channel_stats(channel_id, amount):
    """更新渠道统计"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE payment_channels SET total_amount = total_amount + %s, total_count = total_count + 1, last_used_at = %s WHERE id = %s',
                       (amount, datetime.now(), channel_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[渠道统计] 更新失败: {e}")


def get_channel_wxpay(channel, use_mp_appid=False):
    """根据渠道配置创建支付实例"""
    from wxpay import WxPay, ThirdPartyPay as TPP
    channel_type = channel.get('channel_type', 'wechat')
    if channel_type == 'wechat':
        app_id = channel.get('app_id') or (WX_MP_APP_ID if use_mp_appid else WX_APP_ID)
        cert_name = channel.get('cert_name', '')
        if cert_name:
            cert_path = f'/home/ubuntu/smart-locker/cert/{cert_name}_cert.pem'
            key_path = f'/home/ubuntu/smart-locker/cert/{cert_name}_key.pem'
        else:
            cert_path = WX_CERT_PATH
            key_path = WX_KEY_PATH
        return WxPay(mch_id=channel['mch_id'], api_key=channel['api_key'],
                      app_id=app_id, cert_path=cert_path, key_path=key_path), 'wechat'
    elif channel_type == 'third_party':
        extra = json.loads(channel.get('extra_config', '{}')) if channel.get('extra_config') else {}
        return TPP(appid=channel['mch_id'], appsecret=channel['api_key'],
                    notify_url=WX_PAY_NOTIFY_URL.replace('/api/pay/notify', '/api/pay/notify/third-party'),
                    return_url=extra.get('return_url', '')), 'third_party'
    return None, None


def get_wxpay(use_mp_appid=False):
    """获取默认微信支付实例"""
    from wxpay import WxPay, MockWxPay
    mode = get_setting('pay_mode', 'mock')
    if mode == 'mock':
        return MockWxPay()
    app_id = WX_MP_APP_ID if use_mp_appid else WX_APP_ID
    return WxPay(mch_id=WX_MCH_ID, api_key=WX_API_KEY, app_id=app_id,
                 cert_path=WX_CERT_PATH, key_path=WX_KEY_PATH)


def get_payment_params(order_id, order_no, deposit_amount, user_phone=None, openid=None,
                       payment_channel=None, payment_channel_id=None, _retry_count=0):
    """获取微信支付参数"""
    from wxpay import WxPay
    mock_mode = is_mock_mode()

    if mock_mode:
        return {'mode': 'mock', 'order_id': order_id, 'order_no': order_no, 'total_fee': int(deposit_amount * 100)}

    if openid or user_phone:
        try:
            assign_merchant(phone=user_phone, openid=openid)
        except Exception:
            pass

    trade_type = 'MWEB'
    scene_info = None
    if is_mobile_browser():
        if is_wechat_browser():
            trade_type = 'JSAPI' if openid else 'MWEB'
            if trade_type == 'MWEB':
                scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '智能寄存柜'})
        else:
            scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '智能寄存柜'})
    else:
        scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '智能寄存柜'})

    if openid:
        trade_type = 'JSAPI'
    # 使用支付渠道
    if payment_channel_id:
        ch = _get_payment_channel(payment_channel_id)
        current_channel = ch or payment_channel
    elif payment_channel:
        current_channel = payment_channel
    else:
        current_channel = _get_payment_channel()  # 自动选活跃渠道，避免fallback到硬编码默认商户

    if current_channel:
        wxpay, ch_type = get_channel_wxpay(current_channel, use_mp_appid=False)
        if ch_type == 'third_party' and wxpay:
            third_party_type = 'alipay' if not is_wechat_browser() else 'wechat'
            result = wxpay.unifiedorder(trade_type=third_party_type, body='若预付款未退回，可进入下方公众号提现或拨打客服电话400-698-1080',
                                         total_fee=int(deposit_amount * 100), out_trade_no=order_no)
            if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
                # 更新渠道统计（用于轮转）
                if current_channel:
                    update_channel_stats(current_channel['id'], deposit_amount)
                return {'mode': 'third_party', 'channel_type': third_party_type, 'order_id': order_id,
                        'order_no': order_no, 'pay_url': result.get('url', ''), 'url_qrcode': result.get('url_qrcode', '')}
            return {'mode': 'error', 'error_msg': result.get('return_msg', '第三方下单失败')}
        if wxpay is None:
            return {'mode': 'error', 'error_msg': '支付渠道配置异常'}
    else:
        return {'mode': 'error', 'error_msg': '无可用活跃商户，请联系管理员'}

    total_fee = int(deposit_amount * 100)
    time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')

    result = wxpay.unifiedorder(trade_type=trade_type, body='若预付款未退回，可进入下方公众号提现或拨打客服电话400-698-1080',
                                 total_fee=total_fee, out_trade_no=order_no,
                                 notify_url=WX_PAY_NOTIFY_URL, openid=openid,
                                 scene_info=scene_info, time_expire=time_expire)

    if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
        # 更新订单的实际支付渠道（防止轮转导致不一致）
        try:
            from database import get_db as _gdb3
            _db3 = _gdb3()
            _db3.execute("UPDATE orders SET payment_channel_id=%s WHERE id=%s", (current_channel["id"], order_id))
            _db3.commit()
            _db3.close()
        except Exception as _e:
            logger.error(f"[支付渠道更新] 失败: {_e}")
        # 更新渠道统计
        if current_channel:
            update_channel_stats(current_channel['id'], deposit_amount)
        prepay_id = result.get('prepay_id')
        if trade_type == 'JSAPI':
            jsapi_params = wxpay.get_jsapi_params(prepay_id)
            result = {'mode': 'jsapi', 'order_id': order_id, 'order_no': order_no,
                    'prepay_id': prepay_id}
            result.update(jsapi_params)
            return result
        else:
            return {'mode': 'h5', 'order_id': order_id, 'order_no': order_no,
                    'mweb_url': result.get('mweb_url')}
    
    # 商户被封/异常自动检测
    _dead_errors = {'MCH_NOT_EXIST', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}
    _skip_errors = {'NOAUTH', 'NO_AUTH'}  # 收款受限，切换重试但不永久禁用
    _err_code = result.get('err_code', '')
    if current_channel and _retry_count < 3 and (_err_code in _dead_errors or _err_code in _skip_errors):
        # 只对严重错误禁用商户；NOAUTH等收款受限只切换不禁用
        if _err_code in _dead_errors:
            try:
                from database import get_db as _gdb2
                _db2 = _gdb2()
                _db2.execute('UPDATE payment_channels SET is_active=0 WHERE id=%s', (current_channel['id'],))
                _db2.commit()
                _db2.close()
                logger.warning(f'[渠道] 商户异常已自动禁用: id={current_channel["id"]}, name={current_channel.get("name","")}, err={result.get("err_code")}')
            except Exception as _e:
                logger.error(f'[渠道] 自动禁用失败: {_e}')
        else:
            logger.warning(f'[渠道] 商户收款受限(不禁用)，切换重试: id={current_channel["id"]}, err={result.get("err_code")}')
        next_ch = select_payment_channel(exclude_channel_id=current_channel['id'])
        if next_ch and next_ch.get('id') and next_ch['id'] != current_channel['id']:
            logger.info(f'[渠道] 切换到下一个渠道重试: {next_ch["name"]}')
            # [已修复] 不再修改订单的payment_channel_id，让用户重新扫码
            # 原因：用户扫码时是商户A，如果系统偷偷换成商户B，支付回调时会找不到订单
            logger.warning(f'[渠道] 商户异常，需要用户重新扫码。不修改订单#{order_id}的payment_channel_id')
            return get_payment_params(order_id, order_no, deposit_amount, user_phone, openid, payment_channel=next_ch, payment_channel_id=next_ch['id'], _retry_count=_retry_count+1)
    
    if current_channel:
        try:
            from database import get_db
            _db = get_db()
            _db.close()
            logger.info(f'[WX-PAY] channel {current_channel["id"]} failed, not counting')
        except Exception as _e:
            logger.error(f'[WX-PAY] update channel stats failed: {_e}')

    logger.error(f'[WX-PAY] unifiedorder failed: {result}')
    return {'mode': 'error', 'error_msg': '交易失败，请重新支付'}


def process_auto_refund(order, cursor, conn):
    """自动退款（防测试场景）- 调用真正的微信退款API"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
    # 检查是否已经退款
    if order.get('refund_status') in ('success','refunded'):
        return json_response({'status': 'already_refunded', 'refund_amount': amount, 'refund_id': None, 'message': '已退款'})
    payment_channel_id = order.get('payment_channel_id')
    
    # 调用真正的退款API
    success, refund_id, refund_msg = do_real_refund(order_id=order_id, order_no=order_no, amount=amount, payment_channel_id=payment_channel_id)
    
    if success:
        cursor.execute("UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s", (refund_id, datetime.now(), order_id))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        cursor.execute("INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)", (order_id, amount, refund_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 2, 'system', %s)", (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_refund', 'refund_amount': amount, 'refund_id': refund_id, 'message': '系统已自动退款', 'show_refunding_status': order.get('show_refunding_status', 1)})
    else:
        cursor.execute("UPDATE orders SET status = 6, refund_id = %s, refund_time = %s WHERE id = %s", ('FAIL:' + refund_msg[:50], datetime.now(), order_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 1, 'system', %s)", (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_refund_failed', 'refund_amount': 0, 'refund_id': None, 'message': '退款失败: ' + refund_msg, 'show_refunding_status': order.get('show_refunding_status', 1)})
def process_auto_approve(order, cursor, conn):
    """自动通过（点击免审）- 调用真正的微信退款API"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
    payment_channel_id = order.get('payment_channel_id')
    
    # 调用真正的退款API
    success, refund_id, refund_msg = do_real_refund(order_id=order_id, order_no=order_no, amount=amount, payment_channel_id=payment_channel_id)
    
    if success:
        cursor.execute('UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s',
                       (refund_id, datetime.now(), order_id))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        cursor.execute('INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)',
                       (order_id, amount, refund_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 2, 'system', %s)",
                       (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_approve', 'refund_amount': amount, 'refund_id': refund_id,
                              'message': '已自动通过，退款将很快到账',
                              'show_refunding_status': order.get('show_refunding_status', 1)})
    else:
        # 退款失败
        cursor.execute("UPDATE orders SET status = 6 WHERE id = %s", (order_id,))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_approve_failed', 'refund_amount': 0, 'refund_id': None,
                              'message': '自动审批失败: ' + refund_msg,
                              'show_refunding_status': order.get('show_refunding_status', 1)})
def generate_sms_code():
    """生成6位短信验证码"""
    return ''.join(random.choices(string.digits, k=6))


def _row_dict(row):
    return dict(row) if row is not None else None


def _clean(value):
    return (value or '').strip()


def phone_openid_rows(cursor, phone='', openid='', mp_openid='', unionid=''):
    """Return matching phone_openids rows. A phone may have several identities."""
    parts = []
    params = []
    if phone:
        parts.append('phone = %s')
        params.append(phone)
    if unionid:
        parts.append('unionid = %s')
        params.append(unionid)
    if mp_openid:
        parts.append('mp_openid = %s')
        params.append(mp_openid)
    if openid:
        parts.append('openid = %s')
        params.append(openid)
    if not parts:
        return []
    cursor.execute('SELECT * FROM users WHERE ' + ' OR '.join(parts) + ' ORDER BY id', params)
    return [dict(r) for r in cursor.fetchall()]


def resolve_user_identity(cursor, openid='', mp_openid='', phone='', unionid='', user_id=0):
    """Resolve one WeChat identity instead of blindly trusting phone_openids.user_id.

    Returns a dict with user_id/unionid/mp_openid/phone/ambiguous. When ambiguous,
    user_id is 0 so callers must not guess another account.
    """
    out = {
        'user_id': 0,
        'unionid': unionid or '',
        'mp_openid': mp_openid or '',
        'phone': phone or '',
        'ambiguous': False,
        'reason': '',
    }
    uid = int(user_id or 0)
    if uid:
        try:
            cursor.execute("SELECT id, unionid, phone, openid, mp_openid FROM users WHERE id = %s", (uid,))
            row = cursor.fetchone()
            if row:
                out['user_id'] = uid
                out['unionid'] = row['unionid'] or unionid or ''
                out['mp_openid'] = row['mp_openid'] or mp_openid or ''
                out['phone'] = row['phone'] or phone or ''
                return out
        except Exception:
            pass

    strong_keys = []
    for key, value in (('unionid', unionid), ('mp_openid', mp_openid), ('openid', openid)):
        if _clean(value):
            strong_keys.append((key, value))

    app_candidates = []
    for key, value in strong_keys:
        try:
            cursor.execute(
                "SELECT id, unionid, phone, openid, mp_openid FROM users WHERE " + key + " = %s AND id > 0 ORDER BY id",
                (value,),
            )
            for row in cursor.fetchall():
                app_candidates.append(dict(row))
        except Exception:
            pass

    if app_candidates:
        unions = {r['unionid'] for r in app_candidates if r['unionid']}
        if len(unions) > 1:
            out['ambiguous'] = True
            out['reason'] = 'multiple_users'
            return out
        union_rows = [r for r in app_candidates if r['unionid']]
        row = min(union_rows, key=lambda r: r['id']) if union_rows else min(app_candidates, key=lambda r: r['id'])
        out['user_id'] = row['id']
        out['unionid'] = row['unionid'] or unionid or ''
        out['mp_openid'] = row['mp_openid'] or mp_openid or ''
        out['phone'] = row['phone'] or phone or ''
        return out

    if not strong_keys and phone:
        try:
            cursor.execute(
                "SELECT id, unionid, phone, openid, mp_openid FROM users WHERE phone = %s AND id > 0 ORDER BY id",
                (phone,),
            )
            phone_apps = [dict(r) for r in cursor.fetchall()]
            if len(phone_apps) > 1:
                out['ambiguous'] = True
                out['reason'] = 'multiple_users_by_phone'
                return out
            if phone_apps:
                row = phone_apps[0]
                out['user_id'] = row['id']
                out['unionid'] = row['unionid'] or unionid or ''
                out['mp_openid'] = row['mp_openid'] or mp_openid or ''
                out['phone'] = row['phone'] or phone or ''
                return out
        except Exception:
            pass
        try:
            cursor.execute("""
                SELECT count(DISTINCT x) FROM (
                  SELECT NULLIF(unionid,'') AS x FROM users WHERE phone = %s AND NULLIF(unionid,'') IS NOT NULL
                  UNION ALL
                  SELECT NULLIF(unionid,'') FROM users WHERE phone = %s AND NULLIF(unionid,'') IS NOT NULL
                  UNION ALL
                  SELECT NULLIF(unionid,'') FROM orders WHERE user_phone = %s AND NULLIF(unionid,'') IS NOT NULL
                  UNION ALL
                  SELECT NULLIF(unionid,'') FROM user_balances WHERE phone = %s AND NULLIF(unionid,'') IS NOT NULL
                ) t
            """, (phone, phone, phone, phone))
            distinct_unions = cursor.fetchone()[0]
            if distinct_unions and int(distinct_unions) > 1:
                out['ambiguous'] = True
                out['reason'] = 'multiple_phone_identities'
                return out
        except Exception:
            pass
        try:
            cursor.execute("""
                SELECT count(DISTINCT x) FROM (
                  SELECT NULLIF(id,0) AS x FROM users WHERE phone = %s AND id > 0
                  UNION ALL
                  SELECT id FROM users WHERE phone = %s AND id > 0
                  UNION ALL
                  SELECT user_id FROM orders WHERE user_phone = %s AND id > 0
                  UNION ALL
                  SELECT user_id FROM user_balances WHERE phone = %s AND id > 0
                ) t
            """, (phone, phone, phone, phone))
            distinct_uids = cursor.fetchone()[0]
            if distinct_uids and int(distinct_uids) > 1:
                out['ambiguous'] = True
                out['reason'] = 'multiple_phone_user_ids'
                return out
        except Exception:
            pass

    po_candidates = []
    if strong_keys:
        for key, value in strong_keys:
            try:
                cursor.execute(
                    "SELECT * FROM users WHERE " + key + " = %s ORDER BY id",
                    (value,),
                )
                for row in cursor.fetchall():
                    po_candidates.append(dict(row))
            except Exception:
                pass
    if not strong_keys and phone:
        try:
            cursor.execute("SELECT * FROM users WHERE phone = %s ORDER BY id", (phone,))
            po_candidates = [dict(r) for r in cursor.fetchall()]
        except Exception:
            pass

    if po_candidates:
        if phone:
            po_candidates = [r for r in po_candidates if r['phone'] == phone]
        if len(po_candidates) > 1:
            unions = {r['unionid'] for r in po_candidates if r['unionid']}
            if len(unions) <= 1 and len({r['openid'] or r['mp_openid'] for r in po_candidates if r['openid'] or r['mp_openid']}) <= 1:
                po_candidates = po_candidates[:1]
            else:
                out['ambiguous'] = True
                out['reason'] = 'multiple_phone_openids'
                return out
        row = po_candidates[0]
        out['user_id'] = row.get('id') or row.get('user_id') or 0
        out['unionid'] = row.get('unionid') or unionid or ''
        out['mp_openid'] = row.get('mp_openid') or mp_openid or ''
        out['phone'] = row.get('phone') or phone or ''
        return out

    # users 主表没解析到时，用 user_balances 的 unionid 回退（users 仍是最终 user_id 来源）
    if not out['user_id'] and not out['unionid']:
        try:
            _ucond = []
            _uparams = []
            if mp_openid:
                _ucond.append('mp_openid = %s')
                _uparams.append(mp_openid)
            if openid:
                _ucond.append('openid = %s')
                _uparams.append(openid)
            if phone:
                _ucond.append('phone = %s')
                _uparams.append(phone)
            if _ucond:
                cursor.execute(
                    "SELECT DISTINCT unionid FROM user_balances WHERE NULLIF(unionid, '') IS NOT NULL AND ("
                    + ' OR '.join(_ucond) + ")",
                    _uparams,
                )
                _unions = [r['unionid'] for r in cursor.fetchall()]
                if len(_unions) == 1:
                    cursor.execute(
                        "SELECT id, unionid, phone, openid, mp_openid FROM users WHERE unionid = %s AND id > 0 ORDER BY id LIMIT 1",
                        (_unions[0],),
                    )
                    row = cursor.fetchone()
                    if row:
                        out['user_id'] = row['id']
                        out['unionid'] = row['unionid'] or _unions[0]
                        out['mp_openid'] = row['mp_openid'] or out['mp_openid'] or mp_openid
                        out['phone'] = row['phone'] or out['phone'] or phone
                        return out
        except Exception:
            pass

    return out


def find_user_balance_row(cursor, phone='', openid='', mp_openid='', unionid='', user_id=0):
    """Find the balance row belonging to one identity. Returns dict or None."""
    uid = int(user_id or 0)
    if uid:
        try:
            cursor.execute("SELECT * FROM user_balances WHERE user_id = %s ORDER BY id LIMIT 1", (uid,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        except Exception:
            pass
    if unionid:
        if phone:
            try:
                cursor.execute("SELECT * FROM user_balances WHERE phone = %s AND unionid = %s ORDER BY id LIMIT 1", (phone, unionid))
                row = cursor.fetchone()
                if row:
                    return dict(row)
            except Exception:
                pass
        try:
            cursor.execute("SELECT * FROM user_balances WHERE unionid = %s ORDER BY id", (unionid,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1 and phone:
                rows = [r for r in rows if r['phone'] == phone]
                if len(rows) == 1:
                    return rows[0]
        except Exception:
            pass
    if mp_openid:
        try:
            cursor.execute("SELECT * FROM user_balances WHERE mp_openid = %s ORDER BY id", (mp_openid,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1 and phone:
                rows = [r for r in rows if r['phone'] == phone]
                if len(rows) == 1:
                    return rows[0]
        except Exception:
            pass
    if openid:
        try:
            cursor.execute("SELECT * FROM user_balances WHERE openid = %s ORDER BY id", (openid,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1 and phone:
                rows = [r for r in rows if r['phone'] == phone]
                if len(rows) == 1:
                    return rows[0]
        except Exception:
            pass
    if phone:
        try:
            cursor.execute("SELECT * FROM user_balances WHERE phone = %s ORDER BY id", (phone,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                legacy = [r for r in rows if not r.get('unionid')]
                if len(legacy) == 1:
                    return legacy[0]
        except Exception:
            pass
    return None


def upsert_user_balance_row(cursor, phone='', openid='', unionid='', mp_openid='', wechat_name='',
                            balance=0.0, total_deposited=0.0, total_withdrawn=0.0, user_id=0):
    """Add balance to the identity's own user_balances row; never merge phones blindly."""
    phone = _clean(phone)
    openid = _clean(openid)
    unionid = _clean(unionid)
    mp_openid = _clean(mp_openid)
    wechat_name = _clean(wechat_name)
    balance = float(balance or 0)
    total_deposited = float(total_deposited or 0)
    total_withdrawn = float(total_withdrawn or 0)
    user_id = int(user_id or 0)
    existing = find_user_balance_row(cursor, phone=phone, openid=openid, mp_openid=mp_openid, unionid=unionid, user_id=user_id)
    if existing:
        row_id = existing['id']
        cursor.execute(
            """UPDATE user_balances SET
                balance = COALESCE(balance,0) + %s,
                total_deposited = COALESCE(total_deposited,0) + %s,
                total_withdrawn = COALESCE(total_withdrawn,0) + %s,
                wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                user_id = CASE WHEN %s > 0 THEN %s ELSE user_id END
              WHERE id = %s""",
            (balance, total_deposited, total_withdrawn,
             wechat_name, user_id, user_id, row_id),
        )
        return row_id
    if unionid:
        cursor.execute(
            """INSERT INTO user_balances
               (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id, first_use_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (phone, unionid) WHERE unionid IS NOT NULL AND unionid <> ''
               DO UPDATE SET
                 balance = COALESCE(user_balances.balance,0) + EXCLUDED.balance,
                 total_deposited = COALESCE(user_balances.total_deposited,0) + EXCLUDED.total_deposited,
                 total_withdrawn = COALESCE(user_balances.total_withdrawn,0) + EXCLUDED.total_withdrawn,
                 openid = COALESCE(NULLIF(EXCLUDED.openid,''), user_balances.openid),
                 mp_openid = keep_new_mp_openid(user_balances.mp_openid, EXCLUDED.mp_openid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), user_balances.wechat_name)
               RETURNING id""",
            (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id),
        )
    elif mp_openid:
        cursor.execute(
            """INSERT INTO user_balances
               (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id, first_use_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (phone, mp_openid) WHERE mp_openid IS NOT NULL AND mp_openid <> ''
               DO UPDATE SET
                 balance = COALESCE(user_balances.balance,0) + EXCLUDED.balance,
                 total_deposited = COALESCE(user_balances.total_deposited,0) + EXCLUDED.total_deposited,
                 total_withdrawn = COALESCE(user_balances.total_withdrawn,0) + EXCLUDED.total_withdrawn,
                 openid = COALESCE(NULLIF(EXCLUDED.openid,''), user_balances.openid),
                 unionid = COALESCE(NULLIF(EXCLUDED.unionid,''), user_balances.unionid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), user_balances.wechat_name)
               RETURNING id""",
            (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id),
        )
    elif openid:
        cursor.execute(
            """INSERT INTO user_balances
               (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id, first_use_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (openid) WHERE openid IS NOT NULL AND openid <> ''
               DO UPDATE SET
                 balance = COALESCE(user_balances.balance,0) + EXCLUDED.balance,
                 total_deposited = COALESCE(user_balances.total_deposited,0) + EXCLUDED.total_deposited,
                 total_withdrawn = COALESCE(user_balances.total_withdrawn,0) + EXCLUDED.total_withdrawn,
                 mp_openid = keep_new_mp_openid(user_balances.mp_openid, EXCLUDED.mp_openid),
                 unionid = COALESCE(NULLIF(EXCLUDED.unionid,''), user_balances.unionid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), user_balances.wechat_name)
               RETURNING id""",
            (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id),
        )
    else:
        cursor.execute(
            """INSERT INTO user_balances
               (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id, first_use_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (phone) WHERE unionid IS NULL OR unionid = ''
               DO UPDATE SET
                 balance = COALESCE(user_balances.balance,0) + EXCLUDED.balance,
                 total_deposited = COALESCE(user_balances.total_deposited,0) + EXCLUDED.total_deposited,
                 total_withdrawn = COALESCE(user_balances.total_withdrawn,0) + EXCLUDED.total_withdrawn,
                 openid = COALESCE(NULLIF(EXCLUDED.openid,''), user_balances.openid),
                 mp_openid = keep_new_mp_openid(user_balances.mp_openid, EXCLUDED.mp_openid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), user_balances.wechat_name)
               RETURNING id""",
            (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id),
        )
    row = cursor.fetchone()
    return row['id'] if row else None


def upsert_phone_openid_row(cursor, phone='', openid='', mp_openid='', unionid='', wechat_name='', gzh_openid='', user_id=0):
    """Insert or update a phone_openids row keyed by identity, not by phone alone.

    [FIX-20260912] mp_openid 的写入统一走数据库函数 keep_new_mp_openid(旧值, 新值):
    禁止用旧小程序的 openid 覆盖已经存在的【新小程序(ooTcRx) openid】。
      起因: 用户只要偶尔打开一次旧小程序(科莱智), 登录就会把 mp_openid 从 ooTcRx 覆盖成 oWrA8,
      之后所有订阅通知都查不到新 openid 而静默跳过, 用户完全不知道。
      实测: 2026-09-12 14:55:06 测试号 18888889999 的 openid 就是这样被覆盖掉的。
    规则: 新的为空 -> 保留旧值; 新的是 ooTcRx -> 用新的; 旧的是空的 -> 用新的;
          旧的是 ooTcRx 而新的不是 -> 保留旧值(拒绝降级); 其余 -> 用新的。
    """
    phone = _clean(phone)
    openid = _clean(openid)
    unionid = _clean(unionid)
    mp_openid = _clean(mp_openid)
    wechat_name = _clean(wechat_name)
    gzh_openid = _clean(gzh_openid)
    user_id = int(user_id or 0)
    if not phone:
        return None
    if unionid:
        # ★★ UN 只认已绑定的第一个手机号 (2026-09-10)：若该 unionid 已绑定过任何手机号，
        #    则本次传入的 phone 只用于补记 openid/mp_openid，不再新增行、也不改绑手机号。
        #    (防止"同一 unionid 因换/错手机号而生出多身份"的脏数据；老数据保持不变)
        try:
            cursor.execute(
                "SELECT id, phone FROM phone_openids WHERE unionid = %s AND NULLIF(phone,'') IS NOT NULL ORDER BY id ASC LIMIT 1",
                (unionid,))
            _g = cursor.fetchone()
            # 兼容 RealDictCursor(dict) 与普通 cursor(tuple)
            _gid = _g['id'] if isinstance(_g, dict) else (_g[0] if _g else None)
            _gphone = _g['phone'] if isinstance(_g, dict) else (_g[1] if _g else None)
            if _g and _gphone and _gphone != phone:
                cursor.execute(
                    """UPDATE phone_openids SET
                         openid = COALESCE(NULLIF(%s,''), openid),
                         mp_openid = keep_new_mp_openid(mp_openid, %s),
                         wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                         gzh_openid = COALESCE(NULLIF(%s,''), gzh_openid),
                         updated_at = NOW()
                       WHERE id = %s""",
                    (openid, mp_openid, wechat_name, gzh_openid, _gid))
                return _gid
        except Exception as _ge:
            logger.warning(f'[upsert_phone_openid] UN守卫检查失败: {_ge}')
        # 历史脏数据兜底：旧行可能只有 phone+openid/mp_openid 而没有 unionid，
        # 直接 INSERT 会撞 (phone, openid) 唯一索引。先按已有身份找行并更新。
        _existing_id = None
        if openid:
            cursor.execute("SELECT id FROM phone_openids WHERE phone = %s AND openid = %s LIMIT 1", (phone, openid))
            _r = cursor.fetchone()
            if _r:
                _existing_id = _r['id']
        if not _existing_id and mp_openid:
            cursor.execute("SELECT id FROM phone_openids WHERE phone = %s AND mp_openid = %s LIMIT 1", (phone, mp_openid))
            _r = cursor.fetchone()
            if _r:
                _existing_id = _r['id']
        if not _existing_id:
            cursor.execute("SELECT id FROM phone_openids WHERE phone = %s AND unionid = %s LIMIT 1", (phone, unionid))
            _r = cursor.fetchone()
            if _r:
                _existing_id = _r['id']
        if _existing_id:
            # 修复(2026-09-10): 目标 unionid 若已被同手机号的其他行占用(一机两号场景),
            # 本次不更新 unionid, 避免撞 idx_phone_openids_phone_unionid 唯一约束导致 link-mp-openid 500
            _upd_unionid = unionid
            if unionid:
                try:
                    cursor.execute("SELECT id FROM phone_openids WHERE phone = %s AND unionid = %s AND id <> %s LIMIT 1", (phone, unionid, _existing_id))
                    if cursor.fetchone():
                        _upd_unionid = ''
                        logger.info('[upsert_phone_openid] unionid已被同号码其他行占用, 本次不更新: phone=%s', phone)
                except Exception:
                    _upd_unionid = unionid
            cursor.execute(
                """UPDATE phone_openids SET
                     openid = COALESCE(NULLIF(%s,''), openid),
                     mp_openid = keep_new_mp_openid(mp_openid, %s),
                     unionid = COALESCE(NULLIF(%s,''), unionid),
                     wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                     gzh_openid = COALESCE(NULLIF(%s,''), gzh_openid),
                     user_id = CASE WHEN %s > 0 THEN %s ELSE user_id END,
                     updated_at = NOW()
                   WHERE id = %s
                   RETURNING id""",
                (openid, mp_openid, _upd_unionid, wechat_name, gzh_openid, user_id, user_id, _existing_id),
            )
            _row = cursor.fetchone()
            return _row['id'] if _row else _existing_id
        cursor.execute(
            """INSERT INTO phone_openids (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (phone, unionid) WHERE unionid IS NOT NULL AND unionid <> ''
               DO UPDATE SET
                 openid = COALESCE(NULLIF(EXCLUDED.openid,''), phone_openids.openid),
                 mp_openid = keep_new_mp_openid(phone_openids.mp_openid, EXCLUDED.mp_openid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), phone_openids.wechat_name),
                 gzh_openid = COALESCE(NULLIF(EXCLUDED.gzh_openid,''), phone_openids.gzh_openid),
                 user_id = CASE WHEN %s > 0 THEN %s ELSE phone_openids.user_id END,
                 updated_at = NOW()
               RETURNING id""",
            (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, user_id, user_id),
        )
    elif openid:
        cursor.execute(
            """INSERT INTO phone_openids (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (phone, openid) WHERE openid IS NOT NULL AND openid <> ''
               DO UPDATE SET
                 mp_openid = keep_new_mp_openid(phone_openids.mp_openid, EXCLUDED.mp_openid),
                 unionid = COALESCE(NULLIF(EXCLUDED.unionid,''), phone_openids.unionid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), phone_openids.wechat_name),
                 gzh_openid = COALESCE(NULLIF(EXCLUDED.gzh_openid,''), phone_openids.gzh_openid),
                 user_id = CASE WHEN %s > 0 THEN %s ELSE phone_openids.user_id END,
                 updated_at = NOW()
               RETURNING id""",
            (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, user_id, user_id),
        )
    elif mp_openid:
        cursor.execute(
            """INSERT INTO phone_openids (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (phone, mp_openid) WHERE mp_openid IS NOT NULL AND mp_openid <> ''
               DO UPDATE SET
                 openid = COALESCE(NULLIF(EXCLUDED.openid,''), phone_openids.openid),
                 unionid = COALESCE(NULLIF(EXCLUDED.unionid,''), phone_openids.unionid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), phone_openids.wechat_name),
                 gzh_openid = COALESCE(NULLIF(EXCLUDED.gzh_openid,''), phone_openids.gzh_openid),
                 user_id = CASE WHEN %s > 0 THEN %s ELSE phone_openids.user_id END,
                 updated_at = NOW()
               RETURNING id""",
            (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, user_id, user_id),
        )
    else:
        cursor.execute("SELECT id FROM phone_openids WHERE phone = %s LIMIT 1", (phone,))
        r = cursor.fetchone()
        if r:
            cursor.execute(
                """UPDATE phone_openids SET
                     openid = COALESCE(NULLIF(%s,''), openid),
                     mp_openid = keep_new_mp_openid(mp_openid, %s),
                     unionid = COALESCE(NULLIF(%s,''), unionid),
                     wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                     gzh_openid = COALESCE(NULLIF(%s,''), gzh_openid),
                     user_id = CASE WHEN %s > 0 THEN %s ELSE user_id END,
                     updated_at = NOW()
                   WHERE id = %s
                   RETURNING id""",
                (openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, user_id, r['id']),
            )
        else:
            cursor.execute(
                """INSERT INTO phone_openids (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                   RETURNING id""",
                (phone, openid, mp_openid, unionid, wechat_name, gzh_openid, user_id),
            )
    row = cursor.fetchone()
    return row['id'] if row else None

def return_to_balance(phone, amount, withdrawal_id=None, openid='', order_id=None):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        unionid = ''
        mp_openid = ''
        if order_id:
            try:
                cur.execute("SELECT user_phone, openid, unionid, mp_openid FROM orders WHERE id = %s", (order_id,))
                order = cur.fetchone()
                if order:
                    phone = order['user_phone'] or phone
                    openid = order['openid'] or openid
                    unionid = order['unionid'] or ''
                    mp_openid = order['mp_openid'] or ''
            except Exception:
                pass
        upsert_user_balance_row(cur, phone=phone, openid=openid, unionid=unionid, mp_openid=mp_openid,
                                balance=amount, total_withdrawn=-amount)
        if withdrawal_id:
            cur.execute("UPDATE withdrawal_records SET status = 3 WHERE id = %s", (withdrawal_id,))
        # 拒绝退款时：恢复余额明细状态为available
        if order_id:
            cur.execute("UPDATE user_balance_details SET status = 'available' WHERE order_id = %s AND status = 'pending'", (order_id,))
        conn.commit()
        conn.close()
        logger.info("[return_to_balance] phone=" + str(phone) + " amount=" + str(amount) + " order_id=" + str(order_id))
        return True
    except Exception as e:
        logger.error("[return_to_balance] Failed: " + str(e))
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def refund_deposit_to_balance(cursor, order):
    """清柜/定时清柜统一退押金到余额，返回 (是否退款, mp_openid)"""
    deposit = float(order.get('deposit_amount') or 0)
    phone = str(order.get('user_phone') or '')
    if deposit <= 0 or not phone:
        return False, '', False
    openid = order.get('openid') or ''
    unionid = order.get('unionid') or ''
    mp_openid = order.get('mp_openid') or ''
    wechat_name = order.get('wechat_name') or ''
    if not mp_openid:
        rows = phone_openid_rows(cursor, phone=phone, openid=openid, mp_openid=mp_openid, unionid=unionid)
        if not rows and openid:
            rows = phone_openid_rows(cursor, openid=openid)
        if not rows and unionid:
            rows = phone_openid_rows(cursor, unionid=unionid)
        if not rows:
            rows = phone_openid_rows(cursor, phone=phone)
        if len(rows) == 1:
            row = rows[0]
            mp_openid = row.get('mp_openid') or mp_openid
            if not openid:
                openid = row.get('openid') or ''
            if not unionid:
                unionid = row.get('unionid') or ''
            if not wechat_name:
                wechat_name = row.get('wechat_name') or ''
    try:
        cursor.execute("SELECT 1 FROM user_balance_details WHERE order_id = %s LIMIT 1", (order.get('id'),))
        if cursor.fetchone():
            return True, mp_openid, True
    except Exception:
        pass
    upsert_user_balance_row(cursor, phone=phone, openid=openid, unionid=unionid, mp_openid=mp_openid,
                            wechat_name=wechat_name, balance=deposit, total_deposited=deposit,
                            user_id=order.get('user_id') or 0)
    cursor.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING", (phone, order['id'], deposit))
    return True, mp_openid, False


def do_real_refund(order_id=None, order_no=None, amount=0, payment_channel_id=None, skip_balance=False, **kwargs):
    """Actually call WeChat refund API. Returns (success, refund_id, message)"""
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        if order_id:
            cursor.execute('SELECT order_no, transaction_id, payment_channel_id FROM orders WHERE id=%s', (order_id,))
            row = cursor.fetchone()
            if row:
                order_no = order_no or row['order_no']
                payment_channel_id = payment_channel_id or row['payment_channel_id']
        conn.close()
        if not order_no:
            return False, '', 'Order number is empty'
        payer = None
        if payment_channel_id:
            try:
                conn2 = get_db()
                cursor2 = conn2.cursor()
                cursor2.execute('SELECT * FROM payment_channels WHERE id=%s ', (payment_channel_id,))
                channel = cursor2.fetchone()
                conn2.close()
                if channel:
                    channel_dict = {}
                    for key in channel.keys():
                        channel_dict[key] = channel[key]
                    payer, _ = get_channel_wxpay(channel_dict)
            except:
                pass
        if not payer:
            # 尝试从订单的payment_channel_id获取活跃商户
            try:
                if order_id:
                    _rc = conn.cursor()
                    _rc.execute("SELECT payment_channel_id FROM orders WHERE id=%s", (order_id,))
                    _rr = _rc.fetchone()
                    _rc.close()
                    if _rr and _rr.get('payment_channel_id'):
                        _rc2 = conn.cursor()
                        _rc2.execute("SELECT * FROM payment_channels WHERE id=%s", (_rr['payment_channel_id'],))
                        _rch = _rc2.fetchone()
                        if _rch:
                            payer, _ = get_channel_wxpay(dict(_rch))
                        _rc2.close()
            except Exception as _e:
                logger.error('[do_real_refund] 渠道查询异常: %s' % _e)
        if not payer:
            logger.error('[do_real_refund] 无可用活跃商户，退款跳过微信API')
            return False, '', '无可用活跃商户'
        # 查询订单原始支付金额
        if order_id:
            conn3 = get_db()
            cursor3 = conn3.cursor()
            cursor3.execute("""
                SELECT o.deposit_amount, o.per_use_price,
                       (SELECT p.amount FROM payments p WHERE p.order_id=o.id AND p.type=1 AND p.status=1 AND p.amount<=1000 ORDER BY p.id LIMIT 1) AS paid_amount
                FROM orders o WHERE o.id=%s
            """, (order_id,))
            order_row = cursor3.fetchone()
            conn3.close()
            if order_row:
                total_fee = int((float(order_row['deposit_amount']) + float(order_row.get('per_use_price') or 0)) * 100)
            else:
                total_fee = int(float(amount) * 100)
        else:
            total_fee = int(float(amount) * 100)
        refund_fee = int(float(amount) * 100)
        result = payer.refund(out_trade_no=order_no, total_fee=total_fee, refund_fee=refund_fee)
        if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
            refund_id = result.get('refund_id') or result.get('out_refund_no', '')
            logger.info('[do_real_refund] Success: order=%s, refund_id=%s' % (order_no, refund_id))
            # 更新订单退款状态（calc_balance 模式：余额实时计算，无需操作 user_balances）
            if order_id:
                try:
                    conn_bal = get_db()
                    c_bal = conn_bal.cursor()
                    c_bal.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=%s, refund_amount=COALESCE(%s, refund_amount), refund_time=NOW(), refund_mark=1 WHERE id=%s", (refund_id, amount, order_id))
                    if c_bal.rowcount > 0:
                        logger.info("[do_real_refund] Orders updated: order_id=%s" % order_id)
                    c_bal.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order_id,))
                    # 退款成功=订单结束，释放柜门，防止"钱退了柜门还占着"的幽灵占用
                    try:
                        c_bal.execute("UPDATE cabinet_slots SET status=1 WHERE id=(SELECT slot_id FROM orders WHERE id=%s) AND status=2", (order_id,))
                        if c_bal.rowcount > 0:
                            logger.info("[do_real_refund] slot released: order_id=%s" % order_id)
                    except Exception as _sl_e:
                        logger.error('[do_real_refund] slot release err: %s' % _sl_e)
                    conn_bal.commit()
                    conn_bal.close()
                except Exception as be:
                    logger.error('[do_real_refund] Order status update err: %s' % be)
                    try: conn_bal.close()
                    except: pass
            return True, refund_id, 'Refund successful'
        else:
            err_msg = result.get('err_code_des') or result.get('err_code') or result.get('return_msg') or 'Refund failed'
            logger.error('[do_real_refund] Failed: order=%s, msg=%s, result=%s' % (order_no, err_msg, str(result)))
            # 微信明确表示订单已退款/已全额退款时，按退款成功处理，避免恢复余额导致双倍到账
            _already_refunded = ('订单已全额退款' in str(err_msg)) or ('该订单已全额退款' in str(err_msg))
            if _already_refunded:
                _rid = result.get('refund_id') or result.get('out_refund_no') or ('ALREADY_' + str(order_id or order_no))
                logger.info('[do_real_refund] Already refunded: order=%s, refund_id=%s, msg=%s' % (order_no, _rid, err_msg))
                if order_id:
                    try:
                        _bal = get_db()
                        _balc = _bal.cursor()
                        _balc.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=%s, refund_amount=COALESCE(%s, refund_amount), refund_time=NOW(), refund_mark=1 WHERE id=%s", (_rid, amount, order_id))
                        _balc.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order_id,))
                        _bal.commit()
                        _bal.close()
                    except Exception as _be:
                        logger.error('[do_real_refund] Already-refunded order update err: %s' % _be)
                        try:
                            _bal.close()
                        except Exception:
                            pass
                return True, _rid, err_msg
            # 被动检测：判断是否为商户账户级错误
            _ec = result.get('err_code', '')
            # 获取当前渠道信息用于告警
            _alert_channel = None
            if payment_channel_id:
                try:
                    _ac = get_db()
                    _ac_c = _ac.cursor()
                    _ac_c.execute('SELECT id, name, mch_id FROM payment_channels WHERE id=%s', (payment_channel_id,))
                    _ac_row = _ac_c.fetchone()
                    if _ac_row:
                        _alert_channel = dict(_ac_row)
                    _ac.close()
                except:
                    pass
            if is_merchant_account_error(_ec):
                _merchant_health_state['consecutive_errors'] += 1
                _on_merchant_error(_ec, err_msg, result, channel=_alert_channel)
            elif result.get('return_code') != 'SUCCESS':
                # return_code 非 SUCCESS 也可能是账户问题
                _rc = result.get('return_code', '')
                if is_merchant_account_error(_rc):
                    _merchant_health_state['consecutive_errors'] += 1
                    _on_merchant_error(_rc, err_msg, result, channel=_alert_channel)
            return False, '', err_msg
    except Exception as e:
        logger.error('[do_real_refund] Exception: %s' % e)
        return False, '', str(e)


def do_balance_transfer(phone, amount, openid=None, user_id=0):
    """Transfer balance to user WeChat wallet. Returns (success, payment_no, message)"""
    try:
        from database import get_db
        if not openid:
            conn = get_db()
            cursor = conn.cursor()
            ub_row = find_user_balance_row(cursor, phone=phone, openid=openid, user_id=user_id)
            if ub_row and ub_row.get('openid'):
                openid = ub_row['openid']
                conn.close()
            else:
                conn.close()
                logger.error('[do_balance_transfer] No openid for %s' % phone)
                return False, '', 'User openid is empty'
        # 使用订单关联的活跃商户进行转账，不用硬编码默认商户
        _ch = None
        try:
            _cur = conn.cursor()
            if user_id:
                _cur.execute("SELECT payment_channel_id FROM orders WHERE user_id=%s AND payment_channel_id IS NOT NULL ORDER BY id DESC LIMIT 1", (user_id,))
            else:
                _cur.execute("SELECT payment_channel_id FROM orders WHERE user_phone=%s AND payment_channel_id IS NOT NULL ORDER BY id DESC LIMIT 1", (phone,))
            _row = _cur.fetchone()
            if _row and _row.get('payment_channel_id'):
                _cur.execute("SELECT * FROM payment_channels WHERE id=%s AND is_active=1", (_row['payment_channel_id'],))
                _ch_row = _cur.fetchone()
                if _ch_row:
                    payer, _ = get_channel_wxpay(dict(_ch_row))
                    _cur.close()
            else:
                _cur.close()
        except Exception as _e:
            logger.error('[do_balance_transfer] 渠道查询异常: %s' % _e)
        if not payer:
            # 没有活跃渠道时选一个活跃的
            try:
                _cur2 = conn.cursor()
                _cur2.execute("SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1")
                _ch2 = _cur2.fetchone()
                if _ch2:
                    payer, _ = get_channel_wxpay(dict(_ch2))
                _cur2.close()
            except:
                pass
        if not payer:
            logger.error('[do_balance_transfer] 无可用活跃商户，无法转账')
            return False, '', '无可用活跃商户'
        partner_trade_no = 'WD' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
        result = payer.transfer(
            partner_trade_no=partner_trade_no,
            openid=openid,
            amount=int(float(amount) * 100),
            desc='Locker balance withdrawal'
        )
        if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
            payment_no = result.get('payment_no', '')
            logger.info('[do_balance_transfer] Success: phone=%s, payment_no=%s' % (phone, payment_no))
            return True, payment_no, 'Transfer successful'
        else:
            err_msg = result.get('return_msg') or result.get('err_code_des') or 'Transfer failed'
            logger.error('[do_balance_transfer] Failed: phone=%s, msg=%s' % (phone, err_msg))
            return False, '', err_msg
    except Exception as e:
        logger.error('[do_balance_transfer] Exception: %s' % e)
        return False, '', str(e)



def get_access_token(force_refresh=False):
    from datetime import datetime, timedelta
    try:
        conn = get_db()
        cur = conn.cursor()
        if not force_refresh:
            cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = 'wx_mp_access_token'")
            row = cur.fetchone()
            if row and row['setting_value']:
                try:
                    import json as _j
                    data = _j.loads(row['setting_value'])
                    expires_at = datetime.fromisoformat(data['expires_at'])
                    if datetime.now() < expires_at - timedelta(seconds=600):
                        conn.close()
                        return data['token']
                except:
                    pass
        import requests as _r
        url = 'https://api.weixin.qq.com/cgi-bin/stable_token'
        payload = dict(grant_type='client_credential', appid=WX_MP_APP_ID, secret=WX_MP_APP_SECRET, force_refresh=force_refresh)
        resp = _r.post(url, json=payload, timeout=5)
        result = resp.json()
        if 'access_token' in result:
            token = result['access_token']
            ei = result.get('expires_in', 7200)
            ea = (datetime.now() + timedelta(seconds=ei)).isoformat()
            import json as _j2
            cd = _j2.dumps(dict(token=token, expires_at=ea))
            cur.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)", ('wx_mp_access_token', cd))
            conn.commit()
            conn.close()
            return token
        logger.error(f'[get_access_token] fail: {result}')
        conn.close()
        return None
    except Exception as e:
        logger.error(f'[get_access_token] err: {e}')
        try: conn.close()
        except: pass
        return None

        access_token = token_data['access_token']

        # 发送订阅消息
        send_url = f'https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={access_token}'
        payload = {
            'touser': openid,
            'template_id': template_id,
            'data': data
        }
        if page:
            payload['page'] = page

        resp = requests.post(send_url, json=payload, timeout=5)
        result = resp.json()

        if result.get('errcode') == 0:
            logger.info(f'[subscribe_msg] 发送成功: openid={openid[:8]}..., template={template_id}')
            return True
        else:
            logger.error(f'[subscribe_msg] 发送失败: {result}')
            return False
    except Exception as e:
        logger.error(f'[subscribe_msg] 异常: {e}')
        return False


# ============================================
# PushPlus 推送 & 商户号健康检查
# ============================================

# 商户号异常的错误码
_MERCHANT_ERROR_CODES = {'SIGN_ERROR', 'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'SYSTEMERROR', 'FREQUENCY_LIMITED'}  # NO_AUTH removed
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0

def send_pushplus(title, content, template='txt'):
    """通过 PushPlus 发送微信通知"""
    import requests, json
    try:
        from config import PUSHPLUS_TOKEN
        if not PUSHPLUS_TOKEN:
            logger.warning('[PushPlus] Token 未配置')
            return False
        url = 'http://www.pushplus.plus/send'
        data = {'token': PUSHPLUS_TOKEN, 'title': title, 'content': content, 'template': template}
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result.get('code') == 200:
            logger.info('[PushPlus] 推送成功: %s' % title)
            return True
        else:
            logger.error('[PushPlus] 推送失败: %s' % str(result))
            return False
    except Exception as e:
        logger.error('[PushPlus] 异常: %s' % e)
        return False

def is_merchant_account_error(err_code):
    """判断错误码是否为商户账户级别错误"""
    if not err_code:
        return False
    err_code_upper = str(err_code).upper()
    return err_code_upper in _MERCHANT_ERROR_CODES

def _on_merchant_error(err_code, err_desc, raw_result, channel=None):
    """商户号异常告警，防止短时间内重复推送，包含具体商户号信息"""
    import time
    now = time.time()
    # 按商户号独立记录告警时间，避免一个商户告警后其他商户的告警被跳过
    mch_key = 'last_alert_%s' % (channel['mch_id'] if channel else 'default')
    last = _merchant_health_state.get(mch_key, 0)
    if now - last < 600:  # 10分钟内同商户不重复告警
        return
    _merchant_health_state[mch_key] = now
    _merchant_health_state['last_alert_time'] = now
    # 构造包含商户号信息的告警内容
    mch_id = channel.get('mch_id', '未知') if channel else '未知(默认渠道)'
    mch_name = channel.get('name', '未知') if channel else '未知(默认渠道)'
    ch_id = channel.get('id', '?') if channel else '?'
    title = '【%s】商户号被封' % mch_name
    content = ("微信支付商户号出现异常，可能被限制或封禁。\n"
               "商户名称: %s\n"
               "商户号(mch_id): %s\n"
               "渠道ID: %s\n"
               "错误码: %s\n"
               "错误描述: %s\n"
               "请立刻登录 pay.weixin.qq.com 查看。") % (mch_name, mch_id, ch_id, err_code, err_desc)
    send_pushplus(title, content)


def check_merchant_health():
    """主动探测所有活跃商户号状态"""
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        # 查询所有活跃渠道
        cursor.execute("SELECT * FROM payment_channels WHERE is_active = 1")
        channels = cursor.fetchall()
        if not channels:
            logger.debug('[MerchantHealth] 无活跃支付渠道，跳过')
            conn.close()
            return True

        all_ok = True
        for ch_row in channels:
            channel = dict(ch_row)
            ch_name = channel.get('name', '未知')
            mch_id = channel.get('mch_id', '未知')
            try:
                # 找该渠道的最近一笔已支付订单作为探测目标
                cursor.execute(
                    "SELECT order_no FROM orders WHERE status IN (2,3,4) "
                    "AND transaction_id IS NOT NULL AND transaction_id != '' "
                    "AND payment_channel_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (channel['id'],))
                row = cursor.fetchone()
                if not row or not row.get('order_no'):
                    logger.debug('[MerchantHealth] 渠道 %s(%s) 无探测订单，跳过' % (ch_name, mch_id))
                    continue

                payer, ch_type = get_channel_wxpay(channel)
                if not payer:
                    logger.warning('[MerchantHealth] 渠道 %s 无法创建支付实例' % ch_name)
                    continue

                result = payer.order_query(out_trade_no=row['order_no'])
                rc = result.get('return_code', '')
                if rc == 'SUCCESS':
                    logger.debug('[MerchantHealth] 渠道 %s(%s) 正常' % (ch_name, mch_id))
                    _merchant_health_state[f'success_mch_{channel["id"]}'] = time.time()
                else:
                    ec = result.get('err_code', '') or rc
                    err_desc = result.get('err_code_des') or result.get('return_msg', '')
                    if is_merchant_account_error(ec):
                        logger.error('[MerchantHealth] 渠道 %s(%s) 异常! err=%s %s' % (ch_name, mch_id, ec, err_desc))
                        # 自动禁用该渠道
                        cursor.execute('UPDATE payment_channels SET is_active=0, auto_disabled=1 WHERE id=%s', (channel['id'],))
                        conn.commit()
                        logger.warning('[MerchantHealth] 已自动禁用渠道: %s(%s)' % (ch_name, mch_id))
                        _on_merchant_error(ec, err_desc, result, channel=channel)
                        all_ok = False
                    else:
                        logger.warning('[MerchantHealth] 渠道 %s 非预期返回: %s' % (ch_name, str(result)))
            except Exception as e:
                logger.error('[MerchantHealth] 渠道 %s 探测异常: %s' % (ch_name, e))
        conn.close()
        return all_ok
    except Exception as e:
        logger.error('[MerchantHealth] 探测失败: %s' % e)
        return False


_MH_LOCK_FILE = '/tmp/merchant_health_patrol.lock'
_MH_ROUNDS_FILE = '/tmp/merchant_health_rounds.txt'


def _mh_try_lock():
    """保证同一时刻只有一个进程在跑商户号巡检。

    背景: app.py 用模块级 threading.Thread(target=merchant_health_scheduler) 启动本巡检,
    而 gunicorn 没开 --preload, 8 个 worker 各自 import 一次 app
    -> 同一个巡检被复制成 8 份并行跑。2026-09-12 实测: 每分钟 8 个进程各打 12 条日志、
    全天 23.8 万条日志把 journald 灌到 2.7G。用文件锁后同一时刻只有一个执行者。
    """
    import fcntl
    f = None
    try:
        f = open(_MH_LOCK_FILE, 'a+')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        try:
            if f:
                f.close()
        except Exception:
            pass
        return None


def _mh_bump_rounds():
    """把巡检轮次累加到共享文件。

    为什么要落文件: gunicorn 配了 --max-requests 200, worker 跑够请求数就被回收,
    持有锁的进程因此会频繁更换。如果轮次只存在进程内存里, 每次切换心跳都会从"第1轮"重来,
    看起来就像巡检被复制了多份。落到共享文件后, 轮次全局连续且准确。
    """
    import os
    n = 0
    try:
        with open(_MH_ROUNDS_FILE, 'r') as f:
            n = int((f.read() or '0').strip() or 0)
    except Exception:
        n = 0
    n += 1
    try:
        tmp = _MH_ROUNDS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            f.write(str(n))
        os.replace(tmp, _MH_ROUNDS_FILE)
    except Exception:
        pass
    return n


def merchant_health_scheduler():
    """商户号健康巡检 + 自动灾备切换（单实例）。

    [FIX-20260912] 三处调整。起因: 该巡检每天产生 23.8 万条日志(把 journald 顶到 2.7G),
    并对微信支付发起约 6.9 万次/天的主动查询(按设计只需约 1,440 次)。
      1) 文件锁: 同一时刻只有一个 worker 真正执行(原来 8 个 worker 各跑一份并行);
      2) 间隔 10 秒 -> 60 秒;
      3) 例行日志降为 debug, 只保留每 15 分钟一条全局心跳 + 异常日志。
    执行者被 gunicorn 回收后, 其他 worker 会在下一轮自动接管, 轮次计数不中断。
    """
    import time
    from database import get_db
    global _failover_consecutive_fails
    time.sleep(60)
    while True:
        lock_f = _mh_try_lock()
        if lock_f is None:
            # 已有其他 worker 在执行: 静默等待(不产生日志), 一分钟后重试
            time.sleep(60)
            continue
        try:
            # 拿到锁: 本进程成为唯一执行者, 持续跑(直到被 gunicorn 回收释放锁)
            while True:
                conn_f = None
                try:
                    _n = _mh_bump_rounds()
                    check_merchant_health()
                    # Auto-failover
                    conn_f = get_db()
                    c_f = conn_f.cursor()
                    c_f.execute("SELECT count(*) FROM payment_channels WHERE is_active=1")
                    _ac = c_f.fetchone()[0]
                    if _ac == 0:
                        try:
                            _activate_next_channel()
                        except Exception:
                            pass
                    conn_f.close()
                    conn_f = None
                    # 每 15 分钟一条全局心跳(证明巡检还活着); 原来每轮都打 -> 每天23.8万条
                    if _n == 1 or _n % 15 == 0:
                        logger.info('[MerchantHealth] 巡检心跳: 第 %d 轮(每60秒一轮, 单实例)' % _n)
                except Exception as e:
                    logger.error('[MerchantHealth/failover] %s' % e)
                finally:
                    if conn_f:
                        conn_f.close()
                time.sleep(60)
        finally:
            try:
                lock_f.close()
            except Exception:
                pass



def assign_merchant(phone=None, openid=None, user_id=0):
    """为新用户分配商户号"""
    try:
        from database import get_db
        c = get_db()
        cur = c.cursor()
        if user_id:
            row = find_user_balance_row(cur, user_id=user_id, phone=phone, openid=openid)
        elif openid:
            row = find_user_balance_row(cur, openid=openid)
        elif phone:
            row = find_user_balance_row(cur, phone=phone)
        else:
            row = None
        if row and row.get('merchant_id'):
            _alive = c.execute("SELECT id FROM payment_channels WHERE mch_id=%s AND is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0)", (row['merchant_id'],)).fetchone()
            if _alive:
                c.close()
                return row['merchant_id']
            # merchant disabled, fall through to pick a new one
        row = c.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) ORDER BY total_users ASC LIMIT 1").fetchone()
        if not row:
            c.close()
            return None
        mch_id = row[0]
        ub_row = find_user_balance_row(cur, phone=phone, openid=openid, user_id=user_id)
        if ub_row:
            c.execute("UPDATE user_balances SET merchant_id=%s WHERE id=%s", (mch_id, ub_row['id']))
        elif openid:
            c.execute("UPDATE user_balances SET merchant_id=%s WHERE openid=%s", (mch_id, openid))
        elif phone:
            rows = phone_openid_rows(cur, phone=phone)
            if len(rows) == 1:
                c.execute("UPDATE user_balances SET merchant_id=%s WHERE phone=%s", (mch_id, phone))
        c.execute("UPDATE payment_channels SET total_users = (SELECT COUNT(*) FROM user_balances WHERE merchant_id=%s) WHERE mch_id=%s", (mch_id, mch_id))
        c.commit()
        c.close()
        logger.info(f'[MERCHANT] assigned {mch_id}')
        return mch_id
    except Exception as e:
        logger.error(f'[MERCHANT] assign error: {e}')
        return None

def get_withhold_hours(mch_id):
    """根据商户号交易量和投诉率返回卡顿时长"""
    try:
        from database import get_db
        c = get_db()
        row = c.execute("""SELECT COUNT(*) as total, COALESCE((SELECT COUNT(*) FROM complaints co WHERE co.mch_id=%s),0) as comp FROM orders o JOIN payment_channels pc ON o.payment_channel_id=pc.id WHERE pc.mch_id=%s""", (mch_id, mch_id)).fetchone()
        c.close()
        total, comp = row[0], row[1]
        rate = comp / max(total, 1)
        if rate > 0.005:  return 0   # 投诉率>0.5%关闭卡顿
        if total < 200:   return 0   # 保护期
        if total < 500:   return 2   # 轻度
        if total < 1000:  return 12  # 观察期
        return 72                     # 成熟期
    except Exception as e:
        logger.error(f'[MERCHANT] get_withhold error: {e}')
        return 72

def check_withdraw_auto_approve(openid=None, phone=None, user_id=0):
    """检查提现是否需要审批"""
    try:
        from database import get_db
        c = get_db()
        cur = c.cursor()
        if user_id:
            ub = find_user_balance_row(cur, user_id=user_id, phone=phone, openid=openid)
        elif openid:
            ub = find_user_balance_row(cur, openid=openid)
        elif phone:
            ub = find_user_balance_row(cur, phone=phone)
        else:
            c.close()
            return True
        if not ub:
            c.close()
            return False  # 新用户放行
        ht, cc, mi = ub.get('has_triggered_withdraw'), ub.get('complaint_count'), ub.get('merchant_id')
        c.close()
        if cc > 0 or ht:
            return False  # 已投诉/已提现过 → 放行
        if mi:
            h = get_withhold_hours(mi)
            if h == 0:
                return False  # 商户号保护期 → 放行
            return True  # 需要审批
        return False  # 无商户号归属 → 放行，避免卡单
    except Exception as e:
        logger.error(f'[MERCHANT] check_approve error: {e}')
        return True

def mark_user_withdraw(openid=None, phone=None, user_id=0):
    """标记用户已发起过提现"""
    try:
        from database import get_db
        c = get_db()
        cur = c.cursor()
        if user_id:
            ub = find_user_balance_row(cur, user_id=user_id, phone=phone, openid=openid)
        elif openid:
            ub = find_user_balance_row(cur, openid=openid)
        elif phone:
            ub = find_user_balance_row(cur, phone=phone)
        else:
            ub = None
        if ub:
            c.execute("UPDATE user_balances SET has_triggered_withdraw=TRUE WHERE id=%s", (ub['id'],))
        elif openid:
            c.execute("UPDATE user_balances SET has_triggered_withdraw=TRUE WHERE openid=%s", (openid,))
        elif phone:
            rows = phone_openid_rows(cur, phone=phone)
            if len(rows) == 1:
                c.execute("UPDATE user_balances SET has_triggered_withdraw=TRUE WHERE phone=%s", (phone,))
        c.commit()
        c.close()
    except Exception as e:
        logger.error(f'[MERCHANT] mark error: {e}')
# ====== 结束 ======

# 防重缓存：记录每个order_id最后一次开门时间
_last_open_lock_time = {}
# ====== ????????????????????? ======
def _resolve_unionid(openid='', phone=''):
    """按 openid/手机号解析 unionid，白名单统一按 UN 认人"""
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        if openid:
            cur.execute("SELECT unionid FROM phone_openids WHERE openid = %s AND unionid IS NOT NULL AND unionid != '' ORDER BY id DESC LIMIT 1", (openid,))
            row = cur.fetchone()
            if row and row.get('unionid'):
                conn.close()
                return row['unionid']
            cur.execute("SELECT unionid FROM user_balances WHERE openid = %s AND unionid IS NOT NULL AND unionid != '' ORDER BY id DESC LIMIT 1", (openid,))
            row = cur.fetchone()
            if row and row.get('unionid'):
                conn.close()
                return row['unionid']
        if phone:
            cur.execute("SELECT unionid FROM phone_openids WHERE phone = %s AND unionid IS NOT NULL AND unionid != '' ORDER BY id DESC LIMIT 1", (phone,))
            row = cur.fetchone()
            if row and row.get('unionid'):
                conn.close()
                return row['unionid']
            cur.execute("SELECT unionid FROM user_balances WHERE phone = %s AND unionid IS NOT NULL AND unionid != '' ORDER BY id DESC LIMIT 1", (phone,))
            row = cur.fetchone()
            if row and row.get('unionid'):
                conn.close()
                return row['unionid']
        conn.close()
    except Exception as e:
        logger.error("[resolve_unionid] " + str(e))
    return ''


def check_whitelist(openid='', unionid=''):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        if unionid:
            cur.execute("SELECT openid, source, remain_count, unionid, created_at FROM withdrawal_whitelist WHERE unionid = %s AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1", (unionid,))
            row = cur.fetchone()
            if row:
                conn.close()
                return row
        if openid:
            cur.execute("SELECT openid, source, remain_count, unionid, created_at FROM withdrawal_whitelist WHERE openid = %s AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1", (openid,))
            row = cur.fetchone()
            if row and unionid and not (row.get('unionid') or ''):
                try:
                    cur.execute("UPDATE withdrawal_whitelist SET unionid = %s WHERE openid = %s", (unionid, openid))
                    conn.commit()
                except Exception:
                    pass
            conn.close()
            return row
        conn.close()
        return None
    except Exception as e:
        logger.error("[check_whitelist] " + str(e))
        return None


def add_whitelist(openid, source, remain_count=-1, unionid='', expire_days=None):
    """加入提现白名单。expire_days>0: N天后过期; 重复拉白时次数取较小值(不重置回满), 有效期不刷新(保留原值)"""
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        if not unionid:
            unionid = _resolve_unionid(openid=openid)
        # 过期天数参数化(make_interval), 不能拼SQL字符串当参数传, 否则报timestamp语法错
        _exp_days = int(expire_days) if (expire_days is not None and expire_days > 0) else None
        if unionid:
            cur.execute("SELECT openid FROM withdrawal_whitelist WHERE unionid = %s LIMIT 1", (unionid,))
            exist = cur.fetchone()
            if exist:
                cur.execute("""UPDATE withdrawal_whitelist SET openid = %s, source = %s,
                               remain_count = CASE
                                 WHEN withdrawal_whitelist.remain_count = -1 THEN %s
                                 WHEN %s = -1 THEN withdrawal_whitelist.remain_count
                                 ELSE LEAST(withdrawal_whitelist.remain_count, %s)
                               END
                               WHERE unionid = %s""",
                            (openid, source, remain_count, remain_count, remain_count, unionid))
                conn.commit()
                conn.close()
                return True
        sql = """INSERT INTO withdrawal_whitelist (openid, source, remain_count, unionid, created_at, expires_at)
                 VALUES (%s, %s, %s, %s, NOW(), NOW() + make_interval(days => %s))
                 ON CONFLICT (openid) DO UPDATE SET source = EXCLUDED.source,
                   remain_count = CASE
                     WHEN withdrawal_whitelist.remain_count = -1 THEN EXCLUDED.remain_count
                     WHEN EXCLUDED.remain_count = -1 THEN withdrawal_whitelist.remain_count
                     ELSE LEAST(withdrawal_whitelist.remain_count, EXCLUDED.remain_count)
                   END,
                   unionid = COALESCE(NULLIF(EXCLUDED.unionid, ''), withdrawal_whitelist.unionid),
                   expires_at = withdrawal_whitelist.expires_at"""
        cur.execute(sql, (openid, source, remain_count, unionid, _exp_days))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error("[add_whitelist] " + str(e))
        return False


def check_whitelist_today(openid='', unionid=''):
    """当天投诉白名单：source=complaint 且白名单创建于今天（北京时间）"""
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        conds = ["source = 'complaint'"]
        params = []
        if unionid:
            conds.append("unionid = %s")
            params.append(unionid)
        elif openid:
            conds.append("(unionid IS NULL OR unionid = '') AND openid = %s")
            params.append(openid)
        else:
            conn.close()
            return None
        cur.execute("SELECT current_setting('TimeZone') AS db_tz")
        db_tz_row = cur.fetchone()
        db_tz = (db_tz_row.get('db_tz') or 'UTC') if db_tz_row else 'UTC'
        if db_tz in ('UTC', 'Etc/UTC', 'GMT', 'Etc/GMT', 'Universal', 'UCT'):
            conds.append("(created_at + INTERVAL '8 hours')::date = (NOW() + INTERVAL '8 hours')::date")
        else:
            conds.append("created_at::date = CURRENT_DATE")
        cur.execute("SELECT openid, source, remain_count, unionid, created_at FROM withdrawal_whitelist WHERE " + " AND ".join(conds) + " AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1", params)
        row = cur.fetchone()
        conn.close()
        return row
    except Exception as e:
        logger.error("[check_whitelist_today] " + str(e))
        return None


def get_setting_int(key, default=0):
    try:
        return int(float(get_setting(key, default)))
    except Exception:
        return int(default)


def count_user_complaints(phone='', unionid='', openid=''):
    """按 unionid/手机号/openid 统计微信投诉次数（自有投诉不计入黑名单）"""
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        phones = set()
        if phone:
            phones.add(str(phone))
        if unionid:
            cur.execute("SELECT DISTINCT phone FROM phone_openids WHERE unionid=%s AND phone IS NOT NULL AND phone != ''", (unionid,))
            for r in cur.fetchall():
                if r[0]:
                    phones.add(str(r[0]))
        if openid:
            cur.execute("SELECT DISTINCT phone FROM phone_openids WHERE openid=%s AND phone IS NOT NULL AND phone != ''", (openid,))
            for r in cur.fetchall():
                if r[0]:
                    phones.add(str(r[0]))
        conds, params = ["(type = 'wechat' OR complaint_type = 'wechat')"], []
        if phones:
            conds.append("user_phone IN (%s)" % ','.join(['%s'] * len(phones)))
            params.extend(list(phones))
        if openid:
            conds.append("openid = %s")
            params.append(openid)
        if not conds:
            conn.close()
            return 0
        cur.execute("SELECT COUNT(*) FROM complaints WHERE " + ' AND '.join(conds), params)
        cnt = cur.fetchone()[0]
        conn.close()
        return int(cnt)
    except Exception as e:
        logger.error("[count_user_complaints] " + str(e))
        return 0


def count_today_whitelist_uses(phone='', openid=''):
    """统计当天白名单自动退款已用次数（北京时间）"""
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        conds = ["approver = 'whitelist_auto'"]
        params = []
        if phone:
            conds.append("user_phone = %s")
            params.append(str(phone))
        if openid:
            conds.append("openid = %s")
            params.append(openid)
        if not phone and not openid:
            conn.close()
            return 0
        conds.append("(created_at + INTERVAL '8 hours')::date = (NOW() + INTERVAL '8 hours')::date")
        cur.execute("SELECT COUNT(*) FROM withdrawal_records WHERE " + " AND ".join(conds), params)
        cnt = cur.fetchone()[0]
        conn.close()
        return int(cnt)
    except Exception as e:
        logger.error("[count_today_whitelist_uses] " + str(e))
        return 0


def check_use_limits(phone='', unionid='', openid=''):
    """开单前风控：返回 None 可正常使用，否则返回禁止原因"""
    try:
        # 黑名单拦截: 手机号 或 unionid 在黑名单(status=1)则禁止使用
        _bl_conn = get_db()
        _bl = _bl_conn.cursor()
        _bl_phones = set()
        if phone:
            _bl_phones.add(str(phone))
        if unionid:
            _bl.execute("SELECT DISTINCT phone FROM phone_openids WHERE unionid=%s AND phone IS NOT NULL AND phone != ''", (unionid,))
            for _r in _bl.fetchall():
                if _r[0]:
                    _bl_phones.add(str(_r[0]))
        if _bl_phones:
            _ph_marks = ','.join(['%s'] * len(_bl_phones))
            _bl_params = list(_bl_phones) + [unionid or '']
            _bl.execute(
                "SELECT id, status, unban_use_once FROM blacklist WHERE (phone IN (%s) OR (unionid IS NOT NULL AND unionid != '' AND unionid=%%s)) LIMIT 1" % _ph_marks,
                _bl_params)
            _bl_hit = _bl.fetchone()
            if _bl_hit:
                _bl_status = int(_bl_hit['status'] or 0)
                _bl_once = int(_bl_hit.get('unban_use_once') or 0)
                if _bl_status == 1:
                    # 封禁中: 直接拦截
                    _bl_conn.close()
                    return '操作异常，请联系客服4006981080'
                if _bl_status == 0 and _bl_once:
                    # 临时解除一次性: 本次放行, 使用后立即重新封禁
                    _bl.execute("UPDATE blacklist SET status=1, unban_use_once=0 WHERE id=%s", (_bl_hit['id'],))
                    _bl_conn.commit()
                    _bl_conn.close()
                    return None
        _bl_conn.close()
        black = get_setting_int('complaint_blacklist_limit', 3)
        if black > 0 and count_user_complaints(phone, unionid, openid) > black:
            return '累计投诉次数过多，暂不可使用'
        daily = get_setting_int('whitelist_daily_use_limit', 3)
        if daily > 0:
            _wl = check_whitelist_today(openid, unionid)
            if _wl and count_today_whitelist_uses(phone, openid) >= daily:
                return '今日使用次数已达上限'
    except Exception as e:
        logger.error("[check_use_limits] " + str(e))
    return None


def consume_whitelist(openid):
    """消费一次白名单(限次来源扣1,扣完删除; -1不限次不扣; 过期不消费)"""
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE withdrawal_whitelist SET remain_count = remain_count - 1 WHERE openid = %s AND remain_count > 0 AND (expires_at IS NULL OR expires_at > NOW())", (openid,))
        if cur.rowcount > 0:
            cur.execute("DELETE FROM withdrawal_whitelist WHERE openid = %s AND remain_count <= 0", (openid,))
            conn.commit()
            conn.close()
            return True
        cur.execute("SELECT remain_count FROM withdrawal_whitelist WHERE openid = %s AND (expires_at IS NULL OR expires_at > NOW())", (openid,))
        row = cur.fetchone()
        if row and row["remain_count"] == -1:
            conn.commit()
            conn.close()
            return True
        conn.commit()
        conn.close()
        return False
    except Exception as e:
        logger.error("[consume_whitelist] " + str(e))
        return False
def remove_whitelist(openid):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM withdrawal_whitelist WHERE openid = %s", (openid,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error("[remove_whitelist] " + str(e))
        return False
def get_openid_by_phone(phone):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT openid FROM user_balances WHERE phone = %s AND openid IS NOT NULL AND openid != '' ORDER BY id DESC LIMIT 1", (phone,))
        row = cur.fetchone()
        conn.close()
        return row["openid"] if row else None
    except Exception as e:
        logger.error("[get_openid_by_phone] " + str(e))
        return None
def add_whitelist_by_phone(phone, source, remain_count=-1, expire_days=None):
    openid = get_openid_by_phone(phone)
    if not openid:
        logger.warning("[add_whitelist_by_phone] phone=" + str(phone) + " no openid")
        return False
    unionid = _resolve_unionid(openid=openid, phone=phone)
    return add_whitelist(openid, source, remain_count, unionid, expire_days)


# ============ 白名单发放统一入口（2026-08-21 新增） ============
# 规则：按网点 locations.wl_max_uses 发放免审次数(0=不限,默认3)，带有效期
WL_EXPIRE_DAYS_COMPLAINT = 30      # 投诉/客服退款成功拉白: 30天有效
WL_EXPIRE_DAYS_REJECT_RETRY = 30   # 提现被拒重提拉白: 30天有效
WL_EXPIRE_DAYS_MANUAL_HELP = 7     # 后台手动退款附带: 7天有效


def get_location_wl_uses(location_id=None):
    """网点白名单免审次数: 0=不限次数, 默认3"""
    try:
        if not location_id:
            return 3
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT wl_max_uses FROM locations WHERE id=%s", (int(location_id),))
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
            return int(row[0])
        return 3
    except Exception as e:
        logger.error("[get_location_wl_uses] " + str(e))
        return 3


def _grant_whitelist(phone='', openid='', unionid='', location_id=None, source='complaint', days=30):
    """按网点次数+有效期拉白（投诉/客服退款/被拒重提统一走这里）"""
    try:
        uses = get_location_wl_uses(location_id)
        remain = -1 if uses <= 0 else uses
        if openid:
            return add_whitelist(openid, source, remain, unionid or '', expire_days=days)
        if phone:
            return add_whitelist_by_phone(phone, source, remain, expire_days=days)
        return False
    except Exception as e:
        logger.error("[_grant_whitelist] " + str(e))
        return False


def grant_complaint_whitelist(phone='', openid='', unionid='', location_id=None):
    """投诉退款成功后拉白（30天有效, 按网点次数）"""
    return _grant_whitelist(phone, openid, unionid, location_id, 'complaint', WL_EXPIRE_DAYS_COMPLAINT)


def grant_reject_whitelist(phone='', openid='', unionid='', location_id=None):
    """提现被拒重提拉白（30天有效, 按网点次数）"""
    return _grant_whitelist(phone, openid, unionid, location_id, 'reject_retry', WL_EXPIRE_DAYS_REJECT_RETRY)


def get_online_device_ids():
    """从ws_proxy获取当前在线设备ID列表"""
    try:
        import urllib.request, json
        resp = urllib.request.urlopen("http://127.0.0.1:5004/api/devices/online", timeout=2)
        data = json.loads(resp.read())
        return set(data.get("devices", []))
    except Exception as e:
        logger.error("[get_online_device_ids] %s", str(e))
        return set()


def is_heartbeat_online(heartbeat, timeout_seconds=120):
    """Unified online check by last heartbeat (120s default)."""
    if not heartbeat:
        return False
    try:
        if isinstance(heartbeat, str):
            heartbeat = datetime.strptime(str(heartbeat)[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - heartbeat).total_seconds() < timeout_seconds
    except Exception:
        return False


def is_device_online(device_id, heartbeat=None):
    """设备在线判断(2026-08-28彻底版):
    1. 先查 WS 连接/5004在线列表(实时最准, 设备WS连着=真在线, 可远程开门)
    2. 不在WS列表才回退看心跳(120秒内算在线)
    3. 5004查询失败时容错: 不直接判离线, 回退看心跳
    """
    device_id = str(device_id)
    # 优先: 本进程内存中的 WS 连接表
    try:
        if device_id in connected_devices:
            return True
    except Exception:
        pass
    # 5004 WS 在线列表(实时): 查询失败不算离线, 回退心跳
    _ws_ok = False
    try:
        if device_id in get_online_device_ids():
            return True
        _ws_ok = True
    except Exception:
        _ws_ok = False
    # 回退: 心跳120秒内
    if is_heartbeat_online(heartbeat):
        return True
    # 5004查询正常且设备不在列表+心跳过期 => 真离线
    if _ws_ok:
        return False
    # 5004查询失败: 无法确认, 看心跳(已过期) => 保守判在线, 避免误拦用户
    return True


# ============================================
# PushPlus 推送 & 商户号健康检查
# ============================================

# 商户号异常的错误码
_MERCHANT_ERROR_CODES = {'SIGN_ERROR', 'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'SYSTEMERROR', 'FREQUENCY_LIMITED'}  # NO_AUTH removed
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0


def get_mid_retrieve_config(cursor, cabinet_id):
    """Return effective mid-retrieve allow flag and count limit for a cabinet."""
    cursor.execute("""
        SELECT l.allow_mid_retrieve AS allow_mid_retrieve,
               l.mid_retrieve_limit AS location_limit,
               c.mid_retrieve_limit AS cabinet_limit
        FROM cabinets c
        LEFT JOIN locations l ON c.location_id = l.id
        WHERE c.id = %s
    """, (cabinet_id,))
    row = cursor.fetchone()
    if not row:
        return {'allow_mid_retrieve': 1, 'mid_retrieve_limit': None}
    allow = 1 if row.get('allow_mid_retrieve') else 0
    limit = row.get('cabinet_limit')
    if limit is None:
        limit = row.get('location_limit')
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = None
    return {'allow_mid_retrieve': allow, 'mid_retrieve_limit': limit}


def get_order_mid_retrieve_info(cursor, order):
    """Return count/limit/remaining for an active order."""
    cfg = get_mid_retrieve_config(cursor, order.get('cabinet_id'))
    count = order.get('mid_retrieve_count') or 0
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    limit = cfg['mid_retrieve_limit']
    remaining = None
    if limit is not None:
        remaining = max(0, int(limit) - count)
    return {
        'allow_mid_retrieve': cfg['allow_mid_retrieve'],
        'mid_retrieve_count': count,
        'mid_retrieve_limit': limit,
        'mid_retrieve_remaining': remaining,
    }


def try_increment_mid_retrieve(cursor, order_id, cabinet_id):
    """Atomically reserve one mid-retrieve count if the order is still eligible."""
    cfg = get_mid_retrieve_config(cursor, cabinet_id)
    if not cfg['allow_mid_retrieve']:
        return {
            'allowed': False,
            'reason': 'disabled',
            'count': None,
            'limit': cfg['mid_retrieve_limit'],
            'remaining': 0,
            'config': cfg,
        }
    cursor.execute("""
        UPDATE orders o
        SET mid_retrieve_count = o.mid_retrieve_count + 1
        FROM cabinets c
        LEFT JOIN locations l ON c.location_id = l.id
        WHERE o.id = %s
          AND o.status = 2
          AND c.id = o.cabinet_id
          AND (
            COALESCE(c.mid_retrieve_limit, l.mid_retrieve_limit) IS NULL
            OR o.mid_retrieve_count < COALESCE(c.mid_retrieve_limit, l.mid_retrieve_limit)
          )
        RETURNING o.mid_retrieve_count,
                  COALESCE(c.mid_retrieve_limit, l.mid_retrieve_limit) AS mid_retrieve_limit
    """, (order_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute('SELECT mid_retrieve_count FROM orders WHERE id = %s', (order_id,))
        count_row = cursor.fetchone()
        count = int(count_row['mid_retrieve_count']) if count_row and count_row.get('mid_retrieve_count') is not None else 0
        limit = cfg['mid_retrieve_limit']
        remaining = max(0, int(limit) - count) if limit is not None else 0
        return {
            'allowed': False,
            'reason': 'limit',
            'count': count,
            'limit': limit,
            'remaining': remaining,
            'config': cfg,
        }
    limit = row.get('mid_retrieve_limit')
    count = int(row['mid_retrieve_count'])
    remaining = max(0, int(limit) - count) if limit is not None else None
    return {
        'allowed': True,
        'reason': 'ok',
        'count': count,
        'limit': limit,
        'remaining': remaining,
        'config': cfg,
    }


def send_wx_subscribe_message(openid, template_id, data, page='', phone=None, unionid=None):
    """发送微信订阅消息（仅支持小程序mp_openid）"""
    try:
        import requests
        import config
        from database import get_db

        # 如果提供了手机号，查openid（先user_balances.openid，再phone_openids.mp_openid）
        if not openid and phone:
            try:
                _conn = get_db()
                _cur = _conn.cursor()
                # [FIX-20260716] 必须查 mp_openid（小程序openid），禁止查 openid（可能是公众号openid会导致40003）
                # ???? oLhbm2 ??????openid????? ooTcRx ??????openid
                _ub_row = find_user_balance_row(_cur, phone=phone, unionid=unionid or '')
                if _ub_row and _ub_row.get('mp_openid') and _ub_row['mp_openid'] not in ('', None) and not _ub_row['mp_openid'].startswith('oLhbm2'):
                    openid = _ub_row['mp_openid']
                if not openid:
                    _po_rows = phone_openid_rows(_cur, phone=phone, unionid=unionid or '')
                    if len(_po_rows) == 1 and _po_rows[0].get('mp_openid') and not _po_rows[0]['mp_openid'].startswith('oLhbm2'):
                        openid = _po_rows[0]['mp_openid']
                    elif len(_po_rows) > 1 and not unionid:
                        logger.warning(f'[subscribe_msg] 手机号绑定多个微信，缺少unionid，不猜测: phone={phone}')

                if not openid:
                    _cur.execute("""
                        SELECT mp_openid, unionid FROM phone_openids
                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL
                          AND mp_openid NOT LIKE 'oLhbm2%%'
                        ORDER BY id ASC
                    """, (phone,))
                    _po2 = _cur.fetchall()
                    if _po2:
                        _uniqs = {r['unionid'] for r in _po2 if r['unionid']}
                        if unionid and any(r['unionid'] == unionid for r in _po2):
                            _po2 = [r for r in _po2 if r['unionid'] == unionid]
                        elif len(_uniqs) == 1:
                            _po2 = _po2[:1]
                        if len(_po2) == 1 and _po2[0].get('mp_openid'):
                            openid = _po2[0]['mp_openid']
                _conn.close()
            except Exception as _e:
                logger.warning(f'[subscribe_msg] 查询phone_openids失败: {_e}')

        # 保险：公众号openid不能发小程序订阅消息，按手机号反查正确小程序openid
                # ★ 若 openid 还是旧小程序(科莱智 oWrA8 前缀) 或 公众号(oLhbm2)，换到同 unionid 的新小程序(伧置 ooTcRx) openid；
        #   换不到（说明该用户还未用新小程序登录/授权）则跳过，避免微信返回 40003 invalid openid。
        if openid and phone and not openid.startswith('ooTcRx'):
            try:
                _conn4 = get_db()
                _cur4 = _conn4.cursor()
                _r4 = None
                _ub4 = find_user_balance_row(_cur4, phone=phone, unionid=unionid or '')
                if _ub4 and _ub4.get('mp_openid') and str(_ub4['mp_openid']).startswith('ooTcRx'):
                    _r4 = (_ub4['mp_openid'],)
                if not _r4:
                    _po4 = phone_openid_rows(_cur4, phone=phone, unionid=unionid or '')
                    for _rr4 in _po4:
                        if _rr4.get('mp_openid') and str(_rr4['mp_openid']).startswith('ooTcRx'):
                            _r4 = (_rr4['mp_openid'],)
                            break
                if not _r4:
                    _cur4.execute("""
                        SELECT mp_openid FROM phone_openids
                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE 'ooTcRx%%'
                        ORDER BY id ASC LIMIT 1
                    """, (phone,))
                    _rr = _cur4.fetchone()
                    if _rr and _rr.get('mp_openid'):
                        _r4 = (_rr['mp_openid'],)
                if not _r4 and unionid:
                    # [FIX-20260912] 再按 unionid 跨手机号找一遍(关键补充)。
                    #   起因: 同一个微信号(unionid)的新小程序 openid 会被记在它的"首个手机号"那一行,
                    #   而消息是按用户当前手机号发的 -> 只按手机号查会查不到, 消息被白白跳过。
                    #   实例: 测试号 18888889999(unionid oTGtk2e5...) 的新 openid 记在 18867642312 那一行。
                    _cur4.execute("""
                        SELECT mp_openid FROM phone_openids
                        WHERE unionid = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE 'ooTcRx%%'
                        ORDER BY id ASC LIMIT 1
                    """, (unionid,))
                    _rru = _cur4.fetchone()
                    if _rru and _rru.get('mp_openid'):
                        _r4 = (_rru['mp_openid'],)
                        logger.info('[subscribe_msg] 按unionid跨手机号找到新openid: phone=%s', phone)
                _conn4.close()
                if _r4 and _r4[0]:
                    openid = _r4[0]
            except Exception as _e4:
                logger.warning(f'[subscribe_msg] 旧openid换新失败: {_e4}')
        # 保险：公众号openid不能发小程序订阅消息，按手机号反查正确小程序openid
        if openid and openid.startswith('oLhbm2') and phone:
            try:
                _conn3 = get_db()
                _cur3 = _conn3.cursor()
                _r3 = None
                _ub3 = find_user_balance_row(_cur3, phone=phone, unionid=unionid or '')
                if _ub3 and _ub3.get('mp_openid') and _ub3['mp_openid'].startswith('ooTcRx'):
                    _r3 = (_ub3['mp_openid'],)
                if not _r3:
                    _po3 = phone_openid_rows(_cur3, phone=phone, unionid=unionid or '')
                    if len(_po3) == 1 and _po3[0].get('mp_openid') and _po3[0]['mp_openid'].startswith('ooTcRx'):
                        _r3 = (_po3[0]['mp_openid'],)
                _conn3.close()
                if _r3 and _r3[0]:
                    openid = _r3[0]
            except Exception as _e3:
                logger.warning(f'[subscribe_msg] 纠正openid失败: {_e3}')
        if openid and not openid.startswith('ooTcRx'):
            logger.warning(f'[subscribe_msg] 跳过公众号openid: openid={openid[:8]}..., phone={phone}')
            return False
        if not openid:
            logger.warning(f'[subscribe_msg] mp_openid为空，跳过发送（phone={phone}）')
            return False

        # 获取access_token（使用getStableAccessToken + DB缓存）
        access_token = get_access_token()
        if not access_token:
            logger.error('[subscribe_msg] 获取access_token失败')
            return False

        # 发送订阅消息
        send_url = f'https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={access_token}'
        payload = {
            'touser': openid,
            'template_id': template_id,
            'data': data
        }
        if page:
            payload['page'] = page

        resp = requests.post(send_url, json=payload, timeout=5)
        result = resp.json()

        if result.get('errcode') == 0:
            logger.info(f'[subscribe_msg] 发送成功: openid={openid[:8]}..., template={template_id}')
            return True
        else:
            logger.error(f'[subscribe_msg] 发送失败: openid={openid[:8]}..., phone={phone}, template={template_id}, result={result}')
            return False
    except Exception as e:
        logger.error(f'[subscribe_msg] 异常: {e}')
        return False


# ============================================
# PushPlus 推送 & 商户号健康检查
# ============================================

# 商户号异常的错误码
_MERCHANT_ERROR_CODES = {'SIGN_ERROR', 'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'SYSTEMERROR', 'FREQUENCY_LIMITED'}  # NO_AUTH removed
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0


def calc_balance(user_id=None, phone=None, openid=None, mp_openid=None, unionid=None):
    """按订单金额实时计算可用余额：订单保证金 - 已退款 - 已提现 - 待提现（每订单封顶）"""
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    try:
        ident = resolve_user_identity(c, openid=openid or '', mp_openid=mp_openid or '', phone=phone or '', unionid=unionid or '', user_id=user_id or 0)
        if ident['ambiguous'] or not ident['user_id'] and not ident['unionid'] and not ident['mp_openid'] and not phone:
            return 0.0
        if ident['user_id'] == 0:
            return 0.0
        # 身份条件：先按 user_id（unionid 换出的户口本编号），没有才退 unionid/mp_openid/openid/手机号
        cond = []
        params = []
        if ident['user_id']:
            cond.append('o.user_id = %s')
            params.append(ident['user_id'])
        elif ident['unionid']:
            cond.append('o.unionid = %s')
            params.append(ident['unionid'])
        elif ident['mp_openid']:
            cond.append('o.mp_openid = %s')
            params.append(ident['mp_openid'])
        elif openid:
            cond.append('o.openid = %s')
            params.append(openid)
        elif phone:
            cond.append('o.user_phone = %s')
            params.append(phone)
        where = ' OR '.join(cond)
        sql = (
            "SELECT COALESCE(SUM(bd.amount), 0) FROM user_balance_details bd "
            "JOIN orders o ON bd.order_id = o.id "
            "WHERE bd.status = 'available' AND o.status = 3 AND (" + where + ") "
            "AND NOT EXISTS (SELECT 1 FROM withdrawal_records w WHERE w.order_id = o.id AND w.status IN (0, 1, 2))"
        )
        c.execute(sql, params)
        r = c.fetchone()
        return max(0.0, float(r[0] or 0))
    finally:
        try: conn.close()
        except: pass


def send_smsbao(phone, fee=0, amount=0, app_name=''):
    """短信宝发送短信 (S106): 结束订单退押金通知
    返回 (success, msg)
    """
    try:
        from config import (SMSBAO_USERNAME, SMSBAO_APIKEY, SMSBAO_SIGN,
                            SMSBAO_TEMPLATE, SMSBAO_APP_NAME)
        import hashlib, requests, urllib.parse
        # 填充模板变量
        content = SMSBAO_TEMPLATE.format(
            fee=str(fee or 0),
            amount=('%g' % float(amount or 0)),
            appName=app_name or SMSBAO_APP_NAME,
        )
        full = SMSBAO_SIGN + content
        # S106: 按短信宝万能接口格式, p=APIKey明文(不用MD5)
        url = 'https://api.smsbao.com/sms?' + urllib.parse.urlencode({
            'u': SMSBAO_USERNAME, 'p': SMSBAO_APIKEY, 'm': phone, 'c': full
        })
        resp = requests.get(url, timeout=10)
        code = resp.text.strip()
        if code == '0':
            logger.info('[smsbao] 发送成功 phone=%s', phone)
            return True, 'ok'
        logger.warning('[smsbao] 发送失败 phone=%s code=%s msg=%s', phone, code, full[:50])
        return False, code
    except Exception as e:
        logger.error('[smsbao] 发送异常 phone=%s: %s', phone, e)
        return False, str(e)


def send_yunpian(phone, fee=0, amount=0):
    """云片发送短信 (S116): 指定模板6449738, 变量fee/amount
    返回 (success, msg)
    """
    try:
        from config import YP_API_KEY, YP_TPL_ID
        import requests as _req
        import urllib.parse as _up
        # 模板: 【重庆科莱维科技有限公司】...本次寄存费#fee#元，您的预付款#amount#元已退款...
        tpl_value = _up.urlencode({'#fee#': ('%g' % float(fee or 0)), '#amount#': ('%g' % float(amount or 0))})
        form = _up.urlencode({
            'apikey': YP_API_KEY,
            'mobile': phone,
            'tpl_id': str(YP_TPL_ID),
            'tpl_value': tpl_value,
        })
        resp = _req.post('https://sms.yunpian.com/v2/sms/tpl_single_send.json',
                         data=form, headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}, timeout=10)
        result = resp.json()
        code = result.get('code')
        if code == 0:
            logger.info('[yunpian] 发送成功 phone=%s sid=%s', phone, result.get('sid'))
            return True, 'ok'
        logger.warning('[yunpian] 发送失败 phone=%s code=%s msg=%s', phone, code, result.get('msg'))
        return False, str(result.get('msg'))
    except Exception as e:
        logger.error('[yunpian] 发送异常 phone=%s: %s', phone, e)
        return False, str(e)


def send_smsbao_smart(phone, fee=0, amount=0):
    """统一发送入口 (S116): 根据SMS_PROVIDER选云片/短信宝
    默认云片(模板已过,可自动发); 短信宝小程序引流词要人工触发留作备用
    """
    try:
        from config import SMS_PROVIDER
    except Exception:
        SMS_PROVIDER = 'yunpian'
    if SMS_PROVIDER == 'smsbao':
        return send_smsbao(phone, fee=fee, amount=amount)
    return send_yunpian(phone, fee=fee, amount=amount)
