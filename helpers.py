"""
??????? - ???????????
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
connected_devices = {}         # WebSocket ????? {device_id: sid}
pending_lock_commands = {}

# ?????: ??device_id??Event??????set()
import threading as _th
_pending_cmd_events = {}
_pending_cmd_events_lock = _th.Lock()

def signal_pending_command(device_id):
    """?????????????????"""
    with _pending_cmd_events_lock:
        evt = _pending_cmd_events.get(device_id)
        if evt:
            evt.set()

def get_pending_event(device_id):
    """??(???)?????????"""
    with _pending_cmd_events_lock:
        if device_id not in _pending_cmd_events:
            _pending_cmd_events[device_id] = _th.Event()
        return _pending_cmd_events[device_id]

def clear_pending_event(device_id):
    """??????(????????)"""
    with _pending_cmd_events_lock:
        evt = _pending_cmd_events.get(device_id)
        if evt:
            evt.clear()     # ???????? {device_id: [commands]}

# ============================================
# ????
# ============================================

def _get_device_protocol(device_id):
    """?cabinets?mainboard_source???????????YBM"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT mainboard_source FROM cabinets WHERE mainboard_device_id=%s', (str(device_id),))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logger.error(f'[????] ??: {e}')
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
    """??JSON????"""
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
    """??????"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT setting_value FROM system_settings WHERE setting_key = %s', (key,))
    result = cursor.fetchone()
    conn.close()
    return result['setting_value'] if result else default


def set_setting(key, value):
    """??????"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)', (key, str(value)))
    conn.commit()
    conn.close()


# ============================================
# ????
# ============================================
def is_mock_mode():
    """???????????"""
    return get_setting('pay_mode', 'mock') == 'mock'


# ============================================
# ?????
# ============================================
def is_wechat_browser():
    """???????????"""
    from flask import request
    user_agent = request.headers.get('User-Agent', '')
    return 'MicroMessenger' in user_agent


def is_mobile_browser():
    """????????????"""
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
    """??????? - ????session cookie?Bearer token"""
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
        return json_response(message='????????', code=401)
    return decorated


def require_merchant_auth(f):
    """??/??????? - ????session cookie?Bearer token"""
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
        return json_response(message='????????', code=401)
    return decorated


def require_agent_auth(f):
    """???????"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'agent_id' not in session:
            return json_response(message='????????', code=401)
        return f(*args, **kwargs)
    return decorated


def require_employee_auth(f):
    """??????"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'employee_id' not in session:
            return json_response(message='????????', code=401)
        return f(*args, **kwargs)
    return decorated


