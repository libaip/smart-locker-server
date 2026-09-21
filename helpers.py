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
from wx_config import (h5_base as _wx_h5b, h5_store as _wx_h5s, oauth_callback as _wx_oauthcb,
                     ws_base as _wx_ws, pay_notify_url as _wx_payurl)   # [CFG-STEP2C] 域名改从配置中心读，读不到自动用 config.py 原值
from wx_config import mp_openid_prefix, oa_openid_prefix   # [CFG-STEP2D] openid 前缀改成跟着当前生效账号走（缺省仍是 ooTcRx / oLhbm2）
from wx_config import (mp_appid as _wx_mp_id, mp_secret as _wx_mp_secret,
                     oa_appid as _wx_oa_id, oa_secret as _wx_oa_secret)   # [CFG-STEP2B] 账号凭据改从配置中心读，读不到自动用 config.py 原值
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
    return bool(v) and str(v).startswith(mp_openid_prefix())


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


def is_alipay_browser():
    """[S255] 是否在支付宝客户端内置浏览器中"""
    from flask import request
    user_agent = request.headers.get('User-Agent', '')
    return 'AlipayClient' in user_agent


def get_alipay_mp_client():
    """[S272] 支付宝【小程序应用】的客户端（登录 / 订阅消息用）

    与支付通道无关：支付宝小程序是独立应用，用它自己的 appid + 密钥。
    密钥文件：cert/alipay_mp_private_key.pem、cert/alipay_mp_alipay_public_key.pem
    appid 可用环境变量 ALIPAY_MP_APPID 覆盖。
    """
    import os
    from alipay import AlipayClient, PROD_GATEWAY, SANDBOX_GATEWAY
    appid = (os.environ.get('ALIPAY_MP_APPID') or '').strip() or '2021006199688688'
    cert_dir = os.environ.get('SMART_LOCKER_CERT_DIR') or '/home/ubuntu/smart-locker/cert'
    priv_path = os.path.join(cert_dir, 'alipay_mp_private_key.pem')
    pub_path = os.path.join(cert_dir, 'alipay_mp_alipay_public_key.pem')
    if not (os.path.exists(priv_path) and os.path.exists(pub_path)):
        logger.error('[get_alipay_mp_client] 密钥文件不存在: %s / %s' % (priv_path, pub_path))
        return None
    try:
        priv = open(priv_path, 'r').read()
        pub = open(pub_path, 'r').read()
    except Exception as e:
        logger.error('[get_alipay_mp_client] 读密钥失败: %s' % (e,))
        return None
    gw = SANDBOX_GATEWAY if str(appid).startswith('9021') else PROD_GATEWAY
    return AlipayClient(app_id=appid, private_key=priv, alipay_public_key=pub, gateway=gw)


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
def _get_payment_channel(channel_id=None, exclude_channel_id=None, channel_type=None):
    """获取支付渠道（支持严格轮转和加权随机）

    [S317] channel_type: 限定渠道类型（'wechat' / 'alipay'）。
           微信支付和支付宝支付【绝不能相互轮询】—— 选错类型会直接导致付款失败。
           传 None = 不限定（兼容旧调用）。
    """
    conn = get_db()
    cursor = conn.cursor()
    if channel_id:
        cursor.execute('SELECT * FROM payment_channels WHERE id = %s', (channel_id,))
        ch = cursor.fetchone()
        conn.close()
        return dict(ch) if ch else None
    cursor.execute('SELECT * FROM payment_channels WHERE is_active = 1')
    channels = cursor.fetchall()
    # [S317] 先按渠道类型过滤（channel_type 为空的历史数据按 wechat 处理）
    if channel_type:
        channels = [ch for ch in channels if (ch.get('channel_type') or 'wechat') == channel_type]
        if not channels:
            conn.close()
            logger.warning('[channel] 没有可用的 %s 渠道', channel_type)
            return None
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
            cursor.execute('SELECT * FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) AND id != %s ORDER BY rotation_index ASC', (exclude_channel_id,))
        else:
            cursor.execute('SELECT * FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) ORDER BY rotation_index ASC')
        _seq_rows = cursor.fetchall()
        # [S317] 按渠道类型挑第一个（原来 SQL 带 LIMIT 1，会越过类型过滤）
        ch = None
        for _r in _seq_rows:
            if not channel_type or ((_r.get('channel_type') or 'wechat') == channel_type):
                ch = _r
                break
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


def select_payment_channel(exclude_channel_id=None, channel_type=None):
    """选择支付渠道（加权随机轮换）
    exclude_channel_id: 排除的渠道ID，用于故障切换时跳过当前失败的渠道
    [S317] channel_type: 限定渠道类型（'wechat'/'alipay'），不传=不限定
    """
    return _get_payment_channel(exclude_channel_id=exclude_channel_id, channel_type=channel_type)


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


def get_channel_wxpay(channel, use_mp_appid=False, openid=None, acct_type=None):
    """根据渠道配置创建支付实例"""
    from wxpay import WxPay, ThirdPartyPay as TPP
    channel_type = channel.get('channel_type', 'wechat')
    if channel_type == 'wechat':
        # [S357] 谁付钱由【这笔支付的 openid】决定：前缀 = 公众号的用公众号 appid，
        #   前缀 = 小程序的用小程序 appid。一条通道行只存一个 app_id，降级为【最后兜底】；
        #   判断不出来（openid 为空 / 前缀没登记 / 读不到库）时行为与改动前完全一致。
        app_id = (appid_by_openid(openid, acct_type=acct_type) or channel.get('app_id')
                  or (_wx_mp_id() if use_mp_appid else _wx_oa_id()))
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
                    notify_url=_wx_payurl().replace('/api/pay/notify', '/api/pay/notify/third-party'),
                    return_url=extra.get('return_url', '')), 'third_party'
    elif channel_type == 'alipay':
        # [S255] 支付宝（手机网站支付）
        from alipay import AlipayClient, PROD_GATEWAY, SANDBOX_GATEWAY
        try:
            extra = json.loads(channel.get('extra_config') or '{}')
        except Exception:
            extra = {}
        cert_name = channel.get('cert_name') or channel.get('app_id') or str(channel.get('mch_id') or '')
        priv_path = extra.get('private_key_path') or '/home/ubuntu/smart-locker/cert/%s_private_key.pem' % cert_name
        pub_path = extra.get('alipay_public_key_path') or '/home/ubuntu/smart-locker/cert/%s_alipay_public_key.pem' % cert_name
        try:
            _priv = open(priv_path, encoding='utf-8').read()
        except Exception as _e:
            logger.error('[支付宝] 私钥读取失败 %s: %s', priv_path, _e)
            return None, 'alipay'
        _pub = ''
        try:
            _pub = open(pub_path, encoding='utf-8').read()
        except Exception as _e:
            logger.warning('[支付宝] 支付宝公钥读取失败(回调将走查单核对) %s: %s', pub_path, _e)
        _appid = str(channel.get('app_id') or '')
        _gw = extra.get('gateway') or (SANDBOX_GATEWAY if _appid.startswith('9021') else PROD_GATEWAY)
        return AlipayClient(app_id=_appid, private_key=_priv, alipay_public_key=_pub, gateway=_gw,
                            notify_url=_wx_payurl().replace('/api/pay/notify', '/api/pay/notify/alipay'),
                            return_url=_wx_h5b() + '/store'), 'alipay'
    return None, None


def get_wxpay(use_mp_appid=False):
    """获取默认微信支付实例"""
    from wxpay import WxPay, MockWxPay
    mode = get_setting('pay_mode', 'mock')
    if mode == 'mock':
        return MockWxPay()
    app_id = _wx_mp_id() if use_mp_appid else _wx_oa_id()
    return WxPay(mch_id=WX_MCH_ID, api_key=WX_API_KEY, app_id=app_id,
                 cert_path=WX_CERT_PATH, key_path=WX_KEY_PATH)


# ============================================
# [S531-20260921] 商户号被封(收款受限)告警
# ============================================
# 背景(为什么原来的"商户号被封通知"是坏的):
#   1) 主动巡检 check_merchant_health() 用 order_query 判活, 而微信对"收款功能受限"
#      是在【下单 unifiedorder】上才返回 NOAUTH / err_code_des
#      "此商家的收款功能已被限制，暂无法支付"。实测 2026-09-21: 109/109 个微信渠道
#      order_query 全部 return_code=SUCCESS -> 探不到封号。
#   2) _MERCHANT_ERROR_CODES 明确把 NOAUTH 排除在外, 所以真封号时只走
#      logger.warning("[渠道] 商户收款受限(不禁用)，切换重试") 然后切渠道, 从不告警。
#   生产实证: 2026-09-21 16:04:04 商户号 1000688402 被封, 16:04:04/13/18 连续 3 次
#   NOAUTH, 全程 0 条通知。
# 本函数只在"已无任何可用微信渠道"时告警, 避免正常轮转时打扰。
# 去重靠 DB(system_settings 一条 key), 跨 8 个 worker/跨进程有效。
_MCH_RESTRICTED_ALERT_KEY = '[S531]mch_restricted_alert'
_MCH_RESTRICTED_ALERT_GAP = 600      # 同渠道 10 分钟内只告警一次


