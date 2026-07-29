"""
鏅鸿兘瀵勫瓨鏌滅郴缁?- 鍏变韩杈呭姪鍑芥暟涓庡叏灞€鐘舵€?
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
# 鍏ㄥ眬鍏变韩鐘舵€?
# ============================================
connected_devices = {}         # WebSocket 宸茶繛鎺ヨ澶?{device_id: sid}
pending_lock_commands = {}

# 闀胯疆璇俊鍙? 姣忎釜device_id涓€涓狤vent锛屾湁鏂版寚浠ゆ椂set()
import threading as _th
_pending_cmd_events = {}
_pending_cmd_events_lock = _th.Lock()

def signal_pending_command(device_id):
    """閫氱煡绛夊緟涓殑闀胯疆璇㈣姹傦細鏈夋柊鎸囦护浜?""
    with _pending_cmd_events_lock:
        evt = _pending_cmd_events.get(device_id)
        if evt:
            evt.set()

def get_pending_event(device_id):
    """鑾峰彇(鎴栧垱寤?鎸囧畾璁惧鐨勭瓑寰呬簨浠?""
    with _pending_cmd_events_lock:
        if device_id not in _pending_cmd_events:
            _pending_cmd_events[device_id] = _th.Event()
        return _pending_cmd_events[device_id]

def clear_pending_event(device_id):
    """娓呴櫎浜嬩欢鐘舵€?鍦ㄥ紑濮嬬瓑寰呭墠璋冪敤)"""
    with _pending_cmd_events_lock:
        evt = _pending_cmd_events.get(device_id)
        if evt:
            evt.clear()     # 绂荤嚎寮€閿佹寚浠ら槦鍒?{device_id: [commands]}

# ============================================
# 鍝嶅簲鏍煎紡
# ============================================

def _get_device_protocol(device_id):
    """浠巆abinets琛╩ainboard_source璇诲彇璁惧鍗忚绫诲瀷锛岄粯璁BM"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT mainboard_source FROM cabinets WHERE mainboard_device_id=%s', (str(device_id),))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logger.error(f'[鍗忚鏌ヨ] 澶辫触: {e}')
    return 'YBM'



def _format_datetimes(obj):
    """Recursively convert datetime objects to YYYY-MM-DD HH:MM:SS strings"""
    if isinstance(obj, dict):
        return {k: _format_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_format_datetimes(item) for item in obj]
    elif hasattr(obj, 'strftime') and hasattr(obj, 'hour'):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    return obj


def json_response(data=None, message='success', code=200, headers=None):
    """缁熶竴JSON鍝嶅簲鏍煎紡"""
    resp = jsonify({'code': code, 'message': message, 'data': _format_datetimes(data)})
    resp.status_code = code
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    return resp


# ============================================
# 绯荤粺璁剧疆
# ============================================
def get_setting(key, default=None):
    """鑾峰彇绯荤粺璁剧疆"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT setting_value FROM system_settings WHERE setting_key = %s', (key,))
    result = cursor.fetchone()
    conn.close()
    return result['setting_value'] if result else default


def set_setting(key, value):
    """璁剧疆绯荤粺閰嶇疆"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)', (key, str(value)))
    conn.commit()
    conn.close()


# ============================================
# 鏀粯妯″紡
# ============================================
def is_mock_mode():
    """妫€鏌ユ槸鍚︿负妯℃嫙鏀粯妯″紡"""
    return get_setting('pay_mode', 'mock') == 'mock'


# ============================================
# 娴忚鍣ㄦ娴?
# ============================================
def is_wechat_browser():
    """妫€鏌ユ槸鍚﹀湪寰俊娴忚鍣ㄤ腑"""
    from flask import request
    user_agent = request.headers.get('User-Agent', '')
    return 'MicroMessenger' in user_agent


def is_mobile_browser():
    """妫€鏌ユ槸鍚﹀湪绉诲姩绔祻瑙堝櫒涓?""
    from flask import request
    user_agent = request.headers.get('User-Agent', '')
    mobile_keywords = ['Mobile', 'Android', 'iPhone', 'iPad', 'iPod', 'Windows Phone']
    return any(keyword in user_agent for keyword in mobile_keywords)


# ============================================
# 鏉冮檺楠岃瘉瑁呴グ鍣?
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
    """绠＄悊鍛樻潈闄愰獙璇?- 鍚屾椂鏀寔session cookie鍜孊earer token"""
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
        return json_response(message='鏈櫥褰曪紝璇峰厛鐧诲綍', code=401)
    return decorated


def require_merchant_auth(f):
    """鍟嗗/浠ｇ悊鍟嗘潈闄愰獙璇?- 鍚屾椂鏀寔session cookie鍜孊earer token"""
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
                                emp = cursor.execute('SELECT e.id, e.merchant_id, e.name, e.permissions, m.name as merchant_name FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id WHERE e.id=%s', (uid,)).fetchone()
                                if emp:
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
                        row = cursor.execute("SELECT e.id, e.merchant_id, e.name, e.permissions, m.name as merchant_name FROM employees e LEFT JOIN merchants m ON e.merchant_id = m.id WHERE e.auth_token=%s", (token,)).fetchone()
                        if row:
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
        return json_response(message='鏈櫥褰曪紝璇峰厛鐧诲綍', code=401)
    return decorated


def require_agent_auth(f):
    """浠ｇ悊鍟嗘潈闄愰獙璇?""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'agent_id' not in session:
            return json_response(message='鏈櫥褰曪紝璇峰厛鐧诲綍', code=401)
        return f(*args, **kwargs)
    return decorated