# ============================================
# ??????
# ============================================
def should_hide_order(merchant_id, order_id, phone, hide_rate, whitelist, logic_mark=None, total_orders=0):
    """???????????????????
    logic_mark: 'N'=????(???), 'Y'=????, None=?hash??
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
    """?????????"""
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
# WebSocket ????
# ============================================
def supersede_force_update_cmds(cursor, device_id):
    """??????? force_update ???????????????"""
    cursor.execute(
        "UPDATE pending_lock_cmds SET delivered=1, status='cancelled' "
        "WHERE device_id=%s AND (delivered=0 OR status='pending') AND strpos(command,'force_update')>0",
        (device_id,)
    )


def send_open_lock(device_id, board_no, lock_no, protocol=None, order_id='', slot_number=None, slot_label=None, skip_dedup=False, require_online=False, manual=False):
    """
    ?????? - ????WebSocket + Socket.IO + HTTP????
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
                logger.info(f'[SEND_LOCK] ?????????: device_id={device_id}')
                return False
        except Exception as _oe:
            logger.warning(f'[SEND_LOCK] ??????(????): {_oe}')
    # ??1????????? order_id 60???????????worker???
    _now = time.time()
    if not skip_dedup and order_id and order_id in _last_open_lock_time:
        if _now - _last_open_lock_time[order_id] < 60:
            logger.info(f'[SEND_LOCK] ??????: order_id={order_id}, {_now - _last_open_lock_time[order_id]:.1f}s ago')
            return True
    # ??2??worker?????????? order_id 60?????????
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
                logger.info(f'[SEND_LOCK] DB????: order_id={order_id}, found {_dup_count} recent cmds')
                return True
        except Exception as _chk_e:
            logger.warning(f'[SEND_LOCK] DB??????(????): {_chk_e}')
        _last_open_lock_time[order_id] = _now
    # ????????????
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
    # ??WebSocket????DB???DB???????
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
    # ????WebSocket??(??????WS???)
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

    
    # ?????????WS????????
    if not _ws_sent:
        if device_id not in pending_lock_commands:
            pending_lock_commands[device_id] = []
        if command not in pending_lock_commands[device_id]:
            pending_lock_commands[device_id].append(command)
    
    # ????????delivered=0??HTTP?????????WS?????????????
    _delivered = 0
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
        # ??WS????????????????WS?????
        signal_pending_command(device_id)
    except Exception as _e:
        logger.error(f"[DB] ??pending_lock??: {_e}")
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
        logger.error(f"[DB] ??door_record??: {_e3}")
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
# ???? - ????????
# ============================================
def _get_payment_channel(channel_id=None, exclude_channel_id=None):
    """???????????????????"""
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
    # ????????????
    if exclude_channel_id:
        channels = [ch for ch in channels if ch['id'] != exclude_channel_id]
        if not channels:
            conn.close()
            return None
    # ??????
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
        # ?????last_used_at??????????????
        from datetime import datetime as _dt; selected = min(channels, key=lambda ch: ch['last_used_at'] or _dt(1970,1,1))
        logger.info(f"[????-????] ??: {selected['name']} (id={selected['id']}, last_used={selected['last_used_at']})")
    else:
        # ????
        weights = []
        for ch in channels:
            base_weight = ch['weight'] or 1
            inverse_factor = 1.0 / (1 + (ch['total_amount'] or 0) / 1000)
            weights.append(base_weight * inverse_factor)
        selected = random.choices(list(channels), weights=weights, k=1)[0]
        logger.info(f"[????-????] ??: {selected['name']} (id={selected['id']})")
    conn.close()
    return dict(selected)


def select_payment_channel(exclude_channel_id=None):
    """??????????????
    exclude_channel_id: ?????ID?????????????????
    """
    return _get_payment_channel(exclude_channel_id=exclude_channel_id)