def _alert_mch_restricted(channel, err_code, err_desc):
    """微信回 NOAUTH/收款受限 -> 若无其他可用微信渠道则告警(不产生任何支付副作用)"""
    import json as _json
    import time as _time
    try:
        cid = (channel or {}).get('id')
        cname = (channel or {}).get('name', '未知')
        cmch = (channel or {}).get('mch_id', '未知')

        from database import get_db
        conn = get_db()
        cur = conn.cursor()

        # 还有别的活跃微信渠道可用吗? 有就只是常规轮转, 不打扰
        cur.execute("SELECT count(*) FROM payment_channels "
                    "WHERE is_active=1 AND channel_type='wechat' AND id<>%s", (cid,))
        _r = cur.fetchone()
        alive = (_r[0] if not isinstance(_r, dict) else list(_r.values())[0]) if _r else 0
        if alive and alive > 0:
            conn.close()
            return False

        # 去重: 同渠道 10 分钟内只告警一次
        cur.execute("SELECT setting_value FROM system_settings WHERE setting_key=%s",
                    (_MCH_RESTRICTED_ALERT_KEY,))
        row = cur.fetchone()
        st = {}
        if row:
            raw = row.get('setting_value') if isinstance(row, dict) else row[0]
            try:
                st = _json.loads(raw or '{}') or {}
            except Exception:
                st = {}
        now = int(_time.time())
        last = int((st.get(str(cid)) or {}).get('ts') or 0) if isinstance(st.get(str(cid)), dict) else 0
        if now - last < _MCH_RESTRICTED_ALERT_GAP:
            logger.info('[MchRestricted] 渠道 %s 10分钟内已告警过, 跳过' % cid)
            conn.close()
            return False

        title = '【寄存柜】微信商户号被封/收款受限'
        content = ('微信支付商户号被限制收款，且当前已无其他可用微信渠道。\n'
                   '商户名称: %s\n'
                   '商户号(mch_id): %s\n'
                   '渠道ID: %s\n'
                   '错误码: %s\n'
                   '错误描述: %s\n'
                   '\n请立刻登录 pay.weixin.qq.com 查看，并到后台"支付渠道"启用备用商户号。') % (
            cname, cmch, cid, err_code, err_desc)
        ok = send_pushplus(title, content)
        st[str(cid)] = {'ts': now, 'name': cname, 'mch': cmch, 'err': err_code, 'desc': err_desc}
        try:
            cur.execute(
                "INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s) "
                "ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                (_MCH_RESTRICTED_ALERT_KEY, _json.dumps(st, ensure_ascii=False)))
            conn.commit()
        except Exception as _we:
            logger.warning('[MchRestricted] 告警状态写库失败: %s' % _we)
        conn.close()
        logger.warning('[MchRestricted] 告警已发(%s): %s' % ('成功' if ok else '失败', title))
        return ok
    except Exception as e:
        logger.error('[MchRestricted] 告警失败: %s' % e)
        return False


_mch_fail_poll_count = {}


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
                scene_info = json.dumps({'type': 'Wap', 'wap_url': _wx_h5b(), 'wap_name': '智能寄存柜'})
        else:
            scene_info = json.dumps({'type': 'Wap', 'wap_url': _wx_h5b(), 'wap_name': '智能寄存柜'})
    else:
        scene_info = json.dumps({'type': 'Wap', 'wap_url': _wx_h5b(), 'wap_name': '智能寄存柜'})

    if openid:
        trade_type = 'JSAPI'
    # 使用支付渠道
    if payment_channel_id:
        ch = _get_payment_channel(payment_channel_id)
        current_channel = ch or payment_channel
    elif payment_channel:
        current_channel = payment_channel
    else:
        # [S317] 这里是要【发起微信支付】，必须只在 wechat 渠道里选，绝不能选到支付宝渠道
        # [S512b-20260921] 例外：在【支付宝内置浏览器】里必须挑支付宝通道。
        #   否则支付宝用户永远拿到的是微信通道 -> 下面 ch_type=='alipay' 那段成了死代码，
        #   用户在支付宝里根本付不了钱。支付宝通道不存在时回退微信通道（保持原行为）。
        if is_alipay_browser():
            current_channel = _get_payment_channel(channel_type='alipay')
            if current_channel:
                logger.info('[支付宝] 支付宝浏览器：选用支付宝通道 id=%s appid=%s',
                            current_channel.get('id'), current_channel.get('app_id'))
            else:
                logger.warning('[支付宝] 支付宝浏览器但没有可用的支付宝通道，回退微信通道')
                current_channel = _get_payment_channel(channel_type='wechat')
        else:
            current_channel = _get_payment_channel(channel_type='wechat')  # 自动选活跃的微信渠道

    if current_channel:
        wxpay, ch_type = get_channel_wxpay(current_channel, use_mp_appid=False, openid=openid)
        if ch_type == 'third_party' and wxpay:
            third_party_type = 'alipay' if not is_wechat_browser() else 'wechat'
            result = wxpay.unifiedorder(trade_type=third_party_type, body='使用储物柜预付款',
                                         total_fee=int(deposit_amount * 100), out_trade_no=order_no)
            if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
                # 更新渠道统计（用于轮转）
                if current_channel:
                    update_channel_stats(current_channel['id'], deposit_amount)
                return {'mode': 'third_party', 'channel_type': third_party_type, 'order_id': order_id,
                        'order_no': order_no, 'pay_url': result.get('url', ''), 'url_qrcode': result.get('url_qrcode', '')}
            return {'mode': 'error', 'error_msg': result.get('return_msg', '第三方下单失败')}
        if ch_type == 'alipay' and wxpay:
            # [S255] 支付宝手机网站支付：生成跳转链接与自动提交表单（无需预下单接口）
            try:
                _pay = wxpay.wap_pay(out_trade_no=order_no, total_amount=deposit_amount,
                                     subject='储物柜预付款', quit_url=_wx_h5b() + '/store')
            except Exception as _e:
                logger.error('[支付宝] 下单失败: %s', _e)
                return {'mode': 'error', 'error_msg': '支付宝下单失败'}
            try:
                from database import get_db as _gdb4
                _db4 = _gdb4()
                _db4.execute("UPDATE orders SET payment_channel_id=%s WHERE id=%s", (current_channel['id'], order_id))
                _db4.commit()
                _db4.close()
            except Exception as _e:
                logger.error('[支付宝渠道更新] 失败: %s', _e)
            if current_channel:
                update_channel_stats(current_channel['id'], deposit_amount)
            logger.info('[支付宝] 已生成支付跳转: order=%s channel=%s', order_no, current_channel.get('name'))
            return {'mode': 'alipay', 'order_id': order_id, 'order_no': order_no,
                    'pay_url': _pay.get('url', ''), 'form': _pay.get('form', '')}
        if wxpay is None:
            return {'mode': 'error', 'error_msg': '支付渠道配置异常'}
    else:
        return {'mode': 'error', 'error_msg': '无可用活跃商户，请联系管理员'}

    total_fee = int(deposit_amount * 100)
    time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')

    result = wxpay.unifiedorder(trade_type=trade_type, body='使用储物柜预付款',
                                 total_fee=total_fee, out_trade_no=order_no,
                                 notify_url=_wx_payurl(), openid=openid,
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
    # [S415-20260921] APPID_MCHID_NOT_MATCH 移出"商户死亡"名单：
    #   它的含义是"这笔单的付款人身份(appid)跟这个商户不搭"，不是"商户坏了"。
    #   2026-09-21 生产实例：老身份进来下单 -> 微信回 APPID_MCHID_NOT_MATCH ->
    #   一句话就把当时唯一在用的商户 118 停掉 -> 全站支付挂了 3 次(08:00/08:18/08:19)。
    #   现在它跟 NOAUTH 一样：只换渠道重试、不停商户（真死的商户仍会被停）。
    _dead_errors = {'MCH_NOT_EXIST', 'ACCOUNT_ERROR', 'BANK_ERROR'}
    _skip_errors = {'NOAUTH', 'NO_AUTH', 'APPID_MCHID_NOT_MATCH'}  # 收款受限，切换重试但不永久禁用
    _err_code = result.get('err_code', '')
    # [S531-20260921] 失败轮询: 用户重扫码时前端会再调一次, 靠内存计数代替自递归,
    #   否则 _retry_count 恒为 0 会无限重试, 永远到不了"无渠道可用"的告警分支。
    _pfx = '%s' % (order_no or order_id or '')
    _mch_fail_poll_count[_pfx] = int(_mch_fail_poll_count.get(_pfx, 0) or 0) + 1
    _poll = _mch_fail_poll_count[_pfx] - 1
    if current_channel and _poll < 3 and (_err_code in _dead_errors or _err_code in _skip_errors):
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
            # [S531-20260921] 原来的"商户号被封通知"就断在这里: 只切渠道、从不告警。
            #   现在若无其他可用微信渠道, 立刻推送给管理员(带 10 分钟去重)。
            _alert_mch_restricted(current_channel, result.get('err_code'),
                                  result.get('err_code_des') or result.get('return_msg', ''))
        next_ch = select_payment_channel(exclude_channel_id=current_channel['id'])
        if next_ch and next_ch.get('id') and next_ch['id'] != current_channel['id']:
            logger.info(f'[渠道] 切换到下一个渠道重试: {next_ch["name"]}')
            # [已修复] 不再修改订单的payment_channel_id，让用户重新扫码
            # 原因：用户扫码时是商户A，如果系统偷偷换成商户B，支付回调时会找不到订单
            logger.warning(f'[渠道] 商户异常，需要用户重新扫码。不修改订单#{order_id}的payment_channel_id')
            return get_payment_params(order_id, order_no, deposit_amount, user_phone, openid, payment_channel=next_ch, payment_channel_id=next_ch['id'], _retry_count=_poll+1)
    
    _mch_fail_poll_count.pop(_pfx, None)
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


# ============================================
# [S319] 小程序支付参数（微信 JSAPI for 小程序）
# ============================================
def _mp_pick_wechat_channel(channel_id=None):
    """只挑 channel_type='wechat' 的通道；挑不到返回 None。

    故意不复用 select_payment_channel() 的自动选择：那个会连支付宝通道一起轮询，
    而【小程序 JSAPI 支付必须用微信通道】。兼容两版 helpers：
      · 175 版：_get_payment_channel(channel_id, exclude_channel_id, channel_type)
      · 106 版：_get_payment_channel(channel_id, exclude_channel_id)  ← 没有 channel_type
    """
    if channel_id:
        _ch = _get_payment_channel(channel_id)
        if _ch and (_ch.get('channel_type') or 'wechat') == 'wechat':
            return _ch
        return None
    _ch = None
    try:
        _ch = _get_payment_channel(channel_type='wechat')
    except TypeError:
        _ch = None   # 老版本没有 channel_type 参数
    except Exception as _e:
        logger.error('[mp-jsapi] 选微信通道异常: %s', _e)
        _ch = None
    if _ch and (_ch.get('channel_type') or 'wechat') == 'wechat':
        return _ch
    # 老版本兜底：自己查库挑第一个活跃微信通道（按 rotation_index 顺序，与 sequential 模式一致）
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payment_channels WHERE is_active=1 AND (auto_disabled IS NULL OR auto_disabled=0) ORDER BY rotation_index ASC, id ASC")
        rows = cursor.fetchall()
        conn.close()
        for _r in rows:
            if (_r.get('channel_type') or 'wechat') == 'wechat':
                return dict(_r)
    except Exception as _e:
        logger.error('[mp-jsapi] 查库挑微信通道失败: %s', _e)
    return None


def _mp_openid_prefix_of(app_id):
    """取某个 appid 对应的 openid 前缀（wx_accounts.openid_prefix），取不到返回 ''。

    注意：不能走 wx_config.resolve_by_appid() —— 它返回的字典里没有 openid_prefix
    （只有 appid/secret/token/aes_key/name/account_id/subject/source/mch_relation），
    照那样写会永远拿到 ''，等于把 openid 校验静默关掉。这里直接查库。
    """
    app_id = (app_id or '').strip()
    if not app_id:
        return ''
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT openid_prefix FROM wx_accounts WHERE appid=%s LIMIT 1", (app_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row.get('openid_prefix'):
            return row['openid_prefix']
        logger.warning('[mp-jsapi] wx_accounts 里没查到 appid=%s 的 openid_prefix，跳过前缀校验', app_id)
        return ''
    except Exception as _e:
        logger.error('[mp-jsapi] 取 openid 前缀失败: %s', _e)
        return ''


# ============================================================
# [S357] 支付 appid 按 openid 前缀动态取
# ============================================================
# 问题：一条通道行只存一个 app_id，但
#   · 小程序内支付 要用【小程序】的 appid
#   · H5/公众号支付 要用【公众号】的 appid
# 两者必然冲突。原来 get_channel_wxpay() 写的是
#   app_id = channel.get('app_id') or 当前生效账号
# 「通道行里一填了 app_id 就强制用它」-> 谁付钱都用通道那个 appid，
# 于是 openid 属于另一个号时微信直接 PARAM_ERROR(appid和openid不匹配)。
#
# 改成：谁付钱由【这笔支付的 openid】决定，openid 前缀 -> 对应账号的 appid。
#   通道行的 app_id 降级为【最后兜底】；当前生效账号兜底不变。
# 判断不出来（openid 为空 / 前缀没登记 / 前缀不属于要求的 acct_type / 读不到库）
#   -> 返回 ''，调用方回落原行为（老路径一字不变）。
_APPID_BY_PREFIX_CACHE = {'ts': 0.0, 'rows': None}
_APPID_BY_PREFIX_TTL = 60


def _appid_rows():
    """wx_accounts 里所有「有 openid_prefix 且有可用 appid」的账号：(prefix, appid, name, acct_type)。

    读不到库 -> 返回 []，调用方回落到原行为。60 秒缓存，避免支付热路径每次都查库。

    ⚠️ 这里【故意不 conn.close()】：get_db() 在请求上下文里返回的是 flask.g 复用的
       连接，close() 会把这条连接 putconn 归还池子，而调用链上别处可能还持有同一个
       conn 对象在用 -> 归还后另一线程可能同时拿到同一条连接 = 串号/竞态。
       请求结束由 teardown 统一回收；非请求上下文（脚本/巡检）最多每 60 秒漏 1 条，可忽略。
    """
    now = time.time()
    cached = _APPID_BY_PREFIX_CACHE.get('rows')
    if cached is not None and (now - _APPID_BY_PREFIX_CACHE.get('ts', 0.0)) < _APPID_BY_PREFIX_TTL:
        return cached
    rows = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        # [S415-20260921] 已停用的公众号(oa)不再参与"按 openid 取 appid"：
        #   它的 appid 跟现在在用的商户没有绑定，拿来下单必然 APPID_MCHID_NOT_MATCH。
        #   （2026-09-21 生产实例：老公众号 oLhbm2 的 appid 被拿去给卓蓝时商户下单）
        cursor.execute("SELECT appid, openid_prefix, name, acct_type FROM wx_accounts "
                       "WHERE COALESCE(openid_prefix,'') <> '' AND COALESCE(appid,'') <> '' "
                       "AND NOT (COALESCE(acct_type,'') = 'oa' AND COALESCE(is_active,0) = 0)")
        for r in cursor.fetchall():
            _p = str(r.get('openid_prefix') or '').strip()
            _a = str(r.get('appid') or '').strip()
            # 未登记的占位 appid（如 REPLACE_ME_MP_BACKUP）绝不能拿来下单
            if _p and _a and not _a.startswith('REPLACE_ME'):
                rows.append((_p, _a, r.get('name') or '', r.get('acct_type') or ''))
    except Exception as _e:
        logger.error('[S357] 取 openid 前缀->appid 映射失败，回落原逻辑: %s', _e)
        rows = []
    _APPID_BY_PREFIX_CACHE['rows'] = rows
    _APPID_BY_PREFIX_CACHE['ts'] = now
    return rows


def appid_by_openid(openid, acct_type=None):
    """[S357] 按 openid 前缀反查这笔支付该用哪个 appid。

    最长前缀优先；判断不出来一律返回 ''（调用方必须回落，保证老路径一字不变）。

    acct_type 给定时只认该类账号（mp=小程序 / oa=公众号）：
      · H5/公众号支付不传（mp、oa 都合法，H5 也在小程序 webview 里跑过）；
      · 小程序内支付传 'mp' —— 这样遇到公众号 openid 会返回 ''，回落通道 app_id 后
        由 get_mp_jsapi_params 的前缀校验报出改动前那句人话错误，而不是拿公众号 appid
        去统一下单（小程序前端 wx.requestPayment 根本拉不起来）。
    """
    _oid = str(openid or '').strip()
    if not _oid:
        return ''
    best_len = -1
    best = ''
    for _p, _a, _name, _t in _appid_rows():
        if acct_type and _t != acct_type:
            continue
        if _oid.startswith(_p) and len(_p) > best_len:
            best_len = len(_p)
            best = _a
    if best:
        logger.info('[S357] appid 按 openid 前缀取: openid=%s... acct_type=%s -> appid=%s',
                    _oid[:8], acct_type or '-', best)
    return best


# [S320] 多小程序身份隔离：新小程序只认 openid，不做"按手机号找回老账号"
# ============================================================
# 背景：老小程序(ooTcRx/科莱维)、公众号 与 新小程序(重庆清域智, oQXFs3) 在库里
#   共用 users / user_balances / phone_openids。老体系里同一个自然人的手机号/unionid
#   会通过"手机号 -> unionid -> users"这条桥把新小程序用户认成老账号
#   (实测：新 openid + 13667618419 -> 老 uid 97336)。
#   老板决策：新小程序不做老用户找回，新用户就是全新用户(余额 0、无老订单)。
#
# 判定原则（白名单式，绝不"看到陌生前缀就当新小程序"）：
#   1) 客户端带了 appid -> 只按 appid 判定：等于新小程序 appid 才是新体系；
#      其它已登记 appid 与 未登记 appid 一律按老体系处理(保持原行为)。
#   2) 没带 appid       -> 用 openid 前缀兜底：前缀不属于任何【已知老体系账号】
#      才算新体系。已知老体系前缀从 wx_accounts 实时取(除新小程序外的全部账号)，
#      取不到库时退回内置常量，宁可多算老前缀(不启用严格模式)，也不漏判。
#   3) 什么身份信息都没有 -> 返回 False(不改变任何现有行为)。
NEW_MP_APPID = 'wx0be09d4de1417e01'      # 新小程序(另一主体 重庆清域智)，见 wx_accounts.id=9
_NEW_MP_PREFIX_FALLBACK = 'oQXFs3'       # 新小程序 openid 前缀(2026-09-19 实测)，读不到库时兜底
_LEGACY_PREFIX_FALLBACK = ('ooTcRx', 'oWrA8', 'oLhbm2', 'ov47M3')
_legacy_prefix_cache = {'ts': 0.0, 'prefixes': None}
_new_prefix_cache = {'ts': 0.0, 'prefix': None}
_LEGACY_PREFIX_TTL = 300


def _active_mp_ident():
    """[S523-20260921] 当前【生效的小程序账号】(appid, openid_prefix)。

    为什么要有它：原来"新小程序"写死成清域智(wx0be09d4de1417e01/oQXFs3)，
      但老板 2026-09-21 起生效的小程序是卓蓝时(wx281a9540a6a5b64d/oXTD3x)，
      它的用户于是被判成"没有身份"（订单 user_id=0、看不到订单/余额）。
    取值：wx_accounts 里 is_active=1 的 mp 账号；读不到 -> 前缀读不到 -> 再回落写死常量。
    """
    appid, prefix = '', ''
    try:
        import wx_config as _wc523
        _acc523 = _wc523.get_effective_account('mp') or {}
        appid = str(_acc523.get('appid') or '').strip()
        prefix = str(_acc523.get('openid_prefix') or '').strip()
    except Exception as _e523:
        logger.warning('[S523] 取生效小程序账号失败，回落写死常量: %s', _e523)
    if not appid:
        appid = NEW_MP_APPID
    if not prefix:
        try:
            prefix = (_mp_openid_prefix_of(appid) or '').strip()
        except Exception:
            prefix = ''
    if not prefix:
        prefix = _NEW_MP_PREFIX_FALLBACK
    return appid, prefix


def legacy_openid_prefixes():
    """老体系(科莱维/景钧达)已知的 openid 前缀集合。

    = wx_accounts 里【除新小程序外的全部账号】的 openid_prefix，
      **同时包含 mp(小程序) 与 oa(公众号)** —— 故意不加 acct_type 过滤：
      mp 给 ooTcRx(老小程序)/oWrA8(科莱智)，oa 给 oLhbm2(智能寄存柜)/ov47M3(景钧达)。
      客户端会把公众号 openid(oLhbm2…/ov47M3…)一起带上来，那正是老账号 users.openid 的值，
      必须算作"老体系"，否则新小程序的 strict 判定会漏。
    读不到库时用内置常量兜底（宁可多算老前缀→不启用严格模式，也不漏判）。
    """
    now = time.time()
    cached = _legacy_prefix_cache.get('prefixes')
    if cached is not None and (now - _legacy_prefix_cache.get('ts', 0.0)) < _LEGACY_PREFIX_TTL:
        return cached
    out = set(_LEGACY_PREFIX_FALLBACK)
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 注意：这里【不能】加 acct_type 过滤 —— mp + oa 的 prefix 都要算老体系。
        # [S523-20260921] 排除的不能只有写死的清域智：当前生效的小程序(卓蓝时)同样属于"新体系"，
        #   否则它会被算成老体系、隔离逻辑反向。读不到生效账号时只排除写死 appid（老行为）。
        _act_appid_523 = ''
        try:
            _act_appid_523 = _active_mp_ident()[0] or ''
        except Exception:
            _act_appid_523 = ''
        cursor.execute(
            "SELECT DISTINCT openid_prefix FROM wx_accounts "
            "WHERE NULLIF(openid_prefix,'') IS NOT NULL AND appid <> %s AND appid <> %s",
            (NEW_MP_APPID, _act_appid_523 or NEW_MP_APPID))
        for row in cursor.fetchall():
            p = row['openid_prefix'] if isinstance(row, dict) else row[0]
            if p:
                out.add(p)
        conn.close()
    except Exception as _e:
        logger.warning('[S320] 取老体系前缀失败，用内置兜底: %s', _e)
    _legacy_prefix_cache['prefixes'] = out
    _legacy_prefix_cache['ts'] = now
    return out


def new_mp_openid_prefix():
    """新小程序自己的 mp openid 前缀 —— strict 模式下【唯一被承认】的身份前缀。"""
    now = time.time()
    cached = _new_prefix_cache.get('prefix')
    if cached and (now - _new_prefix_cache.get('ts', 0.0)) < _LEGACY_PREFIX_TTL:
        return cached
    # [S523-20260921] 以当前生效的小程序账号为准（生效的是卓蓝时，不再是写死的清域智）
    try:
        p = _active_mp_ident()[1] or ''
    except Exception:
        p = ''
    if not p:
        p = _NEW_MP_PREFIX_FALLBACK
    _new_prefix_cache['prefix'] = p
    _new_prefix_cache['ts'] = now
    return p


def is_new_mp_identity(appid='', openid='', mp_openid=''):
    """[S320] 本次身份是否属于"新小程序"(需要只认 openid、不按手机号找老用户)。

    只有能【确定】不是老体系时才返回 True；无法判断一律 False(保持原行为)。
    """
    _appid = (appid or '').strip()
    # [S523-20260921] 当前生效的小程序账号同样属于"新体系"
    try:
        _act_appid_523b, _act_prefix_523b = _active_mp_ident()
    except Exception:
        _act_appid_523b, _act_prefix_523b = NEW_MP_APPID, _NEW_MP_PREFIX_FALLBACK
    if _appid:
        # 客户端带了 appid：只信 appid，不做前缀猜测
        if _appid == NEW_MP_APPID or _appid == _act_appid_523b:
            return True
        _p = _mp_openid_prefix_of(_appid)
        if _p:
            return _p not in legacy_openid_prefixes()
        return False        # 未登记的 appid -> 不认识 -> 不改变行为
    _oid = (openid or mp_openid or '').strip()
    if not _oid:
        return False
    # [S523] 前缀 == 当前生效小程序 -> 新体系（先于老体系判断）
    if _act_prefix_523b and _oid.startswith(_act_prefix_523b):
        return True
    for _p in legacy_openid_prefixes():
        if _p and _oid.startswith(_p):
            return False
    logger.warning('[S320] 未登记的 openid 前缀，按新小程序隔离处理: %s...', _oid[:8])
    return True


def get_mp_jsapi_params(order_id, order_no, amount, mp_openid,
                        payment_channel_id=None, body='使用储物柜预付款'):
    """[S319] 取【微信小程序】wx.requestPayment 需要的支付参数（统一下单 JSAPI + 签名）。

    为什么不复用 get_payment_params()：
      那个函数是给 H5 用的，里面有 UA 嗅探（is_mobile_browser / is_wechat_browser）和
      MWEB / H5 / 第三方 / 支付宝 一堆分支。小程序支付要的是**确定的一次 JSAPI 下单**，
      且绝不能选到支付宝通道 —— 所以单独一条函数，H5 的既有行为一个字都不动。

    返回 dict：
      成功    {'ok': True,  'mode': 'jsapi', 'timeStamp','nonceStr','package','signType','paySign', ...}
      失败    {'ok': False, 'mode': 'error', 'error_msg': '...'}
      模拟支付 {'ok': True,  'mode': 'mock',  ...}（pay_mode=mock 时没有真实支付）
    """
    try:
        amount = float(amount or 0)
        mp_openid = (mp_openid or '').strip()
        if amount <= 0:
            return {'ok': False, 'mode': 'error', 'error_msg': '订单金额异常，无法支付'}
        if not mp_openid:
            return {'ok': False, 'mode': 'error', 'error_msg': '缺少小程序 openid，请先在小程序内登录'}

        total_fee = int(round(amount * 100))
        if is_mock_mode():
            return {'ok': True, 'mode': 'mock', 'order_id': order_id, 'order_no': order_no,
                    'total_fee': total_fee}

        channel = _mp_pick_wechat_channel(payment_channel_id)
        if not channel and payment_channel_id:
            # 订单挂的渠道不是可用微信通道（真实存在：orders 134273/134274 挂的是支付宝 113）：
            # 不报错，记一条告警改选活跃微信通道，否则用户直接付不了钱。
            logger.warning('[mp-jsapi] 订单渠道 %s 不是可用微信通道，改选活跃微信通道 order=%s',
                           payment_channel_id, order_no)
            channel = _mp_pick_wechat_channel(None)
        if not channel:
            logger.error('[mp-jsapi] 无可用微信通道 order=%s 指定渠道=%s', order_no, payment_channel_id)
            return {'ok': False, 'mode': 'error', 'error_msg': '无可用微信支付商户，请联系管理员'}

        wxpay, ch_type = get_channel_wxpay(channel, use_mp_appid=False,
                                          openid=mp_openid, acct_type='mp')
        if wxpay is None or ch_type != 'wechat':
            logger.error('[mp-jsapi] 微信通道实例化失败 channel=%s type=%s', channel.get('id'), ch_type)
            return {'ok': False, 'mode': 'error', 'error_msg': '微信支付渠道配置异常'}

        # openid 必须是【这个 appid 的】小程序 openid（传公众号 openid 会 OPENID_MISMATCH）
        _prefix = _mp_openid_prefix_of(wxpay.app_id)
        if _prefix and not mp_openid.startswith(_prefix):
            logger.error('[mp-jsapi] openid 与支付 appid 不匹配: appid=%s expect_prefix=%s got=%s...',
                         wxpay.app_id, _prefix, mp_openid[:8])
            return {'ok': False, 'mode': 'error',
                    'error_msg': '支付账号不匹配：请用当前小程序登录后再试（openid 应以 %s 开头）' % _prefix}

        time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')
        result = wxpay.unifiedorder(trade_type='JSAPI', body=body,
                                    total_fee=total_fee, out_trade_no=order_no,
                                    notify_url=_wx_payurl(), openid=mp_openid,
                                    scene_info=None, time_expire=time_expire)
        if not (result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS'):
            logger.error('[mp-jsapi] 统一下单失败 order=%s channel=%s ret=%s/%s err=%s/%s',
                         order_no, channel.get('id'), result.get('return_code'), result.get('return_msg'),
                         result.get('err_code'), result.get('err_code_des'))
            # 故意【不】自动禁用商户：H5 那套遇到 MCH_NOT_EXIST 会顺手 is_active=0，
            # 而小程序支付当前只有 114 一个通道，误禁用会让全站无法收款。只记日志，人工处理。
            return {'ok': False, 'mode': 'error',
                    'error_msg': result.get('err_code_des') or result.get('return_msg') or '微信下单失败'}

        prepay_id = result.get('prepay_id')
        # [S319] 这里【故意不做任何写库】。原本设想是把订单的收款渠道写成真正下单的这个
        #   通道（支付回调要按 orders.payment_channel_id 取密钥验签），但按老板要求：
        #   先保持只读，等小程序端到端确认链路 OK 之后再开回写，避免探测期污染真实订单。
        #   要开回写时，把下面这段只读检查换成：
        #     UPDATE orders SET payment_channel_id=<channel['id']> WHERE id=<order_id>
        if order_id:
            try:
                from database import get_db as _gdbmp
                _dbc = _gdbmp()
                _curc = _dbc.cursor()
                _curc.execute('SELECT payment_channel_id FROM orders WHERE id=%s', (order_id,))
                _rowc = _curc.fetchone()
                _dbc.close()
                _old_ch = _rowc.get('payment_channel_id') if _rowc else None
                if _rowc and _old_ch != channel['id']:
                    logger.warning('[mp-jsapi] 订单渠道与下单渠道不一致(当前未回写): order=%s 订单=%s 下单=%s',
                                   order_id, _old_ch, channel['id'])
            except Exception as _e:
                logger.error('[mp-jsapi] 渠道一致性检查失败: %s', _e)

        jsapi = wxpay.get_jsapi_params(prepay_id) or {}
        out = {'ok': True, 'mode': 'jsapi', 'order_id': order_id, 'order_no': order_no,
               'total_fee': total_fee, 'prepay_id': prepay_id, 'channel_id': channel['id']}
        for _k in ('appId', 'timeStamp', 'nonceStr', 'package', 'signType', 'paySign'):
            if jsapi.get(_k) is not None:
                out[_k] = jsapi.get(_k)
        logger.info('[mp-jsapi] 下单成功 order=%s channel=%s openid=%s...',
                    order_no, channel.get('id'), mp_openid[:8])
        return out
    except Exception as _e:
        logger.error('[mp_jsapi_params] 异常: %s', _e)
        return {'ok': False, 'mode': 'error', 'error_msg': '获取支付参数异常，请重试'}


# ============================================
# [S334] 支付宝小程序支付参数（alipay.trade.create → 前端 my.tradePay）
# ============================================
def _mp_pick_alipay_channel(channel_id=None):
    """只挑 channel_type='alipay' 的通道；挑不到返回 None。

    为什么单独一条：微信/支付宝通道【绝不能相互轮询】（选错类型直接付款失败），
    而 select_payment_channel() 会连微信通道一起算。

    现状（2026-09-19）：payment_channels.id=113（app_id=2021006199688688，cert_name=alipay_mp）
    就是本项目的支付宝【小程序应用】，但 is_active=0 —— 老板要求先别改库。而
    _get_payment_channel(channel_type='alipay') 只认 is_active=1，所以这里：
      1) 先按官方口径选【活跃】的支付宝通道；
      2) 一个都没有时，退回查库拿未启用的支付宝通道（只为把链路先跑通，日志明确告警）；
         一旦老板把 113 置成 is_active=1，第 2 步就永远不会触发。
    """
    if channel_id:
        _ch = _get_payment_channel(channel_id)
        if _ch and (_ch.get('channel_type') or '') == 'alipay':
            return _ch
        return None
    _ch = None
    try:
        _ch = _get_payment_channel(channel_type='alipay')
    except TypeError:
        _ch = None   # 老版本 helpers 没有 channel_type 参数
    except Exception as _e:
        logger.error('[alipay-mp] 选支付宝通道异常: %s', _e)
        _ch = None
    if _ch and (_ch.get('channel_type') or '') == 'alipay':
        return _ch
    # 兜底：查库直接找 alipay 通道（包含 is_active=0 的 113，仅告警不拦）
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payment_channels WHERE channel_type='alipay' "
                       "ORDER BY is_active DESC, auto_disabled ASC, rotation_index ASC, id ASC")
        rows = cursor.fetchall()
        conn.close()
        for _r in rows:
            if (_r.get('channel_type') or '') == 'alipay':
                logger.warning('[alipay-mp] 没有 is_active=1 的支付宝通道，退回使用未启用通道 id=%s '
                               '（老板要求暂不改库；置 is_active=1 后本告警消失）', _r.get('id'))
                return dict(_r)
    except Exception as _e:
        logger.error('[alipay-mp] 查库挑支付宝通道失败: %s', _e)
    return None


def get_alipay_mp_trade_params(order_id, order_no, amount, alipay_uid,
                               payment_channel_id=None, subject='储物柜预付款',
                               timeout_express='15m'):
    """[S334] 支付宝【小程序】支付：服务端 alipay.trade.create + 前端 my.tradePay({tradeNO})

    为什么不复用 get_payment_params()：那个是给 H5 用的（里面有 UA 嗅探 + wap_pay 表单），
    小程序要的是"确定的一次 alipay.trade.create"，并且只能选支付宝通道。

    返回 dict：
      成功      {'ok': True,  'mode': 'alipay_trade', 'trade_no': '2026...', ...}
      交易已付  {'ok': False, 'mode': 'paid',  'error_msg': '交易已被支付'}
      模拟支付  {'ok': True,  'mode': 'mock',  ...}（pay_mode=mock 时没有真实支付）
      失败      {'ok': False, 'mode': 'error', 'error_msg': '...'}
    """
    try:
        amount = float(amount or 0)
        alipay_uid = (alipay_uid or '').strip()
        if amount <= 0:
            return {'ok': False, 'mode': 'error', 'error_msg': '订单金额异常，无法支付'}
        if not alipay_uid:
            return {'ok': False, 'mode': 'error', 'error_msg': '缺少支付宝用户标识，请先在小程序内登录'}

        if is_mock_mode():
            return {'ok': True, 'mode': 'mock', 'order_id': order_id, 'order_no': order_no,
                    'total_fee': int(round(amount * 100))}

        channel = _mp_pick_alipay_channel(payment_channel_id)
        if not channel and payment_channel_id:
            # 订单挂的渠道不是支付宝（真实场景：store/init 走 select_payment_channel 选了微信 114）：
            # 不报错，记一条告警改选支付宝通道，否则用户付不了钱。
            logger.warning('[alipay-mp] 订单渠道 %s 不是可用支付宝通道，改选支付宝通道 order=%s',
                           payment_channel_id, order_no)
            channel = _mp_pick_alipay_channel(None)
        if not channel:
            logger.error('[alipay-mp] 无可用支付宝通道 order=%s 指定渠道=%s', order_no, payment_channel_id)
            return {'ok': False, 'mode': 'error', 'error_msg': '无可用支付宝商户，请联系管理员'}

        client, ch_type = get_channel_wxpay(channel)
        if client is None or ch_type != 'alipay':
            logger.error('[alipay-mp] 支付宝通道实例化失败 channel=%s type=%s', channel.get('id'), ch_type)
            return {'ok': False, 'mode': 'error', 'error_msg': '支付宝渠道配置异常'}

        resp = client.trade_create(out_trade_no=order_no, total_amount=amount,
                                   subject=subject, buyer_id=alipay_uid,
                                   timeout_express=timeout_express)
        if str(resp.get('code')) != '10000':
            sub_code = str(resp.get('sub_code') or '')
            sub_msg = str(resp.get('sub_msg') or resp.get('msg') or '')
            logger.error('[alipay-mp] trade.create 失败 order=%s channel=%s sub_code=%s sub_msg=%s',
                         order_no, channel.get('id'), sub_code, sub_msg)
            if sub_code in ('ACQ.TRADE_HAS_SUCCESS', 'ACQ.TRADE_STATUS_ERROR'):
                return {'ok': False, 'mode': 'paid', 'error_msg': '交易已被支付'}
            return {'ok': False, 'mode': 'error', 'error_msg': sub_msg or '支付宝下单失败'}

        trade_no = str(resp.get('trade_no') or '').strip()
        if not trade_no:
            logger.error('[alipay-mp] trade.create 未返回 trade_no order=%s resp=%s',
                         order_no, str(resp.get('_raw_body'))[:300])
            return {'ok': False, 'mode': 'error', 'error_msg': '支付宝未返回交易号，请重试'}

        # 回写订单收款渠道：支付宝异步通知 /api/pay/notify/alipay 要按 orders.payment_channel_id
        #   取密钥验签。不回写时订单挂的是微信通道，回调会拿到微信实例 → 直接 return fail。
        #   与 H5 支付宝分支（get_payment_params 里 UPDATE orders SET payment_channel_id）同一口径。
        if order_id:
            try:
                from database import get_db as _gdbap
                _dbap = _gdbap()
                _curap = _dbap.cursor()
                _curap.execute('UPDATE orders SET payment_channel_id=%s WHERE id=%s',
                               (channel['id'], order_id))
                _dbap.commit()
                _dbap.close()
            except Exception as _e:
                logger.error('[alipay-mp] 回写订单渠道失败: %s', _e)

        logger.info('[alipay-mp] trade.create 成功 order=%s channel=%s trade_no=%s buyer=%s...',
                    order_no, channel.get('id'), trade_no, alipay_uid[:8])
        return {'ok': True, 'mode': 'alipay_trade', 'order_id': order_id, 'order_no': order_no,
                'trade_no': trade_no,
                'out_trade_no': str(resp.get('out_trade_no') or order_no),
                'total_amount': '%.2f' % amount, 'total_fee': int(round(amount * 100)),
                'channel_id': channel['id']}
    except Exception as _e:
        logger.error('[get_alipay_mp_trade_params] 异常: %s', _e)
        return {'ok': False, 'mode': 'error', 'error_msg': '获取支付参数异常，请重试'}


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


# ============================================================
# [S521-20260921] 身份改造：新数据按平台 id 认人
#   小程序只看 mp_openid / 公众号只看 openid / 支付宝只看 alipay_uid
#   手机号不再参与认人（只当联系方式）
#   开关 system_settings.identity_strict_mode: off(默认=老逻辑) / new_order / all
#   阶段①只落地函数与开关，不接任何调用点 —— 线上行为零变化
# ============================================================
_IDENT_COL = {'mp_openid': 'mp_openid', 'oa_openid': 'openid', 'alipay_uid': 'alipay_uid'}


def identity_strict_mode():
    """[S521] 身份改造开关；任何异常/非法值一律回落 'off'（等同老逻辑）。"""
    try:
        v = str(get_setting('identity_strict_mode', 'off') or 'off').strip().lower()
    except Exception:
        v = 'off'
    return v if v in ('off', 'new_order', 'all') else 'off'


def _s521_ident_kind(openid='', mp_openid='', alipay_uid=''):
    """[S521] 判断本次请求的身份类别，返回 (kind, ident)。

    规则（老板口径：小程序看小程序 ID、公众号看公众号 ID、各自算各自的）：
      · openid 前缀属于【公众号】账号  -> ('oa_openid', openid)   （H5 入口）
      · openid 前缀属于【小程序】账号  -> ('mp_openid', openid)   （小程序客户端常把 mp openid 放在 openid 字段）
      · 前缀判不出来但 is_new_mp_identity 为真（新体系）-> ('mp_openid', openid)
      · 其余：mp_openid 非空 -> ('mp_openid', mp_openid)；alipay_uid 非空 -> ('alipay_uid', alipay_uid)
      · 什么 id 都没有 -> ('', '')，调用方回落老逻辑
    """
    o = _clean(openid)
    m = _clean(mp_openid)
    a = _clean(alipay_uid)
    if o:
        try:
            if appid_by_openid(o, 'oa'):
                return 'oa_openid', o
        except Exception:
            pass
        try:
            if appid_by_openid(o, 'mp'):
                return 'mp_openid', o
        except Exception:
            pass
        try:
            if is_new_mp_identity(openid=o):
                return 'mp_openid', o
        except Exception:
            pass
        return 'oa_openid', o
    if m:
        return 'mp_openid', m
    if a:
        return 'alipay_uid', a
    return '', ''


def resolve_user_by_ident(cursor, kind, ident, auto_create=True):
    """[S521] 按平台 id 认人：kind ∈ {mp_openid, oa_openid, alipay_uid}

    老板口径（2026-09-21）：存量不动；新数据统一按 id 认人；小程序与公众号各算各的、不合并。
      · 只按 id 查/建，手机号不参与；查不到就新建身份（phone 留空）
      · 同一 id 命中多行（历史脏数据）-> 取最早那行(id 最小) + 告警，绝不猜别的行
      · id 空 / 长度不在 16~64 -> 直接拒绝，绝不做兜底查询
      · 并发：用"单语句条件插入 + 回查最早行"，宁可多出一条空行也不引会话级锁
        （连接池 + autocommit 下 pg_advisory_lock 有泄漏风险；空行由对账脚本发现）
    返回 user_id；0 = 没认出来/被拒绝。
    """
    col = _IDENT_COL.get(str(kind or '').strip())
    if not col:
        logger.error('[ident] 未知 kind=%s，拒绝认人', kind)
        return 0
    ident = _clean(ident)
    if not ident:
        logger.warning('[ident] 空 ident，拒绝认人 kind=%s', kind)
        return 0
    if not (16 <= len(ident) <= 64):
        logger.warning('[ident] ident 长度异常(%d)，拒绝认人 kind=%s', len(ident), kind)
        return 0
    try:
        cursor.execute('SELECT id FROM users WHERE %s = %%s ORDER BY id' % col, (ident,))
        rows = cursor.fetchall() or []
    except Exception as e:
        logger.error('[ident] 查询失败 kind=%s: %s', kind, e)
        return 0
    ids = [int((r['id'] if hasattr(r, 'keys') else r[0]) or 0) for r in rows]
    ids = [i for i in ids if i > 0]
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        logger.warning('[ident] 同一身份命中 %d 行（按规矩取最早 id=%s，请客服人工核）kind=%s ident=%s...',
                       len(ids), ids[0], kind, ident[:10])
        return ids[0]
    if not auto_create:
        return 0
    try:
        cursor.execute(
            "INSERT INTO users (%s, phone) SELECT %%s, '' "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE %s = %%s) RETURNING id" % (col, col),
            (ident, ident))
        r = cursor.fetchone()
        if r:
            uid = int((r['id'] if hasattr(r, 'keys') else r[0]) or 0)
            logger.info('[ident] 新建身份 kind=%s ident=%s... user_id=%s', kind, ident[:10], uid)
            return uid
        # 并发下别人先建了 -> 回查最早那行
        cursor.execute('SELECT id FROM users WHERE %s = %%s ORDER BY id LIMIT 1' % col, (ident,))
        r2 = cursor.fetchone()
        if r2:
            return int((r2['id'] if hasattr(r2, 'keys') else r2[0]) or 0)
    except Exception as e:
        logger.error('[ident] 新建失败 kind=%s: %s', kind, e)
    return 0


def resolve_user_identity(cursor, openid='', mp_openid='', phone='', unionid='', user_id=0,
                          strict_openid=False):
    """Resolve one WeChat identity instead of blindly trusting phone_openids.user_id.

    Returns a dict with user_id/unionid/mp_openid/phone/ambiguous. When ambiguous,
    user_id is 0 so callers must not guess another account.

    strict_openid=True（[S320] 新小程序专用）：
      认人范围**严格限制为新小程序自己的 mp_openid**（前缀 == 新小程序前缀），并且：
        * **丢掉客户端带上来的公众号 openid**（oLhbm2…/ov47M3… 这类，正是老账号
          users.openid 的值，会让第 1 步直接命中老账号 97336 —— 是能串号的关键）；
        * **丢掉客户端带上来的 unionid**（同一个人的老身份）；
        * 完全不用手机号兜底：跳过两段"没有强键时按 phone 查 users"，
          以及 user_balances 回退里的 phone = ? 条件。
      于是"新 mp_openid 查不到"时返回 user_id=0，交给调用方新建全新用户。
      默认 False -> 现有调用（含 H5、老小程序）行为一字不变。
    """
    # [S406-20260921] 新公众号身份隔离（收口，所有调用点统一）：
    #   openid 属于【当前启用的公众号】前缀时，丢掉 phone / unionid 兜底键 —— 只按 openid 认人。
    #   否则会把新公众号用户兜到老账号上（生产实例：订单 135729 的 user_id 被兜到老账号 97336，
    #   导致"存包认老账号、看钱包按新 openid 查不到"）。新公众号用户 = 全新用户。
    try:
        from wx_config import oa_openid_prefix as _oap406
        _p406 = _oap406() or ''
        if _p406 and openid and str(openid).startswith(_p406):
            if phone or unionid:
                logger.info('[S406] 新公众号身份隔离：丢弃兜底键(phone/unionid)，只按 openid=%s...', str(openid)[:10])
            phone = ''
            unionid = ''
    except Exception:
        pass
    if strict_openid:
        # [S320] 只承认"新小程序自己的 mp_openid"
        _np = new_mp_openid_prefix()
        if not (_clean(mp_openid) and _np and str(mp_openid).startswith(_np)):
            if mp_openid:
                logger.warning('[S320] strict_openid 下丢弃非新小程序的 mp_openid: %s...', str(mp_openid)[:8])
            mp_openid = ''
        openid = ''      # 丢掉公众号 openid（老账号 users.openid 的值）
        unionid = ''     # 丢掉客户端 unionid（同一个人的老身份）
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

    if not strong_keys and phone and not strict_openid:
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
    if not strong_keys and phone and not strict_openid:
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
            # [S320] strict_openid(新小程序) 时绝不把 phone 当身份条件 ——
            #   这一步正是"新 openid 查不到 -> 用手机号捞出老 unionid -> 认成老账号"的桥。
            if phone and not strict_openid:
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


def find_user_balance_row(cursor, phone='', openid='', mp_openid='', unionid='', user_id=0,
                          strict_identity=False):
    """Find the balance row belonging to one identity. Returns dict or None.

    strict_identity=True（[S320] 新小程序专用）：跳过"最后按 phone 兜底认行"那一段，
      否则新小程序用户会按手机号命中老账号的 user_balances 行，余额/身份被串。
      默认 False -> 现有调用（含 H5、老小程序）行为一字不变。
    """
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
    # [S320] strict_identity(新小程序) 时不做"按手机号认余额行"的兜底
    if phone and not strict_identity:
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
                            balance=0.0, total_deposited=0.0, total_withdrawn=0.0, user_id=0,
                            strict_identity=False):
    """Add balance to the identity's own user_balances row; never merge phones blindly.

    strict_identity=True（[S320] 新小程序专用）：不采信客户端带来的老 unionid，
      且认行时不按手机号兜底（见 find_user_balance_row）。
      默认 False -> 现有调用（含 H5、老小程序）行为一字不变。
    """
    phone = _clean(phone)
    openid = _clean(openid)
    unionid = _clean(unionid)
    mp_openid = _clean(mp_openid)
    wechat_name = _clean(wechat_name)
    balance = float(balance or 0)
    total_deposited = float(total_deposited or 0)
    total_withdrawn = float(total_withdrawn or 0)
    user_id = int(user_id or 0)
    if strict_identity:
        # [S320] 新小程序：不采信客户端带来的老 unionid，也不按手机号认老余额行
        unionid = ''
    existing = find_user_balance_row(cursor, phone=phone, openid=openid, mp_openid=mp_openid,
                                    unionid=unionid, user_id=user_id, strict_identity=strict_identity)
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