def require_employee_auth(f):
    """鍛樺伐鏉冮檺楠岃瘉"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'employee_id' not in session:
            return json_response(message='鏈櫥褰曪紝璇峰厛鐧诲綍', code=401)
        return f(*args, **kwargs)
    return decorated


# ============================================
# 璁㈠崟闅愯棌閫昏緫
# ============================================
def should_hide_order(merchant_id, order_id, phone, hide_rate, whitelist, logic_mark=None, total_orders=0):
    """鍒ゆ柇璁㈠崟鏄惁搴斿鍟嗗闅愯棌锛堢‘瀹氭€у搱甯岋級
    logic_mark: 'N'=鎵嬪姩鎭㈠(涓嶉殣钘?, 'Y'=鎵嬪姩闅愯棌, None=鎸塰ash璁＄畻
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


def filter_duplicate_users(orders, days, limit):
    """杩囨护楂橀鐢ㄦ埛鐨勮鍗?""
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
# WebSocket 寮€閿佹寚浠?
# ============================================
def send_open_lock(device_id, board_no, lock_no, protocol=None, order_id='', slot_number=None, slot_label=None):
    """
    鍙戦€佸紑閿佹寚浠?- 鏀寔鍘熷WebSocket + Socket.IO + HTTP杞鍏滃簳
    """
    # 闃查噸锛氬悓涓€ order_id 5绉掑唴涓嶉噸澶嶅彂閫?
    _now = time.time()
    if order_id and order_id in _last_open_lock_time:
        if _now - _last_open_lock_time[order_id] < 5:
            logger.info(f'[SEND_LOCK] 闃查噸璺宠繃: order_id={order_id}, last_sent={_now - _last_open_lock_time[order_id]:.1f}s ago')
            return True
    if order_id:
        _last_open_lock_time[order_id] = _now
    # 鑷姩浠庢暟鎹簱瑙ｆ瀽鍗忚绫诲瀷
    if protocol is None:
        protocol = _get_device_protocol(device_id)
    logger.info(f'[SEND_LOCK] device={device_id}, protocol={protocol}, id(pending)={id(pending_lock_commands)}, keys_before={list(pending_lock_commands.keys())}')
    command = {
        'type': 'open_lock',
        'device_id': device_id,
        'deviceId': device_id,
        'board_no': board_no,
        'boardNo': board_no,
        'lock_no': lock_no,
        'lockNo': lock_no,
        'protocol': protocol,
        'order_id': order_id,
        'orderId': str(order_id) if order_id else '',
        'slot_number': slot_number or 0,
        'slot_label': slot_label or '',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    # 鍏堝彂WebSocket锛堜笉渚濊禆DB锛屽嵆浣緿B閿佷綇涔熻兘绉掑紑锛?
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
    # 灏濊瘯鐙珛WebSocket鏈嶅姟(璁惧杩炴帴鐙珛WS鏃朵娇鐢?
    if not _ws_sent:
        try:
            import urllib.request as _req, json as _json
            _body = _json.dumps({"device_id": device_id, "command": command}).encode()
            _r = _req.urlopen("http://127.0.0.1:5004/send", data=_body, timeout=2)
            if _json.loads(_r.read()).get("success"):
                _ws_sent = True
                logger.info(f"[WS-DAEMON] open_lock sent via daemon: device={device_id}, board={board_no}, lock={lock_no}")
        except Exception:
            pass

    
    # 鍐呭瓨闃熷垪鍏滃簳锛堜粎鍦╓S鍙戦€佸け璐ユ椂浣跨敤锛?
    if not _ws_sent:
        if device_id not in pending_lock_commands:
            pending_lock_commands[device_id] = []
        if command not in pending_lock_commands[device_id]:
            pending_lock_commands[device_id].append(command)
    
    # 鏁版嵁搴撴搷浣滐細濮嬬粓delivered=0锛岃HTTP杞浣滀负鍙潬鍏滃簳锛圵S鍙兘鍙戦€佹垚鍔熶絾璁惧鏈敹鍒帮級
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
        # 鏃犺WS鏄惁鍙戦€佹垚鍔燂紝閮介€氱煡璁惧鏉ヨ疆璇紙WS鍙兘涓㈠寘锛?
        signal_pending_command(device_id)
    except Exception as _e:
        logger.error(f"[DB] 瀛樺偍pending_lock澶辫触: {_e}")
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
        logger.error(f"[DB] 瀛樺偍door_record澶辫触: {_e3}")
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
    try:
        import urllib.request as _req
        import json as _json
        _body = _json.dumps({'device_id': device_id, 'command': command}).encode()
        _req.urlopen('http://127.0.0.1:5004/send', data=_body, timeout=3)
    except Exception as e:
        logger.error(f'[send_open_all] {e}')

    import threading as _th, urllib.request as _req, json as _json
    try:
        _body = _json.dumps({"device_id": device_id, "command": command}).encode()
        _th.Thread(target=lambda: _req.urlopen("http://127.0.0.1:5004/send", data=_body, timeout=5), daemon=True).start()
        logger.info("[WS-DAEMON] open_all sent via daemon: " + str(device_id))
    except Exception as _e:
        logger.warning("[WS-DAEMON] open_all send failed: " + str(_e))

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


# ============================================
# 鏀粯鐩稿叧 - 寤惰繜瀵煎叆閬垮厤寰幆
# ============================================
def _get_payment_channel(channel_id=None, exclude_channel_id=None):
    """鑾峰彇鏀粯娓犻亾锛堟敮鎸佷弗鏍艰疆杞拰鍔犳潈闅忔満锛?""
    conn = get_db()
    cursor = conn.cursor()
    if channel_id:
        cursor.execute('SELECT * FROM payment_channels WHERE id = %s', (channel_id,))
        ch = cursor.fetchone()
        conn.close()
        return dict(ch) if ch else None
    cursor.execute('SELECT * FROM payment_channels WHERE is_active = 1 AND (auto_disabled IS NULL OR auto_disabled = 0)')
    channels = cursor.fetchall()
    if not channels:
        conn.close()
        return None
    # 濡傛灉鏈夋帓闄ょ殑娓犻亾锛岃繃婊ゆ帀
    if exclude_channel_id:
        channels = [ch for ch in channels if ch['id'] != exclude_channel_id]
        if not channels:
            conn.close()
            return None
    # 璇诲彇杞浆妯″紡
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
        # 鐪熻疆璇細閫塴ast_used_at鏈€鏃╃殑锛屼繚璇佹瘡涓晢鎴蜂緷娆′娇鐢?
        from datetime import datetime as _dt; selected = min(channels, key=lambda ch: ch['last_used_at'] or _dt(1970,1,1))
        logger.info(f"[娓犻亾杞浆-杞浆妯″紡] 閫変腑: {selected['name']} (id={selected['id']}, last_used={selected['last_used_at']})")
    else:
        # 鍔犳潈闅忔満
        weights = []
        for ch in channels:
            base_weight = ch['weight'] or 1
            inverse_factor = 1.0 / (1 + (ch['total_amount'] or 0) / 1000)
            weights.append(base_weight * inverse_factor)
        selected = random.choices(list(channels), weights=weights, k=1)[0]
        logger.info(f"[娓犻亾杞浆-闅忔満妯″紡] 閫変腑: {selected['name']} (id={selected['id']})")
    conn.close()
    return dict(selected)


def select_payment_channel(exclude_channel_id=None):
    """閫夋嫨鏀粯娓犻亾锛堝姞鏉冮殢鏈鸿疆鎹級
    exclude_channel_id: 鎺掗櫎鐨勬笭閬揑D锛岀敤浜庢晠闅滃垏鎹㈡椂璺宠繃褰撳墠澶辫触鐨勬笭閬?
    """
    return _get_payment_channel(exclude_channel_id=exclude_channel_id)


def update_channel_stats(channel_id, amount):
    """鏇存柊娓犻亾缁熻"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE payment_channels SET total_amount = total_amount + %s, total_count = total_count + 1, last_used_at = %s WHERE id = %s',
                       (amount, datetime.now(), channel_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[娓犻亾缁熻] 鏇存柊澶辫触: {e}")


def get_channel_wxpay(channel, use_mp_appid=False):
    """鏍规嵁娓犻亾閰嶇疆鍒涘缓鏀粯瀹炰緥"""
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
    """鑾峰彇榛樿寰俊鏀粯瀹炰緥"""
    from wxpay import WxPay, MockWxPay
    mode = get_setting('pay_mode', 'mock')
    if mode == 'mock':
        return MockWxPay()
    app_id = WX_MP_APP_ID if use_mp_appid else WX_APP_ID
    return WxPay(mch_id=WX_MCH_ID, api_key=WX_API_KEY, app_id=app_id,
                 cert_path=WX_CERT_PATH, key_path=WX_KEY_PATH)


def get_payment_params(order_id, order_no, deposit_amount, user_phone=None, openid=None,
                       payment_channel=None, payment_channel_id=None, _retry_count=0):
    """鑾峰彇寰俊鏀粯鍙傛暟"""
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
                scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '鏅鸿兘瀵勫瓨鏌?})
        else:
            scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '鏅鸿兘瀵勫瓨鏌?})
    else:
        scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '鏅鸿兘瀵勫瓨鏌?})

    if openid:
        trade_type = 'JSAPI'
    # 浣跨敤鏀粯娓犻亾
    if payment_channel_id:
        ch = _get_payment_channel(payment_channel_id)
        current_channel = ch or payment_channel
    elif payment_channel:
        current_channel = payment_channel
    else:
        current_channel = _get_payment_channel()  # 鑷姩閫夋椿璺冩笭閬擄紝閬垮厤fallback鍒扮‖缂栫爜榛樿鍟嗘埛

    if current_channel:
        wxpay, ch_type = get_channel_wxpay(current_channel, use_mp_appid=False)
        if ch_type == 'third_party' and wxpay:
            third_party_type = 'alipay' if not is_wechat_browser() else 'wechat'
            result = wxpay.unifiedorder(trade_type=third_party_type, body='鑻ユ娂閲戞湭閫€鍥烇紝璇锋嫧鎵撳鏈嶇數璇?00-698-1080',
                                         total_fee=int(deposit_amount * 100), out_trade_no=order_no)
            if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
                # 鏇存柊娓犻亾缁熻锛堢敤浜庤疆杞級
                if current_channel:
                    update_channel_stats(current_channel['id'], deposit_amount)
                return {'mode': 'third_party', 'channel_type': third_party_type, 'order_id': order_id,
                        'order_no': order_no, 'pay_url': result.get('url', ''), 'url_qrcode': result.get('url_qrcode', '')}
            return {'mode': 'error', 'error_msg': result.get('return_msg', '绗笁鏂逛笅鍗曞け璐?)}
        if wxpay is None:
            return {'mode': 'error', 'error_msg': '鏀粯娓犻亾閰嶇疆寮傚父'}
    else:
        return {'mode': 'error', 'error_msg': '鏃犲彲鐢ㄦ椿璺冨晢鎴凤紝璇疯仈绯荤鐞嗗憳'}

    total_fee = int(deposit_amount * 100)
    time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')

    result = wxpay.unifiedorder(trade_type=trade_type, body='鑻ユ娂閲戞湭閫€鍥烇紝璇锋嫧鎵撳鏈嶇數璇?00-698-1080',
                                 total_fee=total_fee, out_trade_no=order_no,
                                 notify_url=WX_PAY_NOTIFY_URL, openid=openid,
                                 scene_info=scene_info, time_expire=time_expire)

    if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
        # 鏇存柊璁㈠崟鐨勫疄闄呮敮浠樻笭閬擄紙闃叉杞浆瀵艰嚧涓嶄竴鑷达級
        try:
            from database import get_db as _gdb3
            _db3 = _gdb3()
            _db3.execute("UPDATE orders SET payment_channel_id=%s WHERE id=%s", (current_channel["id"], order_id))
            _db3.commit()
            _db3.close()
        except Exception as _e:
            logger.error(f"[鏀粯娓犻亾鏇存柊] 澶辫触: {_e}")
        # 鏇存柊娓犻亾缁熻
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
    
    # 鍟嗘埛琚皝/寮傚父鑷姩妫€娴?
    _dead_errors = {'MCH_NOT_EXIST', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}
    _skip_errors = {'NOAUTH', 'NO_AUTH'}  # 鏀舵鍙楅檺锛屽垏鎹㈤噸璇曚絾涓嶆案涔呯鐢?
    _err_code = result.get('err_code', '')
    if current_channel and _retry_count < 3 and (_err_code in _dead_errors or _err_code in _skip_errors):
        # 鍙涓ラ噸閿欒绂佺敤鍟嗘埛锛汵OAUTH绛夋敹娆惧彈闄愬彧鍒囨崲涓嶇鐢?
        if _err_code in _dead_errors:
            try:
                from database import get_db as _gdb2
                _db2 = _gdb2()
                _db2.execute('UPDATE payment_channels SET is_active=0 WHERE id=%s', (current_channel['id'],))
                _db2.commit()
                _db2.close()
                logger.warning(f'[娓犻亾] 鍟嗘埛寮傚父宸茶嚜鍔ㄧ鐢? id={current_channel["id"]}, name={current_channel.get("name","")}, err={result.get("err_code")}')
            except Exception as _e:
                logger.error(f'[娓犻亾] 鑷姩绂佺敤澶辫触: {_e}')
        else:
            logger.warning(f'[娓犻亾] 鍟嗘埛鏀舵鍙楅檺(涓嶇鐢?锛屽垏鎹㈤噸璇? id={current_channel["id"]}, err={result.get("err_code")}')
        next_ch = select_payment_channel(exclude_channel_id=current_channel['id'])
        if next_ch and next_ch.get('id') and next_ch['id'] != current_channel['id']:
            logger.info(f'[娓犻亾] 鍒囨崲鍒颁笅涓€涓笭閬撻噸璇? {next_ch["name"]}')
            # [宸蹭慨澶峕 涓嶅啀淇敼璁㈠崟鐨刾ayment_channel_id锛岃鐢ㄦ埛閲嶆柊鎵爜
            # 鍘熷洜锛氱敤鎴锋壂鐮佹椂鏄晢鎴稟锛屽鏋滅郴缁熷伔鍋锋崲鎴愬晢鎴稡锛屾敮浠樺洖璋冩椂浼氭壘涓嶅埌璁㈠崟
            logger.warning(f'[娓犻亾] 鍟嗘埛寮傚父锛岄渶瑕佺敤鎴烽噸鏂版壂鐮併€備笉淇敼璁㈠崟#{order_id}鐨刾ayment_channel_id')
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
    return {'mode': 'error', 'error_msg': '浜ゆ槗澶辫触锛岃閲嶆柊鏀粯'}


def process_auto_refund(order, cursor, conn):
    """鑷姩閫€娆撅紙闃叉祴璇曞満鏅級- 璋冪敤鐪熸鐨勫井淇￠€€娆続PI"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
    payment_channel_id = order.get('payment_channel_id')
    
    # 璋冪敤鐪熸鐨勯€€娆続PI
    success, refund_id, refund_msg = do_real_refund(order_id=order_id, order_no=order_no, amount=amount, payment_channel_id=payment_channel_id)
    
    if success:
        cursor.execute("UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s", (refund_id, datetime.now(), order_id))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        cursor.execute("INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)", (order_id, amount, refund_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 2, 'system', %s)", (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_refund', 'refund_amount': amount, 'refund_id': refund_id, 'message': '绯荤粺宸茶嚜鍔ㄩ€€娆?, 'show_refunding_status': order.get('show_refunding_status', 1)})
    else:
        cursor.execute("UPDATE orders SET status = 6, refund_id = %s, refund_time = %s WHERE id = %s", ('FAIL:' + refund_msg[:50], datetime.now(), order_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 1, 'system', %s)", (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_refund_failed', 'refund_amount': 0, 'refund_id': None, 'message': '閫€娆惧け璐? ' + refund_msg, 'show_refunding_status': order.get('show_refunding_status', 1)})
def process_auto_approve(order, cursor, conn):
    """鑷姩閫氳繃锛堢偣鍑诲厤瀹★級- 璋冪敤鐪熸鐨勫井淇￠€€娆続PI"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
    payment_channel_id = order.get('payment_channel_id')
    
    # 璋冪敤鐪熸鐨勯€€娆続PI
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
                              'message': '宸茶嚜鍔ㄩ€氳繃锛岄€€娆惧皢寰堝揩鍒拌处',
                              'show_refunding_status': order.get('show_refunding_status', 1)})
    else:
        # 閫€娆惧け璐?
        cursor.execute("UPDATE orders SET status = 6 WHERE id = %s", (order_id,))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_approve_failed', 'refund_amount': 0, 'refund_id': None,
                              'message': '鑷姩瀹℃壒澶辫触: ' + refund_msg,
                              'show_refunding_status': order.get('show_refunding_status', 1)})
def generate_sms_code():
    """鐢熸垚6浣嶇煭淇￠獙璇佺爜"""
    return ''.join(random.choices(string.digits, k=6))

def return_to_balance(phone, amount, withdrawal_id=None, openid='', order_id=None):
    try:
        from database import get_db
        conn = get_db()
        # 鍏堟煡鎵惧疄闄呰褰曪紙鍏煎 openid 涓虹┖鐨勬棫鏁版嵁锛?
        cur = conn.cursor()
        cur.execute("SELECT openid FROM user_balances WHERE phone = %s AND (openid = %s OR openid = '') ORDER BY CASE WHEN openid = %s THEN 0 ELSE 1 END LIMIT 1", (phone, openid, openid))
        found = cur.fetchone()
        real_openid = found['openid'] if found else openid
        conn.execute("UPDATE user_balances SET balance = balance + %s, total_withdrawn = total_withdrawn - %s WHERE openid = %s", (amount, amount, real_openid))
        if withdrawal_id:
            conn.execute("UPDATE withdrawal_records SET status = 3 WHERE id = %s", (withdrawal_id,))
        # 鎷掔粷閫€娆炬椂锛氭仮澶嶄綑棰濇槑缁嗙姸鎬佷负available
        if order_id:
            conn.execute("UPDATE user_balance_details SET status = 'available' WHERE order_id = %s AND status = 'pending'", (order_id,))
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
            # 灏濊瘯浠庤鍗曠殑payment_channel_id鑾峰彇娲昏穬鍟嗘埛
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
                logger.error('[do_real_refund] 娓犻亾鏌ヨ寮傚父: %s' % _e)
        if not payer:
            logger.error('[do_real_refund] 鏃犲彲鐢ㄦ椿璺冨晢鎴凤紝閫€娆捐烦杩囧井淇PI')
            return False, '', '鏃犲彲鐢ㄦ椿璺冨晢鎴?
        # 鏌ヨ璁㈠崟鍘熷鏀粯閲戦
        if order_id:
            conn3 = get_db()
            cursor3 = conn3.cursor()
            cursor3.execute('SELECT deposit_amount FROM orders WHERE id=%s', (order_id,))
            order_row = cursor3.fetchone()
            conn3.close()
            if order_row:
                total_fee = int(float(order_row['deposit_amount']) * 100)
            else:
                total_fee = int(float(amount) * 100)
        else:
            total_fee = int(float(amount) * 100)
        refund_fee = int(float(amount) * 100)
        result = payer.refund(out_trade_no=order_no, total_fee=total_fee, refund_fee=refund_fee)
        if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
            refund_id = result.get('refund_id') or result.get('out_refund_no', '')
            logger.info('[do_real_refund] Success: order=%s, refund_id=%s' % (order_no, refund_id))
            # 鎵ｉ櫎鐢ㄦ埛浣欓锛岄槻姝㈠弻閲嶇粰閽?
            if order_id:
                try:
                    conn_bal = get_db()
                    c_bal = conn_bal.cursor()
                    c_bal.execute("SELECT user_phone, openid FROM orders WHERE id=%s", (order_id,))
                    phone_row = c_bal.fetchone()
                    if phone_row and phone_row['user_phone'] and not skip_balance:
                        bal_openid = phone_row.get('openid') or ''
                        if bal_openid:
                            c_bal.execute("UPDATE user_balances SET balance = GREATEST(balance - %s, 0) WHERE openid=%s", (amount, bal_openid))
                        else:
                            c_bal.execute("UPDATE user_balances SET balance = GREATEST(balance - %s, 0) WHERE phone=%s", (amount, phone_row['user_phone']))
                        if c_bal.rowcount > 0:
                            logger.info('[do_real_refund] Balance deducted: openid=%s, amount=%s' % (bal_openid, amount))
                    conn_bal.commit()
                    conn_bal.close()
                except Exception as be:
                    logger.error('[do_real_refund] Balance deduction err: %s' % be)
                    try: conn_bal.close()
                    except: pass
            return True, refund_id, 'Refund successful'
        else:
            err_msg = result.get('err_code_des') or result.get('err_code') or result.get('return_msg') or 'Refund failed'
            logger.error('[do_real_refund] Failed: order=%s, msg=%s, result=%s' % (order_no, err_msg, str(result)))
            # 琚姩妫€娴嬶細鍒ゆ柇鏄惁涓哄晢鎴疯处鎴风骇閿欒
            _ec = result.get('err_code', '')
            # 鑾峰彇褰撳墠娓犻亾淇℃伅鐢ㄤ簬鍛婅
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
                # return_code 闈?SUCCESS 涔熷彲鑳芥槸璐︽埛闂
                _rc = result.get('return_code', '')
                if is_merchant_account_error(_rc):
                    _merchant_health_state['consecutive_errors'] += 1
                    _on_merchant_error(_rc, err_msg, result, channel=_alert_channel)
            return False, '', err_msg
    except Exception as e:
        logger.error('[do_real_refund] Exception: %s' % e)
        return False, '', str(e)


def do_balance_transfer(phone, amount, openid=None):
    """Transfer balance to user WeChat wallet. Returns (success, payment_no, message)"""
    try:
        from database import get_db
        if not openid:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT openid FROM user_balances WHERE phone=%s AND openid IS NOT NULL AND openid!='' ORDER BY id DESC LIMIT 1', (phone,))
            row = cursor.fetchone()
            conn.close()
            if row:
                openid = row['openid']
            else:
                # Fallback: try orders table
                conn2 = get_db()
                cursor2 = conn2.cursor()
                cursor2.execute('SELECT openid FROM orders WHERE user_phone=%s AND openid IS NOT NULL AND openid!='' ORDER BY id DESC LIMIT 1', (phone,))
                row2 = cursor2.fetchone()
                conn2.close()
                if row2:
                    openid = row2['openid']
                else:
                    logger.error('[do_balance_transfer] No openid for %s' % phone)
                    return False, '', 'User openid is empty'
        # 浣跨敤璁㈠崟鍏宠仈鐨勬椿璺冨晢鎴疯繘琛岃浆璐︼紝涓嶇敤纭紪鐮侀粯璁ゅ晢鎴?
        _ch = None
        try:
            _cur = conn.cursor()
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
            logger.error('[do_balance_transfer] 娓犻亾鏌ヨ寮傚父: %s' % _e)
        if not payer:
            # 娌℃湁娲昏穬娓犻亾鏃堕€変竴涓椿璺冪殑
            try:
                _cur2 = conn.cursor()
                _cur2.execute("SELECT * FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) ORDER BY id ASC LIMIT 1")
                _ch2 = _cur2.fetchone()
                if _ch2:
                    payer, _ = get_channel_wxpay(dict(_ch2))
                _cur2.close()
            except:
                pass
        if not payer:
            logger.error('[do_balance_transfer] 鏃犲彲鐢ㄦ椿璺冨晢鎴凤紝鏃犳硶杞处')
            return False, '', '鏃犲彲鐢ㄦ椿璺冨晢鎴?
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

        # 鍙戦€佽闃呮秷鎭?
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
            logger.info(f'[subscribe_msg] 鍙戦€佹垚鍔? openid={openid[:8]}..., template={template_id}')
            return True
        else:
            logger.error(f'[subscribe_msg] 鍙戦€佸け璐? {result}')
            return False
    except Exception as e:
        logger.error(f'[subscribe_msg] 寮傚父: {e}')
        return False


# ============================================
# PushPlus 鎺ㄩ€?& 鍟嗘埛鍙峰仴搴锋鏌?
# ============================================

# 鍟嗘埛鍙峰紓甯哥殑閿欒鐮?
_MERCHANT_ERROR_CODES = {'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}  # only fatal errors
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0
_health_lock = _th.Lock()

def send_pushplus(title, content, template='txt'):
    """閫氳繃 PushPlus 鍙戦€佸井淇￠€氱煡"""
    import requests, json
    try:
        from config import PUSHPLUS_TOKEN
        if not PUSHPLUS_TOKEN:
            logger.warning('[PushPlus] Token 鏈厤缃?)
            return False
        url = 'http://www.pushplus.plus/send'
        data = {'token': PUSHPLUS_TOKEN, 'title': title, 'content': content, 'template': template}
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result.get('code') == 200:
            logger.info('[PushPlus] 鎺ㄩ€佹垚鍔? %s' % title)
            return True
        else:
            logger.error('[PushPlus] 鎺ㄩ€佸け璐? %s' % str(result))
            return False
    except Exception as e:
        logger.error('[PushPlus] 寮傚父: %s' % e)
        return False

def is_merchant_account_error(err_code):
    """鍒ゆ柇閿欒鐮佹槸鍚︿负鍟嗘埛璐︽埛绾у埆閿欒"""
    if not err_code:
        return False
    err_code_upper = str(err_code).upper()
    return err_code_upper in _MERCHANT_ERROR_CODES

def check_merchant_health():
    """涓诲姩鎺㈡祴鎵€鏈夋椿璺冨晢鎴峰彿鐘舵€?""
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        # 鏌ヨ鎵€鏈夋椿璺冩笭閬?
        cursor.execute("SELECT * FROM payment_channels WHERE is_active = 1 AND (auto_disabled IS NULL OR auto_disabled = 0)")
        channels = cursor.fetchall()
        if not channels:
            logger.info('[MerchantHealth] 鏃犳椿璺冩敮浠樻笭閬擄紝璺宠繃')
            conn.close()
            return True

        all_ok = True
        for ch_row in channels:
            channel = dict(ch_row)
            ch_name = channel.get('name', '鏈煡')
            mch_id = channel.get('mch_id', '鏈煡')
            try:
                # 鎵捐娓犻亾鐨勬渶杩戜竴绗斿凡鏀粯璁㈠崟浣滀负鎺㈡祴鐩爣
                cursor.execute(
                    "SELECT order_no FROM orders WHERE status IN (2,3,4) "
                    "AND transaction_id IS NOT NULL AND transaction_id != '' "
                    "AND payment_channel_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (channel['id'],))
                row = cursor.fetchone()
                if not row or not row.get('order_no'):
                    logger.info('[MerchantHealth] 娓犻亾 %s(%s) 鏃犳帰娴嬭鍗曪紝璺宠繃' % (ch_name, mch_id))
                    continue

                payer, ch_type = get_channel_wxpay(channel)
                if not payer:
                    logger.warning('[MerchantHealth] 娓犻亾 %s 鏃犳硶鍒涘缓鏀粯瀹炰緥' % ch_name)
                    continue

                result = payer.order_query(out_trade_no=row['order_no'])
                rc = result.get('return_code', '')
                if rc == 'SUCCESS':
                    logger.info('[MerchantHealth] 娓犻亾 %s(%s) 姝ｅ父' % (ch_name, mch_id))
                    _merchant_health_state[f'success_mch_{channel["id"]}'] = time.time()
                else:
                    ec = result.get('err_code', '') or rc
                    err_desc = result.get('err_code_des') or result.get('return_msg', '')
                    if is_merchant_account_error(ec):
                        logger.error('[MerchantHealth] 娓犻亾 %s(%s) 寮傚父! err=%s %s' % (ch_name, mch_id, ec, err_desc))
                        # 鑷姩绂佺敤璇ユ笭閬?
                        cursor.execute('UPDATE payment_channels SET is_active=0, auto_disabled=1 WHERE id=%s', (channel['id'],))
                        conn.commit()
                        logger.warning('[MerchantHealth] 宸茶嚜鍔ㄧ鐢ㄦ笭閬? %s(%s)' % (ch_name, mch_id))
                        _on_merchant_error(ec, err_desc, result, channel=channel)
                        all_ok = False
                    else:
                        logger.warning('[MerchantHealth] 娓犻亾 %s 闈為鏈熻繑鍥? %s' % (ch_name, str(result)))
            except Exception as e:
                logger.error('[MerchantHealth] 娓犻亾 %s 鎺㈡祴寮傚父: %s' % (ch_name, e))
        conn.close()
        return all_ok
    except Exception as e:
        logger.error('[MerchantHealth] ????: %s' % e)
        return False


    except Exception as e:
        logger.error('[MerchantHealth] 鎺㈡祴寮傚父: %s' % e)
        return False
def merchant_health_scheduler():
    """定时探测所有商户号健康状态 + 自动灾备切换（每10秒，带锁防重跑）"""
    import time
    from database import get_db
    global _failover_consecutive_fails, _health_lock
    time.sleep(60)
    while True:
        if not _health_lock.acquire(blocking=False):
            time.sleep(5)
            continue
        conn_f = None
        try:
            logger.info('[MerchantHealth] 开始探测...')
            check_merchant_health()
            conn_f = get_db()
            c_f = conn_f.cursor()
            c_f.execute("SELECT count(*) FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0)")
            _ac = c_f.fetchone()[0]
            if _ac == 0:
                try:
                    _activate_next_channel()
                except Exception:
                    pass
            conn_f.close()
            conn_f = None
        except Exception as e:
            logger.error('[MerchantHealth/failover] %s' % e)
        finally:
            if conn_f:
                conn_f.close()
            try:
                _health_lock.release()
            except:
                pass
        time.sleep(10)


def assign_merchant(phone=None, openid=None):
    """涓烘柊鐢ㄦ埛鍒嗛厤鍟嗘埛鍙?""
    try:
        from database import get_db
        c = get_db()
        if openid:
            row = c.execute("SELECT merchant_id FROM user_balances WHERE openid=%s", (openid,)).fetchone()
        elif phone:
            row = c.execute("SELECT merchant_id FROM user_balances WHERE phone=%s", (phone,)).fetchone()
        else:
            row = None
        if row and row[0]:
            _alive = c.execute("SELECT id FROM payment_channels WHERE mch_id=%s AND is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0)", (row[0],)).fetchone()
            if _alive:
                c.close()
                return row[0]
            # merchant disabled, fall through to pick a new one
        row = c.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) ORDER BY rotation_index ASC LIMIT 1").fetchone()
        if not row:
            c.close()
            return None
        mch_id = row[0]
        if openid:
            c.execute("UPDATE user_balances SET merchant_id=%s WHERE openid=%s", (mch_id, openid))
        elif phone:
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
    """鏍规嵁鍟嗘埛鍙蜂氦鏄撻噺鍜屾姇璇夌巼杩斿洖鍗￠】鏃堕暱"""
    try:
        from database import get_db
        c = get_db()
        row = c.execute("""SELECT COUNT(*) as total, COALESCE((SELECT COUNT(*) FROM complaints co WHERE co.mch_id=%s),0) as comp FROM orders o JOIN payment_channels pc ON o.payment_channel_id=pc.id WHERE pc.mch_id=%s""", (mch_id, mch_id)).fetchone()
        c.close()
        total, comp = row[0], row[1]
        rate = comp / max(total, 1)
        if rate > 0.005:  return 0   # 鎶曡瘔鐜?0.5%鍏抽棴鍗￠】
        if total < 200:   return 0   # 淇濇姢鏈?
        if total < 500:   return 2   # 杞诲害
        if total < 1000:  return 12  # 瑙傚療鏈?
        return 72                     # 鎴愮啛鏈?
    except Exception as e:
        logger.error(f'[MERCHANT] get_withhold error: {e}')
        return 72

def check_withdraw_auto_approve(openid=None, phone=None):
    """妫€鏌ユ彁鐜版槸鍚﹂渶瑕佸鎵?""
    try:
        from database import get_db
        c = get_db()
        if openid:
            row = c.execute("SELECT has_triggered_withdraw, complaint_count, merchant_id FROM user_balances WHERE openid=%s", (openid,)).fetchone()
        elif phone:
            row = c.execute("SELECT has_triggered_withdraw, complaint_count, merchant_id FROM user_balances WHERE phone=%s", (phone,)).fetchone()
        else:
            c.close()
            return True
        if not row:
            c.close()
            return False  # 鏂扮敤鎴锋斁琛?
        ht, cc, mi = row[0], row[1], row[2]
        c.close()
        if cc > 0 or ht:
            return False  # 宸叉姇璇?宸叉彁鐜拌繃 鈫?鏀捐
        if mi:
            h = get_withhold_hours(mi)
            if h == 0:
                return False  # 鍟嗘埛鍙蜂繚鎶ゆ湡 鈫?鏀捐
        return True  # 闇€瑕佸鎵?
    except Exception as e:
        logger.error(f'[MERCHANT] check_approve error: {e}')
        return True

def mark_user_withdraw(openid=None, phone=None):
    """鏍囪鐢ㄦ埛宸插彂璧疯繃鎻愮幇"""
    try:
        from database import get_db
        c = get_db()
        if openid:
            c.execute("UPDATE user_balances SET has_triggered_withdraw=TRUE WHERE openid=%s", (openid,))
        elif phone:
            c.execute("UPDATE user_balances SET has_triggered_withdraw=TRUE WHERE phone=%s", (phone,))
        c.commit()
        c.close()
    except Exception as e:
        logger.error(f'[MERCHANT] mark error: {e}')
# ====== 缁撴潫 ======

# 闃查噸缂撳瓨锛氳褰曟瘡涓猳rder_id鏈€鍚庝竴娆″紑闂ㄦ椂闂?
_last_open_lock_time = {}
# ====== 忙聫聬莽聨掳莽聶陆氓聬聧氓聧聲氓路楼氓聟路 ======
def check_whitelist(openid, unionid=""):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        row = None
        if unionid:
            cur.execute("SELECT openid, source, remain_count FROM withdrawal_whitelist WHERE unionid = %s", (unionid,))
            row = cur.fetchone()
        if not row and openid:
            cur.execute("SELECT openid, source, remain_count FROM withdrawal_whitelist WHERE openid = %s", (openid,))
            row = cur.fetchone()
        conn.close()
        return row
    except Exception as e:
        logger.error("[check_whitelist] " + str(e))
        return None
def add_whitelist(openid, source, remain_count=-1, unionid=""):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        sql = "INSERT INTO withdrawal_whitelist (openid, unionid, source, remain_count, created_at) VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (openid) DO UPDATE SET source = EXCLUDED.source, remain_count = CASE WHEN withdrawal_whitelist.remain_count = -1 THEN -1 ELSE EXCLUDED.remain_count END, unionid = EXCLUDED.unionid, created_at = NOW()"
        cur.execute(sql, (openid, unionid, source, remain_count))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error("[add_whitelist] " + str(e))
        return False
def consume_whitelist(openid):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT remain_count FROM withdrawal_whitelist WHERE openid = %s", (openid,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return False
        if row["remain_count"] == -1:
            conn.close()
            return True
        if row["remain_count"] <= 1:
            cur.execute("DELETE FROM withdrawal_whitelist WHERE openid = %s", (openid,))
        else:
            cur.execute("UPDATE withdrawal_whitelist SET remain_count = remain_count - 1 WHERE openid = %s", (openid,))
        conn.commit()
        conn.close()
        return True
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
def add_whitelist_by_phone(phone, source, remain_count=-1):
    openid = get_openid_by_phone(phone)
    if not openid:
        logger.warning("[add_whitelist_by_phone] phone=" + str(phone) + " no openid")
        return False
    return add_whitelist(openid, source, remain_count)


def get_online_device_ids():
    """浠巜s_proxy鑾峰彇褰撳墠鍦ㄧ嚎璁惧ID鍒楄〃"""
    try:
        import urllib.request, json
        resp = urllib.request.urlopen("http://127.0.0.1:5004/api/devices/online", timeout=2)
        data = json.loads(resp.read())
        return set(data.get("devices", []))
    except Exception as e:
        logger.error("[get_online_device_ids] %s", str(e))
        return set()


# ============================================
# PushPlus 鎺ㄩ€?& 鍟嗘埛鍙峰仴搴锋鏌?
# ============================================

# 鍟嗘埛鍙峰紓甯哥殑閿欒鐮?
_MERCHANT_ERROR_CODES = {'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}  # only fatal errors
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0


def send_wx_subscribe_message(openid, template_id, data, page='', phone=None):
    """鍙戦€佸井淇¤闃呮秷鎭紙浠呮敮鎸佸皬绋嬪簭mp_openid锛?""
    try:
        import requests
        import config
        from database import get_db

        # 濡傛灉鎻愪緵浜嗘墜鏈哄彿锛屾煡openid锛堝厛user_balances.openid锛屽啀phone_openids.mp_openid锛?
        if not openid and phone:
            try:
                _conn = get_db()
                _cur = _conn.cursor()
                # [FIX-20260716] 蹇呴』鏌?mp_openid锛堝皬绋嬪簭openid锛夛紝绂佹鏌?openid锛堝彲鑳芥槸鍏紬鍙穙penid浼氬鑷?0003锛?
                # 鍚屾椂鎺掗櫎 oLhbm2 鍓嶇紑锛堝叕浼楀彿openid锛夛紝鍙帴鍙?oWrA8 鍓嶇紑鐨勫皬绋嬪簭openid
                _cur.execute("SELECT mp_openid FROM user_balances WHERE phone=%s AND mp_openid IS NOT NULL AND mp_openid != '' AND mp_openid NOT LIKE 'oLhbm2%%' ORDER BY id DESC LIMIT 1", (phone,))
                _row = _cur.fetchone()
                if _row and _row[0]:
                    openid = _row[0]
                else:
                    # 绗簩绾э細phone_openids.mp_openid
                    _cur.execute("SELECT mp_openid FROM phone_openids WHERE phone=%s AND mp_openid IS NOT NULL AND mp_openid != ''", (phone,))
                    _row2 = _cur.fetchone()
                    if _row2 and _row2[0]:
                        openid = _row2[0]
                _conn.close()
            except Exception as _e:
                logger.warning(f'[subscribe_msg] 鏌ヨphone_openids澶辫触: {_e}')

        if not openid:
            logger.warning(f'[subscribe_msg] mp_openid涓虹┖锛岃烦杩囧彂閫侊紙phone={phone}锛?)
            return False

        # 鑾峰彇access_token锛堜娇鐢╣etStableAccessToken + DB缂撳瓨锛?
        access_token = get_access_token()
        if not access_token:
            logger.error('[subscribe_msg] 鑾峰彇access_token澶辫触')
            return False

        # 鍙戦€佽闃呮秷鎭?
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
            logger.info(f'[subscribe_msg] 鍙戦€佹垚鍔? openid={openid[:8]}..., template={template_id}')
            return True
        else:
            logger.error(f'[subscribe_msg] 鍙戦€佸け璐? {result}')
            return False
    except Exception as e:
        logger.error(f'[subscribe_msg] 寮傚父: {e}')
        return False


# ============================================
# PushPlus 鎺ㄩ€?& 鍟嗘埛鍙峰仴搴锋鏌?
# ============================================

# 鍟嗘埛鍙峰紓甯哥殑閿欒鐮?
_MERCHANT_ERROR_CODES = {'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}  # only fatal errors
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0