def update_channel_stats(channel_id, amount):
    """??????"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE payment_channels SET total_amount = total_amount + %s, total_count = total_count + 1, last_used_at = %s WHERE id = %s',
                       (amount, datetime.now(), channel_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[????] ????: {e}")


def get_channel_wxpay(channel, use_mp_appid=False):
    """????????????"""
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
    """??????????"""
    from wxpay import WxPay, MockWxPay
    mode = get_setting('pay_mode', 'mock')
    if mode == 'mock':
        return MockWxPay()
    app_id = WX_MP_APP_ID if use_mp_appid else WX_APP_ID
    return WxPay(mch_id=WX_MCH_ID, api_key=WX_API_KEY, app_id=app_id,
                 cert_path=WX_CERT_PATH, key_path=WX_KEY_PATH)


def get_payment_params(order_id, order_no, deposit_amount, user_phone=None, openid=None,
                       payment_channel=None, payment_channel_id=None, _retry_count=0):
    """????????"""
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
                scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '?????'})
        else:
            scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '?????'})
    else:
        scene_info = json.dumps({'type': 'Wap', 'wap_url': 'https://locker.cqdyxl.com', 'wap_name': '?????'})

    if openid:
        trade_type = 'JSAPI'
    # ??????
    if payment_channel_id:
        ch = _get_payment_channel(payment_channel_id)
        current_channel = ch or payment_channel
    elif payment_channel:
        current_channel = payment_channel
    else:
        current_channel = _get_payment_channel()  # ??????????fallback????????

    if current_channel:
        wxpay, ch_type = get_channel_wxpay(current_channel, use_mp_appid=False)
        if ch_type == 'third_party' and wxpay:
            third_party_type = 'alipay' if not is_wechat_browser() else 'wechat'
            result = wxpay.unifiedorder(trade_type=third_party_type, body='若押金未退回，请拨打客服电话400-698-1080',
                                         total_fee=int(deposit_amount * 100), out_trade_no=order_no)
            if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
                # ????????????
                if current_channel:
                    update_channel_stats(current_channel['id'], deposit_amount)
                return {'mode': 'third_party', 'channel_type': third_party_type, 'order_id': order_id,
                        'order_no': order_no, 'pay_url': result.get('url', ''), 'url_qrcode': result.get('url_qrcode', '')}
            return {'mode': 'error', 'error_msg': result.get('return_msg', '???????')}
        if wxpay is None:
            return {'mode': 'error', 'error_msg': '????????'}
    else:
        return {'mode': 'error', 'error_msg': '??????????????'}

    total_fee = int(deposit_amount * 100)
    time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')

    result = wxpay.unifiedorder(trade_type=trade_type, body='若押金未退回，请拨打客服电话400-698-1080',
                                 total_fee=total_fee, out_trade_no=order_no,
                                 notify_url=WX_PAY_NOTIFY_URL, openid=openid,
                                 scene_info=scene_info, time_expire=time_expire)

    if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
        # ??????????????????????
        try:
            from database import get_db as _gdb3
            _db3 = _gdb3()
            _db3.execute("UPDATE orders SET payment_channel_id=%s WHERE id=%s", (current_channel["id"], order_id))
            _db3.commit()
            _db3.close()
        except Exception as _e:
            logger.error(f"[??????] ??: {_e}")
        # ??????
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
    
    # ????/??????
    _dead_errors = {'MCH_NOT_EXIST', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}
    _skip_errors = {'NOAUTH', 'NO_AUTH'}  # ???????????????
    _err_code = result.get('err_code', '')
    if current_channel and _retry_count < 3 and (_err_code in _dead_errors or _err_code in _skip_errors):
        # ???????????NOAUTH???????????
        if _err_code in _dead_errors:
            try:
                from database import get_db as _gdb2
                _db2 = _gdb2()
                _db2.execute('UPDATE payment_channels SET is_active=0 WHERE id=%s', (current_channel['id'],))
                _db2.commit()
                _db2.close()
                logger.warning(f'[??] ?????????: id={current_channel["id"]}, name={current_channel.get("name","")}, err={result.get("err_code")}')
            except Exception as _e:
                logger.error(f'[??] ??????: {_e}')
        else:
            logger.warning(f'[??] ??????(???)?????: id={current_channel["id"]}, err={result.get("err_code")}')
        next_ch = select_payment_channel(exclude_channel_id=current_channel['id'])
        if next_ch and next_ch.get('id') and next_ch['id'] != current_channel['id']:
            logger.info(f'[??] ??????????: {next_ch["name"]}')
            # [???] ???????payment_channel_id????????
            # ???????????A???????????B????????????
            logger.warning(f'[??] ???????????????????#{order_id}?payment_channel_id')
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
    return {'mode': 'error', 'error_msg': '??????????'}


def process_auto_refund(order, cursor, conn):
    """???????????- ?????????API"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
    # 检查是否已经退款
    if order.get('refund_status') in ('success','refunded'):
        return json_response({'status': 'already_refunded', 'refund_amount': amount, 'refund_id': None, 'message': '已退款'})
    payment_channel_id = order.get('payment_channel_id')
    
    # ???????API
    success, refund_id, refund_msg = do_real_refund(order_id=order_id, order_no=order_no, amount=amount, payment_channel_id=payment_channel_id)
    
    if success:
        cursor.execute("UPDATE orders SET status = 4, refund_id = %s, refund_time = %s WHERE id = %s", (refund_id, datetime.now(), order_id))
        if order['slot_id']:
            cursor.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s', (order['slot_id'],))
        cursor.execute("INSERT INTO payments (order_id, type, amount, refund_transaction_id, status) VALUES (%s, 2, %s, %s, 1)", (order_id, amount, refund_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 2, 'system', %s)", (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_refund', 'refund_amount': amount, 'refund_id': refund_id, 'message': '???????', 'show_refunding_status': order.get('show_refunding_status', 1)})
    else:
        cursor.execute("UPDATE orders SET status = 6, refund_id = %s, refund_time = %s WHERE id = %s", ('FAIL:' + refund_msg[:50], datetime.now(), order_id))
        cursor.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, auto_approve_time) VALUES (%s, %s, %s, 1, 'system', %s)", (order_id, order['user_phone'], amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_refund_failed', 'refund_amount': 0, 'refund_id': None, 'message': '????: ' + refund_msg, 'show_refunding_status': order.get('show_refunding_status', 1)})