def upsert_phone_openid_row(cursor, phone='', openid='', mp_openid='', unionid='', wechat_name='', gzh_openid='', user_id=0, strict_identity=False):
    """Insert or update a phone_openids row keyed by identity, not by phone alone.

    strict_identity=True（[S320] 新小程序专用）：只按本身份的 openid/mp_openid 认行，
      且**不采信客户端带来的 unionid**（那通常是同一个人的老账号 unionid）。
      否则会按 phone+unionid 命中老账号那一行，再把新小程序的 openid 覆盖进去
      —— 实测事故：phone_openids.id=144664 的 openid 被写成 oQXFs3...
      默认 False -> 现有调用（含 H5、老小程序）行为一字不变。

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
    if strict_identity:
        # [S320] 新小程序：客户端会把同一个人的老 unionid 一起带上来，一律不采信。
        #   采信了就会按 unionid 命中老账号那一行(见上面的 144664 事故)。
        unionid = ''
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
        # [S320] strict_identity(新小程序) 时不按 unionid 认行：unionid 已被清空，
        #   再按"phone + 空 unionid"去认行反而会挂到别的老行上。此时只按 openid/mp_openid 认。
        if not _existing_id and (unionid or not strict_identity):
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


# ============================================================
# [S385 2026-09-21] 公众号【模板消息】(cgi-bin/message/template/send)
#   和小程序订阅消息 / 公众号订阅通知的区别：
#     * 模板消息：用户【关注即可收】，不需要每次点订阅；而且【能带 url】直接跳我们的 H5
#     * 只能发给【关注了该模板所属公众号】的用户 -> 所以 openid 必须按那个公众号的前缀去找
#   任何异常 / 找不到 openid / 没配模板 -> 静默返回 False，绝不抛异常、绝不影响主流程
# ============================================================
_OA_TPLMSG_TOKEN = {}          # appid -> (token, 过期时间戳)


def _oa_tplmsg_token(appid, secret):
    """取指定公众号的 access_token（进程内按 appid 缓存）"""
    import time as _t
    now = _t.time()
    hit = _OA_TPLMSG_TOKEN.get(appid)
    if hit and now < hit[1]:
        return hit[0]
    try:
        # [S413-20260921] 收敛：公众号自己那个 appid 一律走 get_oa_access_token()
        #   （已切稳定版 stable_token，并发返回同一个 token，不会把别人顶失效）。
        #   其它账号（多公众号场景）才用下面的老接口。
        _tok = ''
        try:
            from wx_config import oa_appid as _oai413
            if (appid or '') and appid == (_oai413() or ''):
                _tok = get_oa_access_token() or ''
        except Exception:
            _tok = ''
        if _tok:
            _OA_TPLMSG_TOKEN[appid] = (_tok, now + 6000)
            return _tok
        import requests
        _r = requests.get('https://api.weixin.qq.com/cgi-bin/token',
                          params={'grant_type': 'client_credential', 'appid': appid, 'secret': secret},
                          timeout=8).json()
        _tok = _r.get('access_token') or ''
        if _tok:
            _OA_TPLMSG_TOKEN[appid] = (_tok, now + int(_r.get('expires_in', 7200) or 7200) - 300)
            return _tok
        logger.warning('[oa_tplmsg] 取token失败 appid=%s resp=%s', appid, _r)
    except Exception as _e:
        logger.warning('[oa_tplmsg] 取token异常 appid=%s err=%s', appid, _e)
    return ''


def _oa_tplmsg_openid(prefix, openid='', phone='', unionid=''):
    """找【指定公众号】的 openid（必须前缀匹配）；找不到返回空串"""
    if openid and (not prefix or str(openid).startswith(prefix)):
        return openid
    if not prefix:
        return ''
    try:
        from database import get_db
        _c = get_db()
        _cur = _c.cursor()
        _like = prefix + '%'
        _oid = ''
        if phone:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE phone=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE %s ORDER BY id LIMIT 1", (phone, _like))
            _r = _cur.fetchone()
            if _r and _r.get('gzh_openid'):
                _oid = _r['gzh_openid']
            if not _oid:
                _cur.execute("SELECT openid FROM users WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id LIMIT 1", (phone, _like))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
        if not _oid and unionid:
            _cur.execute("SELECT openid FROM users WHERE unionid=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id LIMIT 1", (unionid, _like))
            _r = _cur.fetchone()
            if _r and _r.get('openid'):
                _oid = _r['openid']
        if not _oid and phone:
            _cur.execute("SELECT openid FROM orders WHERE user_phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id DESC LIMIT 1", (phone, _like))
            _r = _cur.fetchone()
            if _r and _r.get('openid'):
                _oid = _r['openid']
        _c.close()
        return _oid or ''
    except Exception as _e:
        logger.warning('[oa_tplmsg] 找openid异常: %s', _e)
        return ''


def oa_tplmsg_h5_url(path='/static/user-h5.html'):
    """模板消息点开后跳的 H5 地址（默认个人中心）"""
    try:
        from wx_config import h5_base as _hb
        return (_hb() or '') + path
    except Exception:
        return 'https://kelaiwei.top' + path


def send_oa_template_message(biz, data, openid='', phone='', unionid='', url='', account_id=None,
                             order_id=None, order_ids=None, pay_channel_id=None):
    """发【公众号模板消息】。
    biz  : 配置中心 wx_templates 里的 biz（如 oa_tplmsg_deposit_ok）
    data : {'thing8': '网点名'} 或 {'thing8': {'value': '网点名'}}
    只发模板里登记过的字段（防止字段写错报 47003）；返回 True/False，绝不抛异常。
    """
    try:
        # [S525] 平台分流闸门：支付宝单绝不按手机号反查公众号/微信身份
        if order_notify_blocked(order_id=order_id, order_ids=order_ids, pay_channel_id=pay_channel_id):
            logger.info('[S525] 支付宝单跳过公众号模板消息 biz=%s order_id=%s order_ids=%s', biz, order_id, order_ids)
            return False
        import json as _json
        import requests
        import wx_config
        try:
            if str(wx_config.get_config('oa_tplmsg_enabled', 'true')).strip().lower() in ('0', 'false', 'off', 'no'):
                return False
        except Exception:
            pass
        _tpl = wx_config.get_template(biz, 'oa')
        if not _tpl or not _tpl.get('template_id'):
            return False
        _aid = account_id or _tpl.get('account_id') or 0
        appid = secret = _prefix = ''
        try:
            from database import get_db
            _c = get_db()
            _cur = _c.cursor()
            if _aid:
                _cur.execute('SELECT appid, secret, openid_prefix FROM wx_accounts WHERE id=%s', (_aid,))
            else:
                _cur.execute("SELECT appid, secret, openid_prefix FROM wx_accounts WHERE acct_type='oa' AND is_active=1 ORDER BY priority, id LIMIT 1")
            _row = _cur.fetchone()
            _c.close()
            if _row:
                appid = _row.get('appid') or ''
                secret = _row.get('secret') or ''
                _prefix = _row.get('openid_prefix') or ''
        except Exception as _e:
            logger.warning('[oa_tplmsg] 取账号失败 biz=%s err=%s', biz, _e)
            return False
        if not appid or not secret:
            return False
        _to = _oa_tplmsg_openid(_prefix, openid=openid, phone=phone, unionid=unionid)
        if not _to:
            logger.info('[oa_tplmsg] 跳过(该用户没有本公众号openid) biz=%s prefix=%s phone=%s', biz, _prefix, phone)
            return False
        _allowed = set()
        try:
            _allowed = set((_json.loads(_tpl.get('fields') or '{}') or {}).keys())
        except Exception:
            _allowed = set()
        _pdata = {}
        for _k, _v in (data or {}).items():
            if _allowed and _k not in _allowed:
                continue
            _pdata[_k] = _v if isinstance(_v, dict) else {'value': '' if _v is None else str(_v)}
        if not _pdata:
            return False
        _payload = {'touser': _to, 'template_id': _tpl['template_id'], 'data': _pdata}
        if url:
            _payload['url'] = url
        _tok = _oa_tplmsg_token(appid, secret)
        if not _tok:
            return False
        _resp = requests.post('https://api.weixin.qq.com/cgi-bin/message/template/send',
                              params={'access_token': _tok},
                              data=_json.dumps(_payload, ensure_ascii=False).encode('utf-8'),
                              timeout=8).json()
        if _resp.get('errcode') == 0:
            logger.info('[oa_tplmsg] 发送成功 biz=%s to=%s... msgid=%s', biz, _to[:8], _resp.get('msgid'))
            return True
        logger.warning('[oa_tplmsg] 发送失败 biz=%s to=%s... resp=%s', biz, _to[:8], _resp)
        return False
    except Exception as _e:
        logger.warning('[oa_tplmsg] 异常 biz=%s err=%s', biz, _e)
        return False


# ============================================================
# [S541-20260922] 提现"押金原路退回"开关（默认值全部 = 现状行为，上线后行为不变）
#   withdraw_refund_mode              : transfer(默认,现状=仅微信退款且过滤支付宝) / original(逐单按渠道原路退回)
#   withdraw_refund_alipay            : 0(默认) / 1   支付宝单是否参与提现
#   withdraw_refund_dry_run           : 0(默认) / 1   干跑：只算计划打日志，不调渠道、不改库
#   withdraw_refund_original_locations: ''(默认,不限) 逗号分隔 locations.id，仅 mode=original 时的网点灰度白名单
#   读不到/值非法一律回落现状，绝不因配置问题改变资金行为。
# ============================================================
WITHDRAW_REFUND_MODE_KEY = 'withdraw_refund_mode'
WITHDRAW_REFUND_ALIPAY_KEY = 'withdraw_refund_alipay'
WITHDRAW_REFUND_DRY_RUN_KEY = 'withdraw_refund_dry_run'
WITHDRAW_REFUND_ORIG_LOCS_KEY = 'withdraw_refund_original_locations'


def is_channel_balance_error(msg='', result=None):
    """[S541] 渠道"资金水位不足"判定 —— 这类失败是【可重试】的，不是永久失败。
      支付宝: ACQ.SELLER_BALANCE_NOT_ENOUGH (卖家余额不足；官方释义"商户账户充值后重新发起退款即可")
      微信  : NOTENOUGH / 基本账户余额不足
    官方还明确"退款退费：退款时手续费会一并退还"，所以退款失败只可能是账户里没有可动用的钱。
    """
    _s = str(msg or '')
    _u = _s.upper()
    if 'BALANCE_NOT_ENOUGH' in _u or 'SELLER_BALANCE' in _u:
        return True
    if 'NOTENOUGH' in _u or '余额不足' in _s:
        return True
    try:
        if isinstance(result, dict):
            _sc = str(result.get('sub_code') or '').upper()
            _ec = str(result.get('err_code') or '').upper()
            if 'BALANCE_NOT_ENOUGH' in _sc or 'NOTENOUGH' in _ec:
                return True
    except Exception:
        pass
    return False


def alert_withdraw_channel_balance(amount=0, count=0, msg='', wid=None, tag=''):
    """[S541] 渠道余额不足 -> 给管理员一条"人话"提醒(PushPlus)，30 分钟去重。
    返回 True=本次真的推送了。任何异常都不影响退款主流程。"""
    try:
        import time as _t
        import json as _j
        from database import get_db as _gdb
        _key = 'withdraw_channel_balance_alert'
        _conn = _gdb()
        _c = _conn.cursor()
        _c.execute("SELECT setting_value FROM system_settings WHERE setting_key=%s", (_key,))
        _row = _c.fetchone()
        _raw = ''
        if _row:
            _raw = _row.get('setting_value') if isinstance(_row, dict) else _row[0]
        try:
            _st = _j.loads(_raw or '{}') or {}
        except Exception:
            _st = {}
        _now = int(_t.time())
        if _now - int(_st.get('ts') or 0) < 1800:
            try:
                _conn.close()
            except Exception:
                pass
            return False
        _title = '【寄存柜】商户账户余额不足：提现原路退回暂缓，充值后自动重试'
        _content = ('有提现的原路退回因为【商户账户可用余额不足】暂时没退成，钱没有丢，也没有算成拒绝。\n'
                    '提现单: %s (%s)\n'
                    '涉及笔数: %s\n'
                    '涉及金额: 约 %.2f 元\n'
                    '渠道返回: %s\n'
                    '系统已安排 30 分钟后自动重试（同一笔用确定性退款单号，不会重复退款）。\n'
                    '说明: 支付宝官方口径——退款失败只可能是账户里没有可动用的钱（退款时手续费会一并退还）；'
                    '新商户当日收款资金可能是"次日结算/不可用余额"，不是故障。\n'
                    '-> 请到支付宝商户账户充值，或到后台"提现管理"对该笔点【通过】重试。') % (
            wid, tag, int(count or 0), float(amount or 0), str(msg or '')[:120])
        _ok = send_pushplus(_title, _content)
        try:
            _c.execute("INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s) "
                       "ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                       (_key, _j.dumps({'ts': _now, 'wid': wid, 'tag': tag}, ensure_ascii=False)))
            _conn.commit()
        except Exception:
            pass
        try:
            _conn.close()
        except Exception:
            pass
        logger.warning('[S541] 已给管理员推送"渠道余额不足"提醒 wid=%s ok=%s', wid, _ok)
        return bool(_ok)
    except Exception as _e:
        logger.warning('[S541] 渠道余额不足提醒发送失败(不影响主流程): %s', _e)
        return False



def _s541_bool_setting(key, default=False):
    try:
        v = str(get_setting(key, '') or '').strip().lower()
    except Exception:
        return default
    if v in ('1', 'true', 'yes', 'on'):
        return True
    return False


def withdraw_refund_mode():
    """提现退款模式：transfer=现状(仅微信退款且过滤支付宝) / original=逐单按渠道原路退回。"""
    try:
        v = str(get_setting(WITHDRAW_REFUND_MODE_KEY, 'transfer') or '').strip().lower()
    except Exception:
        return 'transfer'
    return 'original' if v == 'original' else 'transfer'


def withdraw_refund_alipay_enabled():
    """支付宝单是否参与提现（默认 0=不参与=现状）。"""
    return _s541_bool_setting(WITHDRAW_REFUND_ALIPAY_KEY, False)


def withdraw_refund_dry_run():
    """干跑：只算计划、写日志，不调渠道、不改库（默认 0）。"""
    return _s541_bool_setting(WITHDRAW_REFUND_DRY_RUN_KEY, False)


def withdraw_refund_original_locations():
    """网点灰度白名单（逗号分隔 locations.id；空=不限）。仅 mode=original 时对微信单生效。"""
    try:
        raw = str(get_setting(WITHDRAW_REFUND_ORIG_LOCS_KEY, '') or '')
    except Exception:
        return set()
    out = set()
    for x in raw.replace('，', ',').split(','):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out


def withdraw_use_original(ch_types, location_id=None, mode=None, alipay_enabled=None):
    """[S541] 判断"这一单"是否走新的原路退回口径。返回 bool。
    规则（与灰度顺序一致）：
      * 支付宝单(channel_type=alipay)：只要 withdraw_refund_alipay=1 就生效 —— 微信路径完全不受影响
        （对应"只放支付宝"灰度：在途支付宝资金极小，微信侧保持现状）
      * 其他/判不出渠道     ：必须 withdraw_refund_mode=original；若配了网点白名单，则要求该单网点在白名单内
    """
    if mode is None:
        mode = withdraw_refund_mode()
    if alipay_enabled is None:
        alipay_enabled = withdraw_refund_alipay_enabled()
    _has_alipay = False
    for t in (ch_types or []):
        if str(t or '').strip().lower() == 'alipay':
            _has_alipay = True
            break
    if _has_alipay:
        return bool(alipay_enabled)
    if mode != 'original':
        return False
    _locs = withdraw_refund_original_locations()
    if _locs:
        try:
            return int(location_id or 0) in _locs
        except Exception:
            return False
    return True


def withdraw_refund_plan(order_ids, mode=None, alipay_enabled=None):
    """[S541] 预扫一批提现订单：{oid: {'channel_type','location_id','original'}}。
    只读；任何异常返回 {}（调用方按"全部现状"处理）。"""
    out = {}
    try:
        ids = [int(x) for x in (order_ids or [])]
    except Exception:
        return out
    if not ids:
        return out
    if mode is None:
        mode = withdraw_refund_mode()
    if alipay_enabled is None:
        alipay_enabled = withdraw_refund_alipay_enabled()
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT o.id,
                            COALESCE(pc.channel_type, '') AS channel_type,
                            l.id AS location_id
                     FROM orders o
                     LEFT JOIN payment_channels pc ON pc.id = o.payment_channel_id
                     LEFT JOIN cabinets cb ON cb.id = o.cabinet_id
                     LEFT JOIN locations l ON l.id = cb.location_id
                     WHERE o.id = ANY(%s)""", (ids,))
        for r in c.fetchall():
            _ch = str(r.get('channel_type') or '').strip().lower()
            out[int(r['id'])] = {
                'channel_type': _ch,
                'location_id': r.get('location_id'),
                'original': bool(withdraw_use_original([_ch], r.get('location_id'),
                                                       mode=mode, alipay_enabled=alipay_enabled)),
            }
    except Exception as e:
        logger.warning('[S541] 提现计划预扫失败(按现状处理): %s', e)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def do_withdraw_order_refund(order_id=None, order_no=None, payment_channel_id=None,
                             amount=0, use_original=False, wid=None):
    """[S541] 提现"逐单"退款统一入口。返回 (success, refund_id, msg)。
    use_original=False：与改动前逐字节相同的调用（只转发 4 个老参数）-> 微信路径零变化。
    use_original=True ：多传 2 个可选参数
        out_refund_no='WD<wid>_<oid>' -> 渠道侧确定性幂等（同单同额重试不重复退款）
        write_payment=True            -> 补写 payments(type=2) 对账流水（方案 §3.6 的缺口）
    """
    if not use_original:
        return do_real_refund(order_id=order_id, order_no=order_no, amount=amount,
                              payment_channel_id=payment_channel_id)
    _out_no = None
    if wid is not None and order_id is not None:
        _out_no = 'WD%s_%s' % (wid, order_id)
    return do_real_refund(order_id=order_id, order_no=order_no, amount=amount,
                          payment_channel_id=payment_channel_id,
                          out_refund_no=_out_no, write_payment=True)


def do_real_refund(order_id=None, order_no=None, amount=0, payment_channel_id=None, skip_balance=False,
                   out_refund_no=None, write_payment=False, **kwargs):
    # [S541-20260922] 只新增 2 个【可选】参数(默认 None/False)，不传时本函数行为与改动前逐字节一致：
    #   out_refund_no : 渠道侧确定性退款单号（微信 out_refund_no / 支付宝 out_request_no）
    #   write_payment : 退款成功后是否补写 payments(type=2) 对账流水（方案 §3.6 的缺口）
    """Actually call WeChat refund API. Returns (success, refund_id, message)"""
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
        # [S538-20260922] 记录渠道实例类型(wechat/alipay), 供后面按渠道分流退款调用
        _s538_ch_type = ''
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
                    payer, _s538_ch_type = get_channel_wxpay(channel_dict)
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
                            payer, _s538_ch_type = get_channel_wxpay(dict(_rch))
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
        # [S538-20260922] 按【订单实际渠道】分流退款调用:
        #   原来无论什么渠道都按【微信退款】口径调用 payer.refund(total_fee/refund_fee),
        #   支付宝单(channel_type='alipay') 拿到的是 AlipayClient ->
        #   TypeError: refund() got an unexpected keyword argument 'total_fee'
        #   -> 退使用费/提现审批/投诉退款等所有走本函数的支付宝单全部失败。
        #   此处只【新增】支付宝分支, 微信分支的调用参数一字未改。
        _s538_req_no = ''
        if _s538_ch_type == 'alipay':
            # out_request_no 用【确定性唯一串】(订单ID/订单号 + 金额分), 同单同额重试幂等, 防重复退款
            # out_request_no 用【确定性唯一串】(订单ID/订单号 + 金额分), 同单同额重试幂等, 防重复退款
            # [S541-20260922] 调用方给了确定性单号(out_refund_no)就优先用它; 没给=原样(S538 的生成式)
            _s538_req_no = out_refund_no or ('RF%s_%d' % (order_id or order_no, int(round(float(amount) * 100))))
            result = payer.refund(out_trade_no=order_no, refund_amount=float(amount),
                                  out_request_no=_s538_req_no, refund_reason='原路退款')
        else:
            # [S541-20260922] 只多一个"给确定性幂等号"的分支：调用方没传 out_refund_no 时，
            #   这里执行的仍是改动前那一行（同一函数、同一参数），微信路径行为完全不变。
            if out_refund_no:
                result = payer.refund(out_trade_no=order_no, total_fee=total_fee, refund_fee=refund_fee,
                                      out_refund_no=out_refund_no)
            else:
                result = payer.refund(out_trade_no=order_no, total_fee=total_fee, refund_fee=refund_fee)
        if _s538_ch_type == 'alipay':
            # [S541-20260922] 官方口径：code=10000 只代表"本次退款请求成功", 不代表退款成功。
            #   必须 fund_change=Y 才算退成功；fund_change=N 或无此字段时用退款查询接口复核。
            _s541_fc = str(result.get('fund_change') or '').strip().upper()
            _s538_refund_ok = (str(result.get('code') or '') == '10000') and (_s541_fc == 'Y')
            if (str(result.get('code') or '') == '10000') and not _s538_refund_ok:
                try:
                    _s541_q = payer.refund_query(out_request_no=_s538_req_no,
                                                 out_trade_no=order_no) or {}
                except Exception as _s541_qe:
                    _s541_q = {}
                    logger.warning('[S541] 支付宝退款查询异常(按未成功处理): order=%s err=%s', order_no, _s541_qe)
                _s541_qs = str(_s541_q.get('refund_status') or '').strip().upper()
                try:
                    _s541_qamt = float(_s541_q.get('refund_amount') or 0)
                except Exception:
                    _s541_qamt = 0.0
                _s538_refund_ok = (str(_s541_q.get('code') or '') == '10000') and (
                    _s541_qs == 'REFUND_SUCCESS' or (_s541_qamt > 0 and _s541_qs != 'REFUND_CLOSED'))
                logger.warning('[S541] 支付宝退款 fund_change=%s 需复核: order=%s out_request_no=%s '
                               'query_code=%s refund_status=%s refund_amount=%s -> ok=%s',
                               _s541_fc or '(空)', order_no, _s538_req_no,
                               _s541_q.get('code'), _s541_qs, _s541_qamt, _s538_refund_ok)
                if _s538_refund_ok and not result.get('trade_no'):
                    result['trade_no'] = _s541_q.get('trade_no')
        else:
            _s538_refund_ok = result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS'
        if _s538_refund_ok:
            refund_id = result.get('refund_id') or result.get('out_refund_no', '') or _s538_req_no
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
                    # [S541-20260922] 对账缺口补齐：提现退款成功后补写 payments(type=2)。
                    #   只有调用方显式要求(write_payment=True，即 original/支付宝灰度)时才写 ->
                    #   transfer(现状)模式不执行这一段，行为与改动前完全一致。
                    if write_payment:
                        try:
                            _s541_rid = refund_id or _s538_req_no or ''
                            _s541_txn = None
                            if isinstance(result, dict):
                                _s541_txn = (result.get('transaction_id') or result.get('trade_no')
                                             or (result.get('out_refund_no') if _s538_ch_type != 'alipay' else None))
                            c_bal.execute(
                                "INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) "
                                "SELECT %s, 2, %s, %s, %s, 1, NOW() "
                                "WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id=%s AND p.type=2 AND p.refund_transaction_id=%s)",
                                (order_id, float(amount), _s541_txn, _s541_rid, order_id, _s541_rid))
                            logger.info('[S541] payments(type=2) 已补写: order_id=%s amount=%s refund_id=%s', order_id, amount, _s541_rid)
                        except Exception as _s541_pe:
                            logger.error('[S541] payments(type=2) 写入失败: %s', _s541_pe)

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
            if _s538_ch_type == 'alipay':
                # 支付宝失败信息在 sub_msg/sub_code, 让调用方能拿到可读原因
                # [S541-20260922] 带上 sub_code(如 ACQ.SELLER_BALANCE_NOT_ENOUGH), 调用方据此判"可重试"
                _s541_sub_code = str(result.get('sub_code') or '')
                err_msg = result.get('sub_msg') or result.get('msg') or err_msg
                if _s541_sub_code:
                    err_msg = '%s(%s)' % (err_msg, _s541_sub_code)
            logger.error('[do_real_refund] Failed: order=%s, msg=%s, result=%s' % (order_no, err_msg, str(result)))
            # 微信明确表示订单已退款/已全额退款时，按退款成功处理，避免恢复余额导致双倍到账
            _already_refunded = ('订单已全额退款' in str(err_msg)) or ('该订单已全额退款' in str(err_msg))
            if _s538_ch_type == 'alipay' and ('ACQ.TRADE_HAS_REFUND' in str(result.get('sub_code') or '')):
                # [S541-20260922] 支付宝明确"交易已全额退款" -> 按已退款处理(幂等: 不重复退也不报错)
                _already_refunded = True
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