def process_auto_approve(order, cursor, conn):
    """??????????- ?????????API"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
    payment_channel_id = order.get('payment_channel_id')
    
    # ???????API
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
                              'message': '?????????????',
                              'show_refunding_status': order.get('show_refunding_status', 1)})
    else:
        # ????
        cursor.execute("UPDATE orders SET status = 6 WHERE id = %s", (order_id,))
        conn.commit()
        conn.close()
        return json_response({'status': 'auto_approve_failed', 'refund_amount': 0, 'refund_id': None,
                              'message': '??????: ' + refund_msg,
                              'show_refunding_status': order.get('show_refunding_status', 1)})
def generate_sms_code():
    """??6??????"""
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
        row = min(app_candidates, key=lambda r: r['id'])
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
        out['user_id'] = row.get('user_id') or 0
        out['unionid'] = row.get('unionid') or unionid or ''
        out['mp_openid'] = row.get('mp_openid') or mp_openid or ''
        out['phone'] = row.get('phone') or phone or ''
        return out

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
                phone = CASE WHEN %s <> '' THEN %s ELSE phone END,
                openid = COALESCE(NULLIF(%s,''), openid),
                unionid = COALESCE(NULLIF(%s,''), unionid),
                mp_openid = COALESCE(NULLIF(%s,''), mp_openid),
                wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                user_id = CASE WHEN %s > 0 THEN %s ELSE user_id END
              WHERE id = %s""",
            (balance, total_deposited, total_withdrawn,
             phone, phone, openid, unionid, mp_openid, wechat_name, user_id, user_id, row_id),
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
                 mp_openid = COALESCE(NULLIF(EXCLUDED.mp_openid,''), user_balances.mp_openid),
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
                 mp_openid = COALESCE(NULLIF(EXCLUDED.mp_openid,''), user_balances.mp_openid),
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
                 mp_openid = COALESCE(NULLIF(EXCLUDED.mp_openid,''), user_balances.mp_openid),
                 wechat_name = COALESCE(NULLIF(EXCLUDED.wechat_name,''), user_balances.wechat_name)
               RETURNING id""",
            (phone, openid, unionid, mp_openid, wechat_name, balance, total_deposited, total_withdrawn, user_id),
        )
    row = cursor.fetchone()
    return row['id'] if row else None


def upsert_phone_openid_row(cursor, phone='', openid='', mp_openid='', unionid='', wechat_name='', user_id=0):
    """Insert or update a users row keyed by unionid/openid/phone."""
    phone = _clean(phone)
    openid = _clean(openid)
    unionid = _clean(unionid)
    mp_openid = _clean(mp_openid)
    wechat_name = _clean(wechat_name)
    if not phone:
        return None
    if unionid:
        cursor.execute("SELECT id FROM users WHERE unionid = %s AND id > 0 LIMIT 1", (unionid,))
        r = cursor.fetchone()
        if r:
            cursor.execute("UPDATE users SET phone=COALESCE(NULLIF(%s,''),phone), openid=COALESCE(NULLIF(%s,''),openid), mp_openid=COALESCE(NULLIF(%s,''),mp_openid), wechat_name=COALESCE(NULLIF(%s,''),wechat_name), updated_at=NOW() WHERE id=%s", (phone, openid, mp_openid, wechat_name, r['id']))
            return r['id']
    if openid:
        cursor.execute("SELECT id FROM users WHERE openid = %s AND id > 0 LIMIT 1", (openid,))
        r = cursor.fetchone()
        if r:
            cursor.execute("UPDATE users SET phone=COALESCE(NULLIF(%s,''),phone), unionid=COALESCE(NULLIF(%s,''),unionid), mp_openid=COALESCE(NULLIF(%s,''),mp_openid), wechat_name=COALESCE(NULLIF(%s,''),wechat_name), updated_at=NOW() WHERE id=%s", (phone, unionid, mp_openid, wechat_name, r['id']))
            return r['id']
    cursor.execute("SELECT id FROM users WHERE phone = %s AND id > 0 LIMIT 1", (phone,))
    r = cursor.fetchone()
    if r:
        cursor.execute("UPDATE users SET openid=COALESCE(NULLIF(%s,''),openid), unionid=COALESCE(NULLIF(%s,''),unionid), mp_openid=COALESCE(NULLIF(%s,''),mp_openid), wechat_name=COALESCE(NULLIF(%s,''),wechat_name), updated_at=NOW() WHERE id=%s", (openid, unionid, mp_openid, wechat_name, r['id']))
        return r['id']
    cursor.execute("INSERT INTO users (phone, openid, mp_openid, unionid, wechat_name) VALUES (%s,%s,%s,%s,%s) RETURNING id", (phone, openid, mp_openid, unionid, wechat_name))
    r = cursor.fetchone()
    return r['id'] if r else None

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
        # ???????????????available
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
    """??/??????????????? (????, mp_openid)"""
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
            # ??????payment_channel_id??????
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
                logger.error('[do_real_refund] ??????: %s' % _e)
        if not payer:
            logger.error('[do_real_refund] ??????????????API')
            return False, '', '???????'
        # ??????????
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
            # ?????????calc_balance ?????????????? user_balances?
            if order_id:
                try:
                    conn_bal = get_db()
                    c_bal = conn_bal.cursor()
                    c_bal.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=%s WHERE id=%s", (refund_id, order_id))
                    if c_bal.rowcount > 0:
                        logger.info("[do_real_refund] Orders updated: order_id=%s" % order_id)
                    c_bal.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order_id,))
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
            # ???????????/???????????????????????????
            _already_refunded = ('???' in str(err_msg)) or ('????' in str(err_msg))
            if _already_refunded:
                _rid = result.get('refund_id') or result.get('out_refund_no') or ('ALREADY_' + str(order_id or order_no))
                logger.info('[do_real_refund] Already refunded: order=%s, refund_id=%s, msg=%s' % (order_no, _rid, err_msg))
                if order_id:
                    try:
                        _bal = get_db()
                        _balc = _bal.cursor()
                        _balc.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=%s WHERE id=%s", (_rid, order_id))
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
            # ?????????????????
            _ec = result.get('err_code', '')
            # ????????????
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
                # return_code ? SUCCESS ????????
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
        # ?????????????????????????
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
            logger.error('[do_balance_transfer] ??????: %s' % _e)
        if not payer:
            # ?????????????
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
            logger.error('[do_balance_transfer] ????????????')
            return False, '', '???????'
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

        # ??????
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
            logger.info(f'[subscribe_msg] ????: openid={openid[:8]}..., template={template_id}')
            return True
        else:
            logger.error(f'[subscribe_msg] ????: {result}')
            return False
    except Exception as e:
        logger.error(f'[subscribe_msg] ??: {e}')
        return False


# ============================================
# PushPlus ?? & ???????
# ============================================

# ?????????
_MERCHANT_ERROR_CODES = {'SIGN_ERROR', 'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'SYSTEMERROR', 'FREQUENCY_LIMITED'}  # NO_AUTH removed
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0

def send_pushplus(title, content, template='txt'):
    """?? PushPlus ??????"""
    import requests, json
    try:
        from config import PUSHPLUS_TOKEN
        if not PUSHPLUS_TOKEN:
            logger.warning('[PushPlus] Token ???')
            return False
        url = 'http://www.pushplus.plus/send'
        data = {'token': PUSHPLUS_TOKEN, 'title': title, 'content': content, 'template': template}
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result.get('code') == 200:
            logger.info('[PushPlus] ????: %s' % title)
            return True
        else:
            logger.error('[PushPlus] ????: %s' % str(result))
            return False
    except Exception as e:
        logger.error('[PushPlus] ??: %s' % e)
        return False

def is_merchant_account_error(err_code):
    """????????????????"""
    if not err_code:
        return False
    err_code_upper = str(err_code).upper()
    return err_code_upper in _MERCHANT_ERROR_CODES

def _on_merchant_error(err_code, err_desc, raw_result, channel=None):
    """????????????????????????????"""
    import time
    now = time.time()
    # ????????????????????????????????
    mch_key = 'last_alert_%s' % (channel['mch_id'] if channel else 'default')
    last = _merchant_health_state.get(mch_key, 0)
    if now - last < 600:  # 10???????????
        return
    _merchant_health_state[mch_key] = now
    _merchant_health_state['last_alert_time'] = now
    # ??????????????
    mch_id = channel.get('mch_id', '??') if channel else '??(????)'
    mch_name = channel.get('name', '??') if channel else '??(????)'
    ch_id = channel.get('id', '?') if channel else '?'
    title = '?%s??????' % mch_name
    content = ("?????????????????????\n"
               "????: %s\n"
               "???(mch_id): %s\n"
               "??ID: %s\n"
               "???: %s\n"
               "????: %s\n"
               "????? pay.weixin.qq.com ???") % (mch_name, mch_id, ch_id, err_code, err_desc)
    send_pushplus(title, content)


def check_merchant_health():
    """?????????????"""
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        # ????????
        cursor.execute("SELECT * FROM payment_channels WHERE is_active = 1")
        channels = cursor.fetchall()
        if not channels:
            logger.info('[MerchantHealth] ??????????')
            conn.close()
            return True

        all_ok = True
        for ch_row in channels:
            channel = dict(ch_row)
            ch_name = channel.get('name', '??')
            mch_id = channel.get('mch_id', '??')
            try:
                # ????????????????????
                cursor.execute(
                    "SELECT order_no FROM orders WHERE status IN (2,3,4) "
                    "AND transaction_id IS NOT NULL AND transaction_id != '' "
                    "AND payment_channel_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (channel['id'],))
                row = cursor.fetchone()
                if not row or not row.get('order_no'):
                    logger.info('[MerchantHealth] ?? %s(%s) ????????' % (ch_name, mch_id))
                    continue

                payer, ch_type = get_channel_wxpay(channel)
                if not payer:
                    logger.warning('[MerchantHealth] ?? %s ????????' % ch_name)
                    continue

                result = payer.order_query(out_trade_no=row['order_no'])
                rc = result.get('return_code', '')
                if rc == 'SUCCESS':
                    logger.info('[MerchantHealth] ?? %s(%s) ??' % (ch_name, mch_id))
                    _merchant_health_state[f'success_mch_{channel["id"]}'] = time.time()
                else:
                    ec = result.get('err_code', '') or rc
                    err_desc = result.get('err_code_des') or result.get('return_msg', '')
                    if is_merchant_account_error(ec):
                        logger.error('[MerchantHealth] ?? %s(%s) ??! err=%s %s' % (ch_name, mch_id, ec, err_desc))
                        # ???????
                        cursor.execute('UPDATE payment_channels SET is_active=0, auto_disabled=1 WHERE id=%s', (channel['id'],))
                        conn.commit()
                        logger.warning('[MerchantHealth] ???????: %s(%s)' % (ch_name, mch_id))
                        _on_merchant_error(ec, err_desc, result, channel=channel)
                        all_ok = False
                    else:
                        logger.warning('[MerchantHealth] ?? %s ?????: %s' % (ch_name, str(result)))
            except Exception as e:
                logger.error('[MerchantHealth] ?? %s ????: %s' % (ch_name, e))
        conn.close()
        return all_ok
    except Exception as e:
        logger.error('[MerchantHealth] ????: %s' % e)
        return False


    except Exception as e:
        logger.error('[MerchantHealth] ????: %s' % e)
        return False

def merchant_health_scheduler():
    """????????????? + ????????30???"""
    import time
    from database import get_db
    global _failover_consecutive_fails
    time.sleep(60)
    while True:
        conn_f = None
        try:
            logger.info('[MerchantHealth] ????...')
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
        except Exception as e:
            logger.error('[MerchantHealth/failover] %s' % e)
        finally:
            if conn_f:
                conn_f.close()
        time.sleep(10)



def assign_merchant(phone=None, openid=None, user_id=0):
    """?????????"""
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
    """??????????????????"""
    try:
        from database import get_db
        c = get_db()
        row = c.execute("""SELECT COUNT(*) as total, COALESCE((SELECT COUNT(*) FROM complaints co WHERE co.mch_id=%s),0) as comp FROM orders o JOIN payment_channels pc ON o.payment_channel_id=pc.id WHERE pc.mch_id=%s""", (mch_id, mch_id)).fetchone()
        c.close()
        total, comp = row[0], row[1]
        rate = comp / max(total, 1)
        if rate > 0.005:  return 0   # ???>0.5%????
        if total < 200:   return 0   # ???
        if total < 500:   return 2   # ??
        if total < 1000:  return 12  # ???
        return 72                     # ???
    except Exception as e:
        logger.error(f'[MERCHANT] get_withhold error: {e}')
        return 72

def check_withdraw_auto_approve(openid=None, phone=None, user_id=0):
    """??????????"""
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
            return False  # ?????
        ht, cc, mi = ub.get('has_triggered_withdraw'), ub.get('complaint_count'), ub.get('merchant_id')
        c.close()
        if cc > 0 or ht:
            return False  # ???/???? ? ??
        if mi:
            h = get_withhold_hours(mi)
            if h == 0:
                return False  # ?????? ? ??
            return True  # ????
        return False  # ?????? ? ???????
    except Exception as e:
        logger.error(f'[MERCHANT] check_approve error: {e}')
        return True

def mark_user_withdraw(openid=None, phone=None, user_id=0):
    """??????????"""
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
# ====== ?? ======

# ?????????order_id????????
_last_open_lock_time = {}
# ====== ????????????????????? ======
def check_whitelist(openid):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT openid, source, remain_count FROM withdrawal_whitelist WHERE openid = %s", (openid,))
        row = cur.fetchone()
        conn.close()
        return row
    except Exception as e:
        logger.error("[check_whitelist] " + str(e))
        return None
def add_whitelist(openid, source, remain_count=-1):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        sql = "INSERT INTO withdrawal_whitelist (openid, source, remain_count, created_at) VALUES (%s, %s, %s, NOW()) ON CONFLICT (openid) DO UPDATE SET source = EXCLUDED.source, remain_count = CASE WHEN withdrawal_whitelist.remain_count = -1 THEN -1 ELSE EXCLUDED.remain_count END, created_at = NOW()"
        cur.execute(sql, (openid, source, remain_count))
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
    """?ws_proxy????????ID??"""
    try:
        import urllib.request, json
        resp = urllib.request.urlopen("http://127.0.0.1:5004/api/devices/online", timeout=2)
        data = json.loads(resp.read())
        return set(data.get("devices", []))
    except Exception as e:
        logger.error("[get_online_device_ids] %s", str(e))
        return set()


def is_device_online(device_id, heartbeat=None):
    """??????????????????/??????"""
    device_id = str(device_id)
    if device_id in connected_devices:
        return True
    if device_id in get_online_device_ids():
        return True
    if heartbeat:
        try:
            if isinstance(heartbeat, str):
                heartbeat = datetime.strptime(str(heartbeat)[:19], "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - heartbeat).total_seconds() < 120
        except Exception:
            pass
    return False


# ============================================
# PushPlus ?? & ???????
# ============================================

# ?????????
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
    """???????????????mp_openid?"""
    try:
        import requests
        import config
        from database import get_db

        # ??????????openid??user_balances.openid??phone_openids.mp_openid?
        if not openid and phone:
            try:
                _conn = get_db()
                _cur = _conn.cursor()
                # [FIX-20260716] ??? mp_openid????openid????? openid???????openid???40003?
                # ???? oLhbm2 ??????openid????? oWrA8 ??????openid
                _ub_row = find_user_balance_row(_cur, phone=phone, unionid=unionid or '')
                if _ub_row and _ub_row.get('mp_openid') and _ub_row['mp_openid'] not in ('', None) and not _ub_row['mp_openid'].startswith('oLhbm2'):
                    openid = _ub_row['mp_openid']
                if not openid:
                    _po_rows = phone_openid_rows(_cur, phone=phone, unionid=unionid or '')
                    if len(_po_rows) == 1 and _po_rows[0].get('mp_openid') and not _po_rows[0]['mp_openid'].startswith('oLhbm2'):
                        openid = _po_rows[0]['mp_openid']
                    elif len(_po_rows) > 1 and not unionid:
                        logger.warning(f'[subscribe_msg] ????????????unionid????: phone={phone}')
                _conn.close()
            except Exception as _e:
                logger.warning(f'[subscribe_msg] ??phone_openids??: {_e}')

        # ??????openid??????????????????????openid
        if openid and openid.startswith('oLhbm2') and phone:
            try:
                _conn3 = get_db()
                _cur3 = _conn3.cursor()
                _r3 = None
                _ub3 = find_user_balance_row(_cur3, phone=phone, unionid=unionid or '')
                if _ub3 and _ub3.get('mp_openid') and _ub3['mp_openid'].startswith('oWrA8'):
                    _r3 = (_ub3['mp_openid'],)
                if not _r3:
                    _po3 = phone_openid_rows(_cur3, phone=phone, unionid=unionid or '')
                    if len(_po3) == 1 and _po3[0].get('mp_openid') and _po3[0]['mp_openid'].startswith('oWrA8'):
                        _r3 = (_po3[0]['mp_openid'],)
                _conn3.close()
                if _r3 and _r3[0]:
                    openid = _r3[0]
            except Exception as _e3:
                logger.warning(f'[subscribe_msg] ??openid??: {_e3}')
        if openid and openid.startswith('oLhbm2'):
            logger.warning(f'[subscribe_msg] ?????openid: openid={openid[:8]}..., phone={phone}')
            return False
        if not openid:
            logger.warning(f'[subscribe_msg] mp_openid????????phone={phone}?')
            return False

        # ??access_token???getStableAccessToken + DB???
        access_token = get_access_token()
        if not access_token:
            logger.error('[subscribe_msg] ??access_token??')
            return False

        # ??????
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
            logger.info(f'[subscribe_msg] ????: openid={openid[:8]}..., template={template_id}')
            return True
        else:
            logger.error(f'[subscribe_msg] ????: openid={openid[:8]}..., phone={phone}, template={template_id}, result={result}')
            return False
    except Exception as e:
        logger.error(f'[subscribe_msg] ??: {e}')
        return False


# ============================================
# PushPlus ?? & ???????
# ============================================

# ?????????
_MERCHANT_ERROR_CODES = {'SIGN_ERROR', 'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'SYSTEMERROR', 'FREQUENCY_LIMITED'}  # NO_AUTH removed
_merchant_health_state = {'last_alert_time': 0, 'consecutive_errors': 0}
_failover_standby_id = 8
_failover_consecutive_fails = 0


def calc_balance(user_id=None, phone=None, openid=None, mp_openid=None, unionid=None):
    """??????????????????? - ??? - ??? - ??????????"""
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    try:
        ident = resolve_user_identity(c, openid=openid or '', mp_openid=mp_openid or '', phone=phone or '', unionid=unionid or '', user_id=user_id or 0)
        if ident['ambiguous'] or not ident['user_id'] and not ident['unionid'] and not ident['mp_openid'] and not phone:
            return 0.0
        if ident['user_id'] == 0:
            return 0.0
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
            'SELECT COALESCE(SUM(o.deposit_amount), 0) FROM orders o '
            'WHERE o.status = 3 AND (' + where + ') '
            'AND NOT EXISTS (SELECT 1 FROM withdrawal_records w WHERE w.order_id = o.id AND w.status IN (0, 1, 2))'
        )
        c.execute(sql, params)
        r = c.fetchone()
        return max(0.0, float(r[0] or 0))
    finally:
        try: conn.close()
        except: pass