def settle_withdrawal_for_order(cur, order_id, amount, approver='投诉自动退款', phone=''):
    """[S188 2026-09-15] 投诉/客服自动退款后, 按"逐单扣减"结算包含该订单的提现单.

    背景: 原来这条路径是 `UPDATE withdrawal_records SET status=2 WHERE order_id=%s`,
    只按单个 order_id 匹配, 而"合并提现单"的 order_id 只是批次里的第一个订单;
    于是"只退了一部分却整张单标记已通过", 剩余押金既没退给用户、余额明细又被隐藏(pending),
    用户看不到也提不出来. 现在改成与 routes/admin_v2.py 订单退款(S102/S111)一致的做法:
      - 从所有待处理(status 0/1)且包含该订单的单里移除该订单并扣减金额;
      - 扣减后还有别的订单 -> 保持待处理, 只更新金额/订单列表(若移除的正好是单里的 order_id, 换成剩余第一个);
      - 全部订单都退完 -> 置为已通过;
      - 并补一条该订单自己的已通过记录, 保证用户提现记录里有这笔退款流水.
    幂等: 重复调用不会重复扣减、也不会重复补记录. 不提交事务, 由调用方 commit.
    返回统计 dict 供日志使用.
    """
    import json as _json_s
    stat = {'matched': 0, 'deducted': 0, 'approved': 0, 'inserted': False}
    try:
        oid = int(order_id)
    except Exception:
        return stat
    amt = float(amount or 0)

    def _g(row, key, idx):
        try:
            if hasattr(row, 'get'):
                return row.get(key)
            return row[idx]
        except Exception:
            return None

    if not phone:
        try:
            cur.execute('SELECT user_phone FROM orders WHERE id=%s', (oid,))
            _r = cur.fetchone()
            if _r:
                phone = _g(_r, 'user_phone', 0) or ''
        except Exception:
            phone = ''

    # 1) 包含该订单的待处理提现单
    # 用文本 LIKE 预筛(避免 order_ids 里若有非 JSON 脏数据时 ::jsonb 强转报错), 命中后再由 Python 精确判断
    cur.execute("""SELECT id, amount, order_ids, order_id FROM withdrawal_records
                   WHERE status IN (0,1) AND (order_id=%s OR order_ids LIKE %s)
                   ORDER BY id""",
                (oid, '%' + str(oid) + '%'))
    for row in cur.fetchall():
        _id = _g(row, 'id', 0)
        _amt = float(_g(row, 'amount', 1) or 0)
        _oids_raw = _g(row, 'order_ids', 2) or '[]'
        _cur_oid = _g(row, 'order_id', 3)
        try:
            _oids = _json_s.loads(_oids_raw)
        except Exception:
            _oids = []
        _contains = str(oid) in [str(x) for x in _oids]
        _is_own = (str(_cur_oid) == str(oid))
        if not (_contains or _is_own):
            continue          # LIKE 预筛的误命中, 跳过
        stat['matched'] += 1
        if _contains:
            _oids = [x for x in _oids if str(x) != str(oid)]
            _new_amt = max(0.0, _amt - amt)
        else:
            _new_amt = _amt
        if _oids:
            _next_oid = _cur_oid
            if str(_cur_oid) == str(oid):
                try:
                    _next_oid = int(_oids[0])
                except Exception:
                    _next_oid = _oids[0]
            cur.execute("""UPDATE withdrawal_records SET amount=%s, order_ids=%s, order_id=%s,
                           error_msg=NULL, retry_count=0 WHERE id=%s""",
                        (round(_new_amt, 2), _json_s.dumps(_oids), _next_oid, _id))
            stat['deducted'] += 1
        else:
            cur.execute("""UPDATE withdrawal_records SET status=2, amount=%s, order_ids=%s,
                           approver=%s, approve_time=CURRENT_TIMESTAMP, error_msg=NULL WHERE id=%s""",
                        (round(_new_amt, 2), _json_s.dumps([]), approver, _id))
            stat['approved'] += 1

    # 2) 补一条该订单自己的已通过记录(保证用户提现记录里能看到这笔退款)
    cur.execute('SELECT 1 FROM withdrawal_records WHERE order_id=%s LIMIT 1', (oid,))
    if not cur.fetchone():
        try:
            cur.execute("""INSERT INTO withdrawal_records
                           (order_id, user_phone, amount, status, approver, order_ids, approve_time, error_msg, dedup_key, created_at)
                           VALUES (%s, %s, %s, 2, %s, %s, CURRENT_TIMESTAMP, %s, %s, CURRENT_TIMESTAMP)
                           ON CONFLICT DO NOTHING""",
                        (oid, phone, round(amt, 2), approver, _json_s.dumps([oid]),
                         '%s(原提现单已联动扣减)' % approver, 'C:%s:%s' % (phone, oid)))
            stat['inserted'] = True
        except Exception as _ie:
            logger.warning('[settle_withdrawal] 补建提现记录失败 order_id=%s err=%s', oid, _ie)
    return stat


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
                    payer, _ = get_channel_wxpay(dict(_ch_row), openid=openid)
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
                    payer, _ = get_channel_wxpay(dict(_ch2), openid=openid)
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
        payload = dict(grant_type='client_credential', appid=_wx_mp_id(), secret=_wx_mp_secret(), force_refresh=force_refresh)
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
                import os as _s538_os
                # [S538-20260922] 支付宝渠道【显式跳过】巡检。
                #   背景: 本巡检用微信专用的 payer.order_query() 判活, 支付宝渠道的
                #   AlipayClient 没有该方法 -> 每分钟抛一条
                #   "'AlipayClient' object has no attribute 'order_query'" ERROR
                #   (2026-09-21 23:01~23:04 实测每 60 秒 1 条)。
                #   这里只对 channel_type='alipay' 提前 continue;
                #   判不出渠道类型(channel_type 为空/其它)时保持原行为(仍走 order_query)。
                #   提示做"每小时最多一次"去重(跨 worker 用文件 mtime), 不再每分钟刷日志。
                if (channel.get('channel_type') or '').strip().lower() == 'alipay':
                    _s538_notice = True
                    try:
                        _s538_notice = (time.time() - _s538_os.path.getmtime(_MH_ALIPAY_NOTICE_FILE)) > 3600
                    except Exception:
                        _s538_notice = True
                    if _s538_notice:
                        try:
                            with open(_MH_ALIPAY_NOTICE_FILE, 'w') as _s538_nf:
                                _s538_nf.write(str(time.time()))
                        except Exception:
                            pass
                        logger.info('[MerchantHealth] 渠道 %s 为支付宝渠道(无微信查单接口), 已显式跳过巡检' % ch_name)
                    else:
                        logger.debug('[MerchantHealth] 渠道 %s 支付宝渠道跳过巡检' % ch_name)
                    continue
                # 找该渠道的最近一笔已支付订单作为探测目标
                # [S414-20260921] 连订单的 openid 一起查出来：下单时的 appid 是【按付款人 openid 前缀】
                #   动态选的（公众号用户=公众号appid / 小程序用户=小程序appid）。
                #   这里不传 openid 就会用渠道里存的 app_id，两者不一致时微信回
                #   APPID_MCHID_NOT_MATCH -> 被误判成"商户异常"-> 自动禁用渠道 -> 支付全挂。
                cursor.execute(
                    "SELECT order_no, openid, mp_openid FROM orders WHERE status IN (2,3,4) "
                    "AND transaction_id IS NOT NULL AND transaction_id != '' "
                    "AND payment_channel_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (channel['id'],))
                row = cursor.fetchone()
                if not row or not row.get('order_no'):
                    logger.debug('[MerchantHealth] 渠道 %s(%s) 无探测订单，跳过' % (ch_name, mch_id))
                    continue

                _probe_oid = ''
                try:
                    _probe_oid = (row.get('openid') or '') or (row.get('mp_openid') or '')
                except Exception:
                    _probe_oid = ''
                payer, ch_type = get_channel_wxpay(channel, openid=_probe_oid)
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
                        # [S414-20260921] 保险：连续 2 轮异常才禁用（一次探测误判不再直接停掉整个渠道）
                        _fk = 'mh_failcnt_%s' % channel['id']
                        _fc = int(_merchant_health_state.get(_fk, 0) or 0) + 1
                        _merchant_health_state[_fk] = _fc
                        if _fc < 2:
                            logger.warning('[MerchantHealth] 渠道 %s(%s) 第 %d 次异常，再观察一轮不停用' % (ch_name, mch_id, _fc))
                        else:
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
# [S538-20260922] 支付宝渠道巡检跳过提示的"已提示过"标记文件(跨 worker 共享, 按 mtime 去重)
_MH_ALIPAY_NOTICE_FILE = '/tmp/merchant_health_alipay_notice.txt'


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


# ============================================================
# [2026-09-14] 公众号订阅通知通道：小程序订阅消息发不出去时的兜底
#   小程序订阅消息是"一次性授权"（用户点一次允许只能发一条），实测今天 34% 的用户
#   至少有一条通知发不出去（43101 没额度 / 只有旧号 openid / 没有小程序 openid）。
#   公众号"订阅通知"(/cgi-bin/message/subscribe/bizsend) 实测对未关注、未订阅的普通
#   用户也能发（连发多条 errcode=0，用户手机确实收到）-> 用它兜底。
#   公众号这三个模板的字段 key 与小程序那三条完全一致，data 可直接复用。
#   开关：设置项 oa_notify_fallback_enabled（默认 true），关掉即等于改造前行为。
# ============================================================
_OA_SUB_TPL = {
    'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk': 'oa_sub_deposit',   # 寄存成功
    'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA': 'oa_sub_general',   # 押金退还
    'lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c': 'oa_sub_refund',    # 退款成功
}
_OA_SUB_TPL_DEFAULT = {
    'oa_sub_deposit': 'RyvAHJzC46JDEk_w8nAJHlLppNtNiAgkqP_GdHGWEUg',
    'oa_sub_general': 'pG1ieUsHgBg5y0ZJCfjNiUrF7QuftlSWwDYXSpAp6js',
    'oa_sub_refund': 'nDTX0vT_wODrTnM4A1qSNnSXDUTNgw2Fm0CMF0eZe-o',
}
_oa_token_cache = {'token': '', 'exp': 0.0}
_oa_sent_cache = {}


def _oa_fallback_enabled():
    """兜底通道总开关（设置项 oa_notify_fallback_enabled，默认开）"""
    try:
        return str(get_setting('oa_notify_fallback_enabled', 'true')).strip().lower() in ('true', '1', 'yes', 'on')
    except Exception:
        return True


def get_oa_access_token():
    """公众号 access_token（进程内缓存；取不到返回空串）"""
    import time as _t
    now = _t.time()
    if _oa_token_cache['token'] and now < _oa_token_cache['exp']:
        return _oa_token_cache['token']
    try:
        import requests
        # [S411-20260921] 改用官方【稳定版】stable_token：
        #   老的 /cgi-bin/token 每次调用都发新 token 并把旧的顶失效；本项目有 8~10 个进程、
        #   多处代码各自刷新 -> 互相顶 -> 公众号模板消息大面积 40001（6h 内 52 失败/26 成功）。
        #   stable_token 在同一 appid 上并发/重复调用都返回同一个 token，不会互顶。
        _r = requests.post(
            'https://api.weixin.qq.com/cgi-bin/stable_token',
            json={'grant_type': 'client_credential', 'appid': _wx_oa_id(),
                  'secret': _wx_oa_secret(), 'force_refresh': False}, timeout=8).json()
        if not _r.get('access_token'):
            # 兜底：稳定版失败时退回老接口，保证不因为改造把通知彻底打断
            _r = requests.get(
                'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s'
                % (_wx_oa_id(), _wx_oa_secret()), timeout=8).json()
        _tok = _r.get('access_token', '') or ''
        if _tok:
            _oa_token_cache['token'] = _tok
            _oa_token_cache['exp'] = now + int(_r.get('expires_in', 7200) or 7200) - 300
        else:
            logger.warning('[oa_notify] 取公众号access_token失败: %s' % _r)
        return _tok
    except Exception as _e:
        logger.warning('[oa_notify] 取公众号access_token异常: %s' % _e)
        return ''


def find_oa_openid(phone='', unionid=''):
    """找该用户的【公众号】openid（前缀 oLhbm2）：phone_openids.gzh_openid -> users.openid -> phone_openids.openid -> 按 unionid 跨手机号"""
    try:
        from database import get_db
        _c = get_db()
        _cur = _c.cursor()
        _pref = oa_openid_prefix()
        _oid = ''
        if phone:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE phone=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE %s ORDER BY id ASC LIMIT 1", (phone, _pref + '%'))
            _r = _cur.fetchone()
            if _r and _r.get('gzh_openid'):
                _oid = _r['gzh_openid']
            if not _oid:
                _cur.execute("SELECT openid FROM users WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id ASC LIMIT 1", (phone, _pref + '%'))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
            if not _oid:
                _cur.execute("SELECT openid FROM phone_openids WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id ASC LIMIT 1", (phone, _pref + '%'))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
        if not _oid and unionid:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE unionid=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE %s ORDER BY id ASC LIMIT 1", (unionid, _pref + '%'))
            _r = _cur.fetchone()
            if _r and _r.get('gzh_openid'):
                _oid = _r['gzh_openid']
        try:
            _c.close()
        except Exception:
            pass
        return _oid or ''
    except Exception as _e:
        logger.warning('[oa_notify] 找公众号openid失败: %s' % _e)
        return ''


def send_oa_subscribe_notify(mp_template_id, data, phone='', unionid='', reason=''):
    """小程序通道发不出去时的兜底：改发公众号订阅通知。返回 True/False。

    只对我们登记过映射的三个模板生效（寄存成功/押金退还/退款成功），其它模板直接返回 False。
    同一个手机号+同一模板+同一内容 90 秒内只发一次（防调用方重试造成重复消息）。
    """
    try:
        if not _oa_fallback_enabled():
            return False
        _biz = _OA_SUB_TPL.get(mp_template_id)
        if not _biz:
            return False
        try:
            from wx_config import template_id as _tpl_id
            _oa_tpl = _tpl_id(_biz, 'oa_sub', _OA_SUB_TPL_DEFAULT.get(_biz, '')) or _OA_SUB_TPL_DEFAULT.get(_biz, '')
        except Exception:
            _oa_tpl = _OA_SUB_TPL_DEFAULT.get(_biz, '')
        if not _oa_tpl:
            return False
        _oid = find_oa_openid(phone=phone, unionid=unionid)
        if not _oid:
            logger.warning('[oa_notify] 跳过(没有公众号openid): phone=%s biz=%s 原因=%s' % (phone, _biz, reason))
            return False
        try:
            import json as _json, time as _t, hashlib as _hl
            _key = (phone or '') + '|' + _biz + '|' + _hl.md5(_json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
            _now = _t.time()
            if _oa_sent_cache.get(_key, 0) > _now - 90:
                logger.info('[oa_notify] 90秒内同内容已发过，跳过重复: phone=%s biz=%s' % (phone, _biz))
                return False
        except Exception:
            _key = None
        _tok = get_oa_access_token()
        if not _tok:
            return False
        import requests
        _resp = requests.post(
            'https://api.weixin.qq.com/cgi-bin/message/subscribe/bizsend?access_token=%s' % _tok,
            json={'touser': _oid, 'template_id': _oa_tpl, 'data': data}, timeout=6).json()
        if _resp.get('errcode') == 0:
            if _key:
                _oa_sent_cache[_key] = time.time()
            logger.info('[oa_notify] 发送成功(公众号兜底): phone=%s openid=%s..., biz=%s, 小程序失败原因=%s'
                        % (phone, _oid[:8], _biz, reason))
            return True
        logger.error('[oa_notify] 发送失败: phone=%s openid=%s..., biz=%s, result=%s, 小程序失败原因=%s'
                     % (phone, _oid[:8], _biz, _resp, reason))
        return False
    except Exception as _e:
        logger.error('[oa_notify] 异常: %s' % _e)
        return False


# ============================================
# [S231-20260917] "押金退还通知" -> "账户余额通知" 换模板过渡用的两个 ID
# 换模板后老用户手里只有旧模板的授权（微信按模板 ID 记账），新模板会被拒(43101 无额度)，
# 所以发送失败时用旧模板再发一次，过渡期一条通知都不丢。
# ============================================
_TPL_ACCOUNT_NEW = 'ax-O5Qa05IWt7bbhRVk9Pb9A_SbXfIMfbhm0Hoh4gYc'   # 账户余额通知（新）
_TPL_DEPOSIT_OLD = 'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA'   # 押金退还通知（旧，仅作过渡回退）


def get_access_token_for(appid, secret, force_refresh=False):
    """[S307] 按【指定小程序】拿 access_token（多小程序并存用）

    与 get_access_token() 的区别：那个用"当前生效账号"，这个用传入的 appid/secret。
    缓存分开（setting_key 按 appid 加后缀），互不干扰。
    """
    from datetime import datetime, timedelta
    appid = (appid or '').strip()
    if not appid or not secret:
        return None
    cache_key = 'wx_mp_access_token_' + appid
    try:
        conn = get_db()
        cur = conn.cursor()
        if not force_refresh:
            cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s", (cache_key,))
            row = cur.fetchone()
            if row and row.get('setting_value'):
                try:
                    import json as _j
                    _d = _j.loads(row['setting_value'])
                    _ea = datetime.fromisoformat(_d['expires_at'])
                    if datetime.now() < _ea - timedelta(seconds=600):
                        conn.close()
                        return _d['token']
                except Exception:
                    pass
        import requests as _r
        resp = _r.post('https://api.weixin.qq.com/cgi-bin/stable_token',
                       json=dict(grant_type='client_credential', appid=appid, secret=secret,
                                 force_refresh=force_refresh), timeout=5)
        result = resp.json()
        if 'access_token' in result:
            _tok = result['access_token']
            _ea2 = (datetime.now() + timedelta(seconds=result.get('expires_in', 7200))).isoformat()
            import json as _j2
            cur.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)",
                        (cache_key, _j2.dumps(dict(token=_tok, expires_at=_ea2))))
            conn.commit(); conn.close()
            return _tok
        logger.error('[get_access_token_for] fail appid=%s: %s', appid, result)
        conn.close()
        return None
    except Exception as e:
        logger.error('[get_access_token_for] err appid=%s: %s', appid, e)
        return None


# ============================================================
# [S343-20260920] 多小程序"模板字段名"映射
#   老模板与新小程序(账号9)模板的字段名不同，调用方一律按老字段名构造 data，
#   直接发给新模板会 47003（data.thing5.value is empty / data.time6.value is empty）。
#   目标字段名来自微信官方 wxaapi/newtmpl/gettemplate 的 content（2026-09-20 实测 errcode=0）：
#     账号9 subscribe_general ReaKJobHOusye1cCDzOPJ0HB3rZrwQadplL-Qf0js3M
#         剩余金额:amount1  时间:time2  变动原因:thing5  备注:thing4
#     账号9 subscribe_refund  sKHQzRCcaxWWn9Qx54gx28GmaE2rMGG6PRPnIGJLuxU
#         退款金额:amount8  退款时间:time6  退款方式:thing10  备注:thing2
#   只对表内列出的 (账号id, biz) 生效；不在表里的账号(含老小程序账号1) data 一字不动。
# ============================================================
_SUBSCRIBE_FIELD_MAP = {
    (9, 'subscribe_general'): {
        'amount1': 'amount1', 'time2': 'time2',
        'thing4': 'thing5',      # 老:变动原因(短状态) -> 新:变动原因
        'thing3': 'thing4',      # 老:温馨提示(长提示) -> 新:备注
    },
    (9, 'subscribe_refund'): {
        'amount2': 'amount8', 'time5': 'time6',
        'thing4': 'thing10',     # 老:退款方式 -> 新:退款方式
        'thing3': 'thing2',      # 老:备注 -> 新:备注
    },
}
_THING_MAX = 20                  # 微信 thing 类关键字上限 20 字，超长截断防 47003


def _clip_thing(nk, v):
    """thing 类关键字上限 20 字，超长截断（防止 47003）"""
    if nk.startswith('thing') and isinstance(v, dict):
        _val = v.get('value')
        if isinstance(_val, str) and len(_val) > _THING_MAX:
            return {'value': _val[:_THING_MAX]}
    return v


def _remap_subscribe_data(account_id, biz, data):
    """[S343] 按【目标账号+业务】把老字段名重映射成目标模板的真实字段名。

    拿不到映射（账号不在表里 / biz 认不出）时原样返回，行为与改动前完全一致。
    两遍处理：先让"已经是目标字段名"的键占位（兼容新式调用方），
    再把老字段名填进还空着的目标槽；目标槽已被占用时保留先到的值并告警。
    """
    m = _SUBSCRIBE_FIELD_MAP.get((int(account_id or 0), biz or ''))
    if not m:
        return data
    data = data or {}
    out = {}
    for k, v in data.items():
        if m.get(k, k) == k:                 # 已是目标字段名 / 本业务不涉及
            out[k] = _clip_thing(k, v)
    for k, v in data.items():
        nk = m.get(k)
        if nk is None or nk == k:
            continue
        if nk in out:
            logger.warning('[subscribe_msg] 字段映射槽冲突，保留先到的值: %s -> %s', k, nk)
            continue
        out[nk] = _clip_thing(nk, v)
    return out


def _send_subscribe_for_account(account_id, openid, template_id, data, page, phone=None, unionid=None):
    """[S307] 用【指定账号】的身份发订阅消息（新小程序走这条）

    与老逻辑的区别：token 和模板都按该账号取，且不做"往老小程序换 openid"的迁移。
    """
    import requests
    import wx_config as _wc
    try:
        with _wc._conn() as (_c2, _k2):
            _acc = _wc._row(_c2, _k2, "SELECT * FROM wx_accounts WHERE id=?", (account_id,))
    except Exception as e:
        logger.error('[subscribe_msg] 取账号失败 id=%s: %s', account_id, e)
        return False
    if not _acc or not _acc.get('appid') or not _acc.get('secret'):
        logger.error('[subscribe_msg] 账号缺 appid/secret: id=%s', account_id)
        return False
    _tok = get_access_token_for(_acc['appid'], _acc['secret'])
    if not _tok:
        logger.error('[subscribe_msg] 拿不到 token: %s', _acc.get('name'))
        return False
    _tpl = template_id
    _row = None
    _biz = ''
    try:
        _biz = _wc.biz_by_template_id(template_id)
        if _biz:
            _row = _wc.get_template(_biz, 'mp', account_id=account_id)
            if _row and _row.get('template_id'):
                _tpl = _row['template_id']
    except Exception as _e:
        logger.warning('[subscribe_msg] 换模板失败(用原ID): %s', _e)
    # [S343] 调用方按老模板字段名构造 data，这里按目标账号的模板字段重映射一次
    try:
        _rdata = _remap_subscribe_data(account_id, _biz, data)
        if _rdata is not data:
            logger.info('[subscribe_msg] 字段映射 账号id=%s biz=%s %s -> %s',
                        account_id, _biz, sorted((data or {}).keys()), sorted(_rdata.keys()))
            data = _rdata
    except Exception as _e5:
        logger.warning('[subscribe_msg] 字段映射失败(原样发送): %s', _e5)
    body = {'touser': openid, 'template_id': _tpl, 'data': data}
    if page:
        body['page'] = page
    try:
        r = requests.post('https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token=%s' % _tok,
                          json=body, timeout=8)
        _res = r.json()
        if _res.get('errcode') == 0:
            logger.info('[subscribe_msg] OK 账号=%s openid=%s...', _acc.get('name'), (openid or '')[:10])
            return True
        logger.warning('[subscribe_msg] 失败 账号=%s errcode=%s errmsg=%s',
                       _acc.get('name'), _res.get('errcode'), _res.get('errmsg'))
        return False
    except Exception as e:
        logger.error('[subscribe_msg] 异常: %s', e)
        return False


def _oa_tv(v):
    """[S408] 模板消息里的时间统一成 'YYYY-MM-DD HH:MM:SS'。
    Python 的 str(datetime) 带微秒 -> 微信 time 类型会报 47003 data.timeN.value invalid。"""
    try:
        if hasattr(v, 'strftime'):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        s = str(v or '')
        if len(s) >= 19 and s[4] == '-' and (s[10] == ' ' or s[10] == 'T'):
            return s[:19]
        return s
    except Exception:
        return ''


def oa_notify_order_end(order_id=None, amount=None, when=None, openid='', phone='', unionid='', site=''):
    """[S400-20260921] 订单结束 -> 发公众号模板消息【寄存结束 + 退款成功】。
    所有"结束订单"的路径共用这一个入口（用户自己结束取物 / 后台关单 / 离线自动结束 / 设备侧结束）。
    缺的字段按 order_id 自己查；任何异常只记日志，绝不影响业务。
    """
    try:
        # [S525] 平台分流：支付宝单不发微信公众号模板消息
        if order_notify_blocked(order_id=order_id):
            logger.info('[S525] 支付宝单跳过结束订单公众号模板消息 order_id=%s', order_id)
            return False
        from database import get_db as _g
        _o = {}
        if order_id:
            try:
                _c = _g()
                _cu = _c.cursor()
                _cu.execute("""SELECT o.id, o.order_no, o.deposit_amount, o.compartment_number, o.store_time,
                                      o.openid, o.unionid, o.user_phone, o.cabinet_id,
                                      COALESCE(l.name, '') AS site_name
                               FROM orders o
                               LEFT JOIN cabinets c ON o.cabinet_id = c.id
                               LEFT JOIN locations l ON c.location_id = l.id
                               WHERE o.id = %s""", (order_id,))
                _r = _cu.fetchone()
                _c.close()
                if _r:
                    _o = dict(_r)
            except Exception as _e:
                logger.warning('[oa_end] 查订单失败 id=%s: %s', order_id, _e)
        _dep = float(amount if amount is not None else (_o.get('deposit_amount') or 0))
        _site = site or (_o.get('site_name') or '') or '智能寄存柜'
        _oid = openid or (_o.get('openid') or '')
        _ph = phone or (_o.get('user_phone') or '')
        _uni = unionid or (_o.get('unionid') or '')
        _t3 = _oa_tv(when) or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _url = oa_tplmsg_h5_url()
        send_oa_template_message('oa_tplmsg_deposit_end', {
            'thing1': _site,
            'character_string7': str(_o.get('compartment_number') or ''),
            'time2': _oa_tv(_o.get('store_time')),
            'time3': _t3,
            'amount4': '¥{:.2f}'.format(_dep),
        }, openid=_oid, phone=_ph, unionid=_uni, url=_url, order_id=order_id)
        if _dep > 0:
            send_oa_template_message('oa_tplmsg_refund_ok', {
                'amount7': '¥{:.2f}'.format(_dep),
                'time10': _t3,
            }, openid=_oid, phone=_ph, unionid=_uni, url=_url, order_id=order_id)
        return True
    except Exception as _e:
        logger.warning('[oa_end] 异常 order_id=%s: %s', order_id, _e)
        return False


def oa_notify_withdraw_ok(amount=None, when=None, openid='', phone='', unionid='',
                          order_id=None, order_ids=None):
    """[S416-20260921] 用户提现申请提交 -> 发公众号模板消息【提现成功通知】。

    用户口径（2026-09-21）：只要用户在【公众号】里提交了提现就发，
    **不管实际到没到账**。所以挂在 /user/withdraw 的提交成功点，
    「自动审批」与「手动审批」两条路都发。

    模板：卓蓝时「提现成功通知」YbdiBY98Zd5x8HLSQAri8uNT0tnP8f8n5ymV5-9bgCs
    字段：amount1 提现金额 / time2 时间（微信 amount 类字段要带单位，例：30元）
    任何异常只记日志，绝不影响提现本身。
    """
    try:
        # [S525] 平台分流：整张提现单都来自支付宝 -> 不发微信公众号模板消息
        if order_notify_blocked(order_id=order_id, order_ids=order_ids):
            logger.info('[S525] 支付宝单跳过提现公众号模板消息 order_id=%s order_ids=%s', order_id, order_ids)
            return False
        _amt = float(amount or 0)
        _t = _oa_tv(when) or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return send_oa_template_message('oa_tplmsg_withdraw_ok', {
            'amount1': '{:.2f}元'.format(_amt),
            'time2': _t,
        }, openid=openid, phone=phone, unionid=unionid, url=oa_tplmsg_h5_url(),
           order_id=order_id, order_ids=order_ids)
    except Exception as _e:
        logger.warning('[oa_withdraw] 异常: %s', _e)
        return False



# ============================================================
# [S525 2026-09-21] 通知"按平台分流"
#   事故：支付宝的单在【结束订单】时，收件人 openid 是拿手机号去微信库里反查出来的
#         -> 微信小程序订阅消息 + 公众号模板消息发给了支付宝用户
#            （订单 137354 / payment_channel_id=120 / channel_type=alipay）。
#   口径：通知只发给"这笔单自己所属平台的身份"；
#         * 订单带 alipay_mp_uid / alipay_pay_uid，或渠道 channel_type='alipay'
#           -> 判为支付宝单，微信侧通知一律不发（只记日志）；
#              支付宝订阅消息后端暂不具备，留 TODO（见 order_notify_target）。
#         * 其它情况（含判不出来的）-> 完全走原逻辑，保证微信行为一字不变。
# ============================================================
NOTIFY_PLATFORM_WECHAT = 'wechat'
NOTIFY_PLATFORM_ALIPAY = 'alipay'
NOTIFY_PLATFORM_UNKNOWN = 'unknown'


def order_notify_platform(order=None, order_id=None, pay_channel_id=None, cursor=None):
    """[S525] 判断"这笔单该用哪个平台的身份发通知"：'alipay' / 'wechat' / 'unknown'。

    只做【正向判定】：只有明确是支付宝单才返回 'alipay'（此时微信侧通知必须拦掉）；
    判不出来一律 'unknown'，调用方按老逻辑走，绝不改变微信既有行为。
    任何异常都吞掉并按 'unknown' 处理（宁可发得出去，也不能因为判定失败把通知全停）。
    """
    try:
        _o = dict(order) if order else None
        _oid = _o.get('id') if _o else order_id
        _chid = pay_channel_id
        if _o and not _chid:
            _chid = _o.get('payment_channel_id')
        # 1) 订单自带支付宝 uid（支付宝小程序登录桥接 / 支付宝支付回调桥接）-> 铁证
        if _o and (str(_o.get('alipay_mp_uid') or '').strip() or str(_o.get('alipay_pay_uid') or '').strip()):
            return NOTIFY_PLATFORM_ALIPAY
        # 2) 渠道类型 = alipay
        if _chid or _oid:
            _c = cursor
            _own = False
            try:
                if _c is None:
                    from database import get_db as _g525
                    _c = _g525()
                    _own = True
                _cur = _c.cursor()
                if _chid:
                    _cur.execute("SELECT channel_type FROM payment_channels WHERE id=%s", (_chid,))
                    _r = _cur.fetchone()
                    if _r:
                        _ct = _r.get('channel_type') if hasattr(_r, 'get') else _r[0]
                        if _ct == 'alipay':
                            return NOTIFY_PLATFORM_ALIPAY
                elif _oid:
                    # [S525] 有渠道 id 时就以渠道结论为准，不再多查一次 orders（少一次查询）
                    _cur.execute("""SELECT pc.channel_type AS ct
                                    FROM orders o
                                    LEFT JOIN payment_channels pc ON pc.id = o.payment_channel_id
                                    WHERE o.id=%s""", (_oid,))
                    _r = _cur.fetchone()
                    if _r:
                        _ct = _r.get('ct') if hasattr(_r, 'get') else _r[0]
                        if _ct == 'alipay':
                            return NOTIFY_PLATFORM_ALIPAY
            finally:
                if _own and _c is not None:
                    try:
                        _c.close()
                    except Exception:
                        pass
        # 3) 订单自带微信身份 -> wechat（仅作正向信号，绝不按手机号反查）
        if _o and (str(_o.get('mp_openid') or '').strip() or str(_o.get('openid') or '').strip()
                   or str(_o.get('unionid') or '').strip()):
            return NOTIFY_PLATFORM_WECHAT
        return NOTIFY_PLATFORM_UNKNOWN
    except Exception as _e:
        logger.warning('[S525] order_notify_platform 异常(按 unknown 处理): %s', _e)
        return NOTIFY_PLATFORM_UNKNOWN


def order_notify_blocked(order=None, order_id=None, order_ids=None, pay_channel_id=None, cursor=None):
    """[S525] 微信侧通知闸门：这笔单是支付宝的 -> True（必须拦掉，绝不按手机号找微信身份）。

    order_ids: 合并提现单场景（一张提现单里多个订单）。只有【全部】订单都是支付宝单才拦；
               只要有一笔微信单，说明这次通知本身就属于微信侧，照旧发（不回归）。
    """
    try:
        if order_ids:
            _pf = [order_notify_platform(order_id=_i, cursor=cursor) for _i in list(order_ids)]
            return bool(_pf) and all(_p == NOTIFY_PLATFORM_ALIPAY for _p in _pf)
        return order_notify_platform(order=order, order_id=order_id,
                                     pay_channel_id=pay_channel_id, cursor=cursor) == NOTIFY_PLATFORM_ALIPAY
    except Exception as _e:
        logger.warning('[S525] order_notify_blocked 异常(按不拦处理): %s', _e)
        return False


def order_notify_target(order=None, order_id=None, cursor=None):
    """[S525] 统一"取这笔单自己所属平台的通知身份"。通知调用点应当【先问它】再发。

    返回 dict:
      {'platform': 'wechat'|'alipay'|'unknown', 'can_send': bool,
       'openid': '', 'mp_openid': '', 'unionid': '', 'phone': '',
       'alipay_uid': '', 'reason': ''}

    规则（关键：绝不跨平台按手机号反查）：
      * 支付宝单 -> can_send=False（后端暂无支付宝订阅消息能力，TODO: 接支付宝订阅消息）；
                    只把 alipay uid 带出来，微信侧任何 openid 都不返回。
      * 微信单   -> 只用订单自带的 openid/mp_openid/unionid；订单没带时 phone 原样带出，
                    由 send_wx_subscribe_message / send_oa_template_message 里【原有的】
                    手机号反查逻辑兜底 —— 微信单保持原行为不变。
      * unknown  -> 老行为（phone 带出，can_send=True）。
    """
    try:
        _o = dict(order) if order else {}
        if not _o and order_id:
            try:
                from database import get_db as _g525t
                _c = cursor or _g525t()
                _cur = _c.cursor()
                _cur.execute("""SELECT o.id, o.openid, o.mp_openid, o.unionid, o.user_phone,
                                       o.alipay_mp_uid, o.alipay_pay_uid, o.payment_channel_id
                                FROM orders o WHERE o.id=%s""", (order_id,))
                _r = _cur.fetchone()
                if _r:
                    _o = dict(_r)
            except Exception as _e:
                logger.warning('[S525] order_notify_target 查订单失败 id=%s: %s', order_id, _e)
        _pf = order_notify_platform(order=_o, order_id=order_id, cursor=cursor)
        if _pf == NOTIFY_PLATFORM_ALIPAY:
            return {'platform': NOTIFY_PLATFORM_ALIPAY, 'can_send': False,
                    'openid': '', 'mp_openid': '', 'unionid': '', 'phone': '',
                    'alipay_uid': str(_o.get('alipay_mp_uid') or _o.get('alipay_pay_uid') or ''),
                    'reason': 'alipay_order_no_wechat_notify'}
        return {'platform': _pf, 'can_send': True,
                'openid': str(_o.get('openid') or ''), 'mp_openid': str(_o.get('mp_openid') or ''),
                'unionid': str(_o.get('unionid') or ''), 'phone': str(_o.get('user_phone') or ''),
                'alipay_uid': '', 'reason': ''}
    except Exception as _e:
        logger.warning('[S525] order_notify_target 异常(按老行为): %s', _e)
        return {'platform': NOTIFY_PLATFORM_UNKNOWN, 'can_send': True,
                'openid': '', 'mp_openid': '', 'unionid': '', 'phone': '',
                'alipay_uid': '', 'reason': 'exception'}


# ============================================================
# [S526-20260921] 支付宝小程序【订阅消息】发送
#   与微信 send_wx_subscribe_message 一一对应，但口径不同：
#     · 收件人 = 支付宝 user_id（users.alipay_uid / phone_openids.alipay_uid），不是 openid
#     · 模板走 wx_templates 里 channel='alipay' 的两条（account_id=0 通用）
#         subscribe_general = c142ac2357774daab8994a0f5a91faa4  账户余额通知
#         subscribe_refund  = de68d98e94c84477b1b9e116fcb8cbfa  寄存押金退还通知
#       调用方取模板ID：wx_config.template_id('subscribe_general', 'alipay', '')
#     · data 的关键词名是 keyword1..keywordN，名称/顺序由"申请模板时选的关键词"决定
#   安全口径：任何异常只记日志、绝不抛出；alipay_uid 为空直接返回 False。
# ============================================================
_ALIPAY_TPL_GENERAL = 'c142ac2357774daab8994a0f5a91faa4'   # 账户余额通知（兜底值）
_ALIPAY_TPL_REFUND = 'de68d98e94c84477b1b9e116fcb8cbfa'    # 寄存押金退还通知（兜底值）

# 微信字段名 -> 支付宝关键词名。
#   ★ 值【待老板提供 / 待实测】：库里 wx_templates.fields 是 {}，项目文档里也没有关键词说明，
#     所以两条先留空 {}。留空 = 不做任何转换，调用方直接给 keyword1..keywordN（最安全的默认）。
#   现有微信字段（来自各处 send_wx_subscribe_message 调用点）：
#     subscribe_general：amount1 金额 / time2 时间 / thing4 变动原因 / thing3 温馨提示
#     subscribe_refund ：amount2 金额 / time5 时间 / thing4 退款方式 / thing3 备注
_ALIPAY_SUBSCRIBE_FIELD_MAP = {
    _ALIPAY_TPL_GENERAL: {},
    _ALIPAY_TPL_REFUND: {},
}

# [S530-20260921] 微信字段名 -> biz：调用方没给 biz 时，用 data 里带的微信字段名反推
#   （这两组字段来自各处 send_wx_subscribe_message 调用点，见上面 _ALIPAY_SUBSCRIBE_FIELD_MAP 注释）
_ALIPAY_TPL_BIZ_HINTS = (
    ('amount1', 'subscribe_general'),
    ('time2', 'subscribe_general'),
    ('amount2', 'subscribe_refund'),
    ('time5', 'subscribe_refund'),
)


def alipay_subscribe_template_id(biz, default=''):
    """[S530-20260921] 直接查库取支付宝【订阅消息】模板ID（绕开配置中心）

    背景（本次修的 bug）：
      wx_config.template_id(biz, 'alipay', '') 对 alipay 通道**永远返回空**——
      因为配置中心 CHANNELS = ('mp', 'oa')，get_template 会把 channel='alipay' 过滤掉。
      发送端拿到空模板ID → 直接 return False，订阅消息一条也发不出去。

    做法：只读 wx_templates（不改配置中心、不加新表、不写库）：
        SELECT template_id FROM wx_templates
         WHERE biz=%s AND channel='alipay' AND is_active=1
         ORDER BY CASE WHEN account_id=0 THEN 0 ELSE 1 END, id LIMIT 1
      （优先 account_id=0 的通用模板，其次 id 最小的）

    取不到 / biz 为空 -> 返回 default；任何异常只 warning 并返回 default，**绝不抛出**。
    """
    _d = str(default or '')
    try:
        _biz = str(biz or '').strip()
        if not _biz:
            return _d
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT template_id FROM wx_templates "
                "WHERE biz=%s AND channel='alipay' AND is_active=1 "
                "ORDER BY CASE WHEN account_id=0 THEN 0 ELSE 1 END, id LIMIT 1",
                (_biz,))
            row = cur.fetchone()
            try:
                cur.close()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not row:
            logger.warning('[alipay_subscribe] wx_templates 里没有可用模板 biz=%s channel=alipay', _biz)
            return _d
        try:
            _tid = row['template_id']
        except Exception:
            _tid = row[0]
        _tid = str(_tid or '').strip()
        if not _tid:
            return _d
        logger.info('[alipay_subscribe] 模板ID取自 wx_templates biz=%s -> %s...', _biz, _tid[:8])
        return _tid
    except Exception as e:
        logger.warning('[alipay_subscribe] 取模板ID异常 biz=%s: %s', biz, e)
        return _d


def alipay_subscribe_guess_biz(template_id='', data=None):
    """[S530-20260921] 调用方没给 biz 时，尽量从函数入参反推 biz（推不出返回 ''）

    顺序：
      1) template_id 就是本项目两个已知模板ID -> 直接对应
      2) template_id 不是 32 位 hex（= 对方把 biz 名当模板ID传了）-> 当 biz 用
      3) data 里显式带了 biz / _biz
      4) data 里带了微信字段名（amount1/time2 -> general, amount2/time5 -> refund）
    """
    _t = str(template_id or '').strip()
    if _t == _ALIPAY_TPL_GENERAL:
        return 'subscribe_general'
    if _t == _ALIPAY_TPL_REFUND:
        return 'subscribe_refund'
    if _t and not (len(_t) == 32 and all(c in '0123456789abcdef' for c in _t.lower())):
        return _t
    if isinstance(data, dict):
        for _k in ('biz', '_biz'):
            _v = str(data.get(_k) or '').strip()
            if _v:
                return _v
        for _field, _biz in _ALIPAY_TPL_BIZ_HINTS:
            if _field in data:
                return _biz
    return ''


def alipay_subscribe_data(template_id, data):
    """[S526] 把 data 规整成支付宝要的关键词字典（序列化交给 AlipayClient）

    · 映射表里没有该模板 / 映射为空 -> 原样返回，不做任何猜测；
    · 映射表里有 -> 只挑映射到的键改名，未映射的键丢弃（防止多传关键词被拒）。
    """
    if not isinstance(data, dict):
        return data
    m = _ALIPAY_SUBSCRIBE_FIELD_MAP.get(str(template_id or '')) or {}
    if not m:
        return data
    out = {}
    for k, v in data.items():
        nk = m.get(k)
        if nk:
            out[nk] = v
    return out


def send_alipay_subscribe_message(alipay_uid, template_id, data, page='pages/mine/mine',
                                  dry_run=False, biz=''):
    """[S526] 发送支付宝小程序订阅消息（对应微信的 send_wx_subscribe_message）

    入参：
      alipay_uid  = 用户支付宝 user_id（users.alipay_uid / phone_openids.alipay_uid）
      template_id = wx_templates channel='alipay' 里那条的 template_id；
                    取法：wx_config.template_id('subscribe_general'|'subscribe_refund', 'alipay', '')
                    ★ [S530] 但该取法对 alipay 通道**永远返回空**（配置中心 CHANNELS 只有 mp/oa）。
                      为兼容旧调用方，参数保持原样；**传空时本函数自动按 biz 查库兜底**。
      data        = dict，推荐 {'keyword1': {'value': '¥30.00'}, 'keyword2': {'value': '2026-09-21 21:00'}}
                    （也接受微信字段名，前提是 _ALIPAY_SUBSCRIBE_FIELD_MAP 里配好了映射）
      page        = 点击消息跳转的小程序页，默认 pages/mine/mine
      dry_run     = True 时只构造 + 签名、不发网络请求（离线自检用）
      biz         = [S530] 可选。'subscribe_general' / 'subscribe_refund'；
                    template_id 为空时按它查库取模板ID。不给则由 data 入参反推。

    返回：dry_run=False -> True/False；dry_run=True -> 参数字典。**绝不抛异常。**
    """
    try:
        alipay_uid = str(alipay_uid or '').strip()
        if not alipay_uid:
            logger.warning('[alipay_subscribe] alipay_uid 为空，跳过发送')
            return False
        template_id = str(template_id or '').strip()
        if not template_id:
            # [S530-20260921] 空模板ID -> 直接查 wx_templates 兜底（原来这里直接放弃发送）
            _bz = str(biz or '').strip() or alipay_subscribe_guess_biz('', data)
            if _bz:
                template_id = alipay_subscribe_template_id(_bz, '')
                logger.info('[alipay_subscribe] 入参 template_id 为空，按 biz=%s 查库兜底 -> %s',
                            _bz, (template_id[:8] + '...') if template_id else '(仍为空)')
        if not template_id:
            logger.warning('[alipay_subscribe] template_id 为空，跳过发送 uid=%s...', alipay_uid[:8])
            return False
        # 疑似把微信模板ID发到支付宝：微信模板ID是43位 base64url，支付宝是32位 hex。
        # 只告警不拦截（真发错了支付宝会回 USER_TEMPLATE_ILLEGAL，不会投递）。
        _tid_l = template_id.lower()
        if not (len(template_id) == 32 and all(c in '0123456789abcdef' for c in _tid_l)):
            logger.warning('[alipay_subscribe] template_id 不是32位hex（疑似微信模板ID）: %s', template_id)
        # 应急开关（默认开；库里没有这个 key 时 get_config 返回默认值 'true'）
        try:
            import wx_config as _wc526
            if str(_wc526.get_config('alipay_subscribe_enabled', 'true')).strip().lower() in ('0', 'false', 'off', 'no'):
                logger.info('[alipay_subscribe] 开关 alipay_subscribe_enabled=off，跳过 uid=%s...', alipay_uid[:8])
                return False
        except Exception:
            pass
        client = get_alipay_mp_client()
        if client is None:
            logger.error('[alipay_subscribe] 支付宝小程序客户端不可用（密钥文件缺失？）')
            return False
        _d = alipay_subscribe_data(template_id, data)
        _res = client.mini_template_message_send(alipay_uid, template_id,
                                                page or 'pages/mine/mine', _d, dry_run=dry_run)
        if dry_run:
            return _res
        if str(_res.get('code')) == '10000':
            logger.info('[alipay_subscribe] 发送成功 uid=%s... template=%s...',
                        alipay_uid[:8], template_id[:8])
            return True
        logger.error('[alipay_subscribe] 发送失败 uid=%s... template=%s... code=%s sub_code=%s sub_msg=%s',
                     alipay_uid[:8], template_id[:8], _res.get('code'),
                     _res.get('sub_code'), _res.get('sub_msg'))
        return False
    except Exception as e:
        logger.error('[alipay_subscribe] 异常: %s', e)
        return False



# ============================================================
# [S533-20260921] 模板ID按"实际发信账号"纠正
#   事故：2026-09-21 23:15:13 order=137358 openid=oXTD3xYN...(新小程序 账号11 卓蓝时)
#         template=ax-O5Qa... -> 微信 40037 invalid template_id，小程序订阅消息静默丢失
#         （同一时刻公众号模板消息发成功，因为 oa 模板是按账号10 专属配的）
#   机制：所有调用点都用 wx_config.template_id(biz,'mp',老ID) 取模板，而
#         template_id() -> get_template(biz, channel) **不传 account_id**，
#         永远返回 account_id=0 的通用行（= 老小程序的模板ID）。生效小程序换成
#         账号11 后，这条ID在账号11 里根本不存在 -> 40037。
#   做法：发信前按 openid 所属账号再查一次同 biz 的专属模板；查到且不同就换掉。
#   安全：任何异常 / 查不到 / 取到同一个ID -> 原样返回，行为与改动前完全一致。
#         本函数只查库、不发任何请求。
# ============================================================
def _wx_fix_subscribe_template(openid, template_id):
    """[S533] 把"不区分账号"取到的模板ID，纠正成 openid 所属小程序自己的模板ID。"""
    _tid = str(template_id or '').strip()
    _oid = str(openid or '').strip()
    if not _tid or not _oid:
        return template_id
    try:
        import wx_config as _wc533
        try:
            _aid = int(_wc533.account_id_by_openid(_oid) or 0)
        except Exception:
            _aid = 0
        if not _aid:
            try:
                _eff533 = _wc533.get_effective_account('mp') or {}
                _aid = int(_eff533.get('id') or 0)
            except Exception:
                _aid = 0
        if not _aid:
            return template_id
        _biz533 = _wc533.biz_by_template_id(_tid)
        if not _biz533:
            return template_id
        _row533 = _wc533.get_template(_biz533, 'mp', account_id=_aid)
        _new533 = str((_row533 or {}).get('template_id') or '').strip()
        if not _new533 or _new533 == _tid:
            return template_id
        logger.info('[S533] 模板ID按账号纠正: account_id=%s biz=%s %s... -> %s...',
                    _aid, _biz533, _tid[:8], _new533[:8])
        return _new533
    except Exception as _e533:
        logger.warning('[S533] 模板ID纠正失败(用原ID): %s', _e533)
        return template_id


def send_wx_subscribe_message(openid, template_id, data, page='', phone=None, unionid=None,
                              order_id=None, order_ids=None, pay_channel_id=None):
    """发送微信订阅消息（仅支持小程序mp_openid）

    [S307] 多小程序分流：先按 openid 前缀判断用户属于哪个小程序。
      若属于"非当前生效账号"（= 新小程序），走 _send_subscribe_for_account（用该小程序的 token + 模板）。
      否则（= 老小程序 / 判断不出）走下面原有的全部逻辑，行为一字不变。
    """
    # [S525] 平台分流闸门：支付宝单绝不按手机号反查微信身份
    if order_notify_blocked(order_id=order_id, order_ids=order_ids, pay_channel_id=pay_channel_id):
        logger.info('[S525] 支付宝单跳过微信订阅消息(不按手机号反查微信身份) order_id=%s order_ids=%s openid=%s...',
                    order_id, order_ids, str(openid or '')[:8])
        return False
    # [S420-20260921] 订阅通知不再靠人工"全局关"，改为【跟随入口模式自动联动】：
    #   纯公众号(oa) / 纯支付宝(alipay) -> 小程序订阅消息根本没有发送场景，自动跳过；
    #   其它模式(mp / h5) -> 正常发送。
    #   wx_config_items.mp_subscribe_enabled 仍然尊重，但默认 true：
    #   只有显式设成 false 才额外全关（留给运维一个应急开关，而不是"想全关只能改代码"）。
    #   历史：S400 曾把它设成 false 把小程序订阅消息全停（当时切纯公众号），
    #   切回 H5 跳小程序时必须自动恢复，不用人工记得去开。
    try:
        import wx_config as _wc_sw
        _em420 = ''
        try:
            from entry_mode import get_entry_mode as _gem420
            _em420 = _gem420()
        except Exception:
            _em420 = ''
        if _em420 in ('oa', 'alipay'):
            logger.info('[subscribe_msg] 入口模式=%s（无小程序场景），跳过小程序订阅消息 openid=%s...',
                        _em420, str(openid or '')[:8])
            return False
        if str(_wc_sw.get_config('mp_subscribe_enabled', 'true')).strip().lower() in ('0', 'false', 'off', 'no'):
            logger.info('[subscribe_msg] 小程序订阅消息手动应急开关=off，跳过 openid=%s...', str(openid or '')[:8])
            return False
    except Exception:
        pass
    try:
        import wx_config as _wc0
        _aid = 0
        try:
            _aid = _wc0.account_id_by_openid(openid or '')
            _eff = _wc0.get_effective_account('mp') or {}
            _eff_id = _eff.get('id') or 0
        except Exception:
            _aid, _eff_id = 0, 0
        if _aid and _eff_id and _aid != _eff_id:
            logger.info('[subscribe_msg] openid 属于其它小程序(账号id=%s, 当前生效=%s)，走新路径', _aid, _eff_id)
            return _send_subscribe_for_account(_aid, openid, template_id, data, page, phone, unionid)
    except Exception as _e0:
        logger.warning('[subscribe_msg] 小程序分流判断失败(按原逻辑): %s', _e0)

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
                if _ub_row and _ub_row.get('mp_openid') and _ub_row['mp_openid'] not in ('', None) and not _ub_row['mp_openid'].startswith(oa_openid_prefix()):
                    openid = _ub_row['mp_openid']
                if not openid:
                    _po_rows = phone_openid_rows(_cur, phone=phone, unionid=unionid or '')
                    if len(_po_rows) == 1 and _po_rows[0].get('mp_openid') and not _po_rows[0]['mp_openid'].startswith(oa_openid_prefix()):
                        openid = _po_rows[0]['mp_openid']
                    elif len(_po_rows) > 1 and not unionid:
                        logger.warning(f'[subscribe_msg] 手机号绑定多个微信，缺少unionid，不猜测: phone={phone}')

                if not openid:
                    _cur.execute("""
                        SELECT mp_openid, unionid FROM phone_openids
                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL
                          AND mp_openid NOT LIKE %s
                        ORDER BY id ASC
                    """, (phone, oa_openid_prefix() + '%'))
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
        if openid and phone and not openid.startswith(mp_openid_prefix()):
            try:
                _conn4 = get_db()
                _cur4 = _conn4.cursor()
                _r4 = None
                _ub4 = find_user_balance_row(_cur4, phone=phone, unionid=unionid or '')
                if _ub4 and _ub4.get('mp_openid') and str(_ub4['mp_openid']).startswith(mp_openid_prefix()):
                    _r4 = (_ub4['mp_openid'],)
                if not _r4:
                    _po4 = phone_openid_rows(_cur4, phone=phone, unionid=unionid or '')
                    for _rr4 in _po4:
                        if _rr4.get('mp_openid') and str(_rr4['mp_openid']).startswith(mp_openid_prefix()):
                            _r4 = (_rr4['mp_openid'],)
                            break
                if not _r4:
                    _cur4.execute("""
                        SELECT mp_openid FROM phone_openids
                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE %s
                        ORDER BY id ASC LIMIT 1
                    """, (phone, mp_openid_prefix() + '%'))
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
                        WHERE unionid = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE %s
                        ORDER BY id ASC LIMIT 1
                    """, (unionid, mp_openid_prefix() + '%'))
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
        if openid and openid.startswith(oa_openid_prefix()) and phone:
            try:
                _conn3 = get_db()
                _cur3 = _conn3.cursor()
                _r3 = None
                _ub3 = find_user_balance_row(_cur3, phone=phone, unionid=unionid or '')
                if _ub3 and _ub3.get('mp_openid') and _ub3['mp_openid'].startswith(mp_openid_prefix()):
                    _r3 = (_ub3['mp_openid'],)
                if not _r3:
                    _po3 = phone_openid_rows(_cur3, phone=phone, unionid=unionid or '')
                    if len(_po3) == 1 and _po3[0].get('mp_openid') and _po3[0]['mp_openid'].startswith(mp_openid_prefix()):
                        _r3 = (_po3[0]['mp_openid'],)
                _conn3.close()
                if _r3 and _r3[0]:
                    openid = _r3[0]
            except Exception as _e3:
                logger.warning(f'[subscribe_msg] 纠正openid失败: {_e3}')
        if openid and not openid.startswith(mp_openid_prefix()):
            logger.warning(f'[subscribe_msg] 跳过公众号openid: openid={openid[:8]}..., phone={phone}')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='只有旧号/公众号openid')
            return False
        if not openid:
            logger.warning(f'[subscribe_msg] mp_openid为空，跳过发送（phone={phone}）')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='没有小程序openid')
            return False

        # [S533-20260921] 模板ID按 openid 所属小程序纠正（不区分账号取模板会 40037）
        template_id = _wx_fix_subscribe_template(openid, template_id)
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
            # [S231] 过渡期回退：换成新"账户余额"模板后，老用户只有旧"押金退还"模板的授权 ->
            #        新模板必然被拒(43101)，这里用旧模板再发一次；过渡期结束(大家都重新授权过)可去掉这段。
            if template_id == _TPL_ACCOUNT_NEW:
                try:
                    _p2 = {'touser': openid, 'template_id': _TPL_DEPOSIT_OLD, 'data': data}
                    if page:
                        _p2['page'] = page
                    _r2 = requests.post(send_url, json=_p2, timeout=5).json()
                    if _r2.get('errcode') == 0:
                        logger.info(f'[subscribe_msg] 新模板失败->旧模板发送成功: openid={openid[:8]}...')
                        return True
                    logger.error(f'[subscribe_msg] 旧模板也失败: openid={openid[:8]}..., result={_r2}')
                except Exception as _e2:
                    logger.error(f'[subscribe_msg] 旧模板回退异常: {_e2}')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='errcode=%s' % result.get('errcode'))
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
        # [S541-20260922] 支付宝排除改为【开关控制】(withdraw_refund_alipay，默认 0=维持现状)：
        #   关(默认)时下面拼出的 SQL 与改动前逐字节相同；开时不再排除支付宝单，
        #   使"用户看到的可提现金额"与"提现实际能取到的订单"一致(S273 临时隔离正名)。
        try:
            _s541_alipay_excl = ''
            if not withdraw_refund_alipay_enabled():
                _s541_alipay_excl = ("AND NOT EXISTS (SELECT 1 FROM payment_channels pc WHERE pc.id = o.payment_channel_id AND pc.channel_type = 'alipay') ")
        except Exception:
            _s541_alipay_excl = ("AND NOT EXISTS (SELECT 1 FROM payment_channels pc WHERE pc.id = o.payment_channel_id AND pc.channel_type = 'alipay') ")

        sql = (
            "SELECT COALESCE(SUM(bd.amount), 0) FROM user_balance_details bd "
            "JOIN orders o ON bd.order_id = o.id "
            "WHERE bd.status = 'available' AND o.status = 3 AND (" + where + ") "
            # [S273] 渠道隔离(方案B)：支付宝渠道付的押金【不进】微信余额。
            #   用"排除支付宝"而非"只算微信"，这样 payment_channel_id 为空的老订单行为完全不变。
            + _s541_alipay_excl +
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
