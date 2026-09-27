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
# [S657-20260924] S531 的三个判断条件(标题 / 去重 key / "无其它活跃渠道")全部保留不动;
#   本次只加"按 err_code_des 分流": 产品级(该产品权限未开通)不再当作账号被封告警。
#   生产实证: system_settings['[S531]mch_restricted_alert'] 里 121/122/124 三个渠道
#   最后一次告警的 desc 都是"商户号该产品权限未开通", 却发了"商户号被封/收款受限"。
_MCH_RESTRICTED_ALERT_KEY = '[S531]mch_restricted_alert'
_MCH_RESTRICTED_ALERT_GAP = 600      # 同渠道 10 分钟内只告警一次

# [S657-20260924] 未知 NOAUTH 的中性告警单独一把去重锁, 不污染 [S531] 那把
_MCH_NOAUTH_ALERT_KEY = '[S657]mch_noauth_alert'

# [S657-20260924] trade_type -> 人话产品名(告警正文与落库都用它)
_MCH_PRODUCT_NAMES = {'JSAPI': '小程序支付', 'MWEB': 'H5支付', 'NATIVE': '扫码支付'}

# [S657-20260924] 被动记录的 key 前缀 + err_desc 截断长度(避免 setting_value 无限膨胀)
_MCH_LAST_ERROR_KEY_PREFIX = 'mch_last_error_'
_MCH_LAST_ERROR_DESC_MAX = 200

# [S657-20260924] 分类枚举(固定 6 值)
_MCH_ERR_CLASSES = ('ok', 'account_restricted', 'product_not_open',
                    'appid_mchid_mismatch', 'sign_error', 'other')


def _mch_product_of(trade_type):
    """trade_type -> 人话产品名; 认不出来原样返回; 空 -> 未知"""
    _tt = (trade_type or '').strip()
    if not _tt:
        return '未知'
    return _MCH_PRODUCT_NAMES.get(_tt.upper(), _tt)


def _mch_noauth_kind(err_code, err_desc):
    """[S657] 只对 NOAUTH/NO_AUTH 按 err_code_des 细分; 其余返回 None(交回原有行为)。

      'account'        账号级: 收款功能已被限制 / 暂无法支付  -> 这才是"商户被限制收款"
      'product'        产品级: 该产品权限未开通              -> 只是这个支付产品没开通
      'unknown_noauth' 其它 NOAUTH 描述                      -> 中性告警
    """
    if (err_code or '').upper() not in ('NOAUTH', 'NO_AUTH'):
        return None
    _ed = err_desc or ''
    if ('收款功能已被限制' in _ed) or ('暂无法支付' in _ed):
        return 'account'
    if '该产品权限未开通' in _ed:
        return 'product'
    return 'unknown_noauth'


def _classify_mch_err(err_code, err_desc, return_code=None, result_code=None):
    """[S657] 把一次微信下单结果归类为 _MCH_ERR_CLASSES 之一(纯函数, 不碰库不联网)"""
    if return_code == 'SUCCESS' and result_code == 'SUCCESS':
        return 'ok'
    _ec = (err_code or '').upper()
    _ed = err_desc or ''
    _kind = _mch_noauth_kind(_ec, _ed)
    if _kind == 'account':
        return 'account_restricted'
    if _kind == 'product':
        return 'product_not_open'
    if _kind == 'unknown_noauth':
        return 'other'
    if _ec == 'APPID_MCHID_NOT_MATCH':
        return 'appid_mchid_mismatch'
    if _ec in ('SIGN_ERROR', 'SIGNERROR') or ('签名错误' in _ed):
        return 'sign_error'
    return 'other'


def _alert_mch_restricted(channel, err_code, err_desc, trade_type=None):
    """微信回 NOAUTH/收款受限 -> 若无其他可用微信渠道则告警(不产生任何支付副作用)

    [S657-20260924] 修掉"产品级未开通被报成账号级被封"(生产实锤见 S657 报告):
      · 账号级(收款功能已被限制/暂无法支付): [S531] 原行为原文案逐字节保留
        (标题 / 10 分钟去重 / "无其它活跃微信渠道"闸门 / 去重 key 全不动), 正文只追加一行"失败产品"
      · 产品级(该产品权限未开通): 只记日志, 一律【不告警】—— 这正是本次要消灭的误导
      · 其它 NOAUTH: 中性文案(不写"被封"、不给产品级/账号级结论)
      · 非 NOAUTH(MCH_NOT_EXIST / APPID_MCHID_NOT_MATCH / 签名错误 等): 行为逐字节不变
    """
    import json as _json
    import time as _time
    try:
        cid = (channel or {}).get('id')
        cname = (channel or {}).get('name', '未知')
        cmch = (channel or {}).get('mch_id', '未知')
        _kind = _mch_noauth_kind(err_code, err_desc)
        _prod = _mch_product_of(trade_type)

        # [S657] 产品级未开通: 不是账号问题, 只记日志; 落库交给 _record_mch_last_error, 绝不告警
        if _kind == 'product':
            logger.warning('[MchRestricted] 渠道 %s 商户号该产品权限未开通(%s), 属产品级, 不告警: %s'
                           % (cid, _prod, err_desc))
            return False

        # [S657] 账号级/非 NOAUTH 沿用 [S531] 那把锁; 其它 NOAUTH 用单独一把, 互不干扰
        _alert_key = _MCH_RESTRICTED_ALERT_KEY if _kind != 'unknown_noauth' else _MCH_NOAUTH_ALERT_KEY

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
                    (_alert_key,))
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

        if _kind == 'unknown_noauth':
            # [S657] 中性文案: 只报事实, 不下"被封"/"产品未开通"任何一种结论
            title = '【寄存柜】微信商户号下单失败(NOAUTH)'
            content = ('微信商户号下单返回 NOAUTH（未归类）。\n'
                       '商户名称: %s\n'
                       '商户号(mch_id): %s\n'
                       '渠道ID: %s\n'
                       '失败产品: %s\n'
                       '错误码: %s\n'
                       '错误描述: %s\n'
                       '\n该描述不属于已知的两种 NOAUTH 情形，请人工确认商户号状态。') % (
                cname, cmch, cid, _prod, err_code, err_desc)
        else:
            # [S531] 原文案逐字节保留(账号级 / 非 NOAUTH 都走这里)
            title = '【寄存柜】微信商户号被封/收款受限'
            content = ('微信支付商户号被限制收款，且当前已无其他可用微信渠道。\n'
                       '商户名称: %s\n'
                       '商户号(mch_id): %s\n'
                       '渠道ID: %s\n'
                       '错误码: %s\n'
                       '错误描述: %s\n'
                       '\n请立刻登录 pay.weixin.qq.com 查看，并到后台"支付渠道"启用备用商户号。') % (
                cname, cmch, cid, err_code, err_desc)
            # [S657] 只有账号级才追加这一行(非 NOAUTH 保持逐字节不变)
            if _kind == 'account':
                content = content + '\n失败产品: %s' % _prod
        ok = send_pushplus(title, content)
        st[str(cid)] = {'ts': now, 'name': cname, 'mch': cmch, 'err': err_code, 'desc': err_desc}
        try:
            cur.execute(
                "INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s) "
                "ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                (_alert_key, _json.dumps(st, ensure_ascii=False)))
            conn.commit()
        except Exception as _we:
            logger.warning('[MchRestricted] 告警状态写库失败: %s' % _we)
        conn.close()
        logger.warning('[MchRestricted] 告警已发(%s): %s' % ('成功' if ok else '失败', title))
        return ok
    except Exception as e:
        logger.error('[MchRestricted] 告警失败: %s' % e)
        return False


def _record_mch_last_error(channel, result, trade_type, source, order_no=None):
    """[S657-20260924] 把【真实下单】的结果被动记到 system_settings.mch_last_error_<渠道id>。

    老板口径(2026-09-24): 不做定时主动探针, 只用"真实下单时微信回的报错"判断商户状态。
      · 只记录被真实流量尝试过的渠道; 不遍历/不探测停用渠道、不写 payment_channels、不做 DDL
      · 任何异常都在函数内吞掉只记日志 —— 绝不影响支付链路
      · 分类见 _classify_mch_err(); 产品名见 _mch_product_of()
      · account_restricted -> 立即告警(复用 [S531] 原文案/原去重/原"无其它渠道"闸门)
      · product_not_open   -> 只落库, 不告警(消灭"产品未开通被报成被封"的误导)
      · ok                 -> 也落一笔(用于看"最后成功时间")
    返回分类字符串(出错时返回 'other')
    """
    import json as _json
    import time as _time
    try:
        cid = (channel or {}).get('id')
        if not cid:
            return 'other'
        result = result or {}
        _rc = result.get('return_code')
        _res = result.get('result_code')
        _ec = result.get('err_code') or ''
        _ed = result.get('err_code_des') or result.get('return_msg') or ''
        _cls = _classify_mch_err(_ec, _ed, _rc, _res)
        _payload = {
            'at': int(_time.time()),
            'product': _mch_product_of(trade_type),
            'trade_type': (trade_type or ''),
            'class': _cls,
            'err_code': _ec,
            'err_desc': str(_ed)[:_MCH_LAST_ERROR_DESC_MAX],
            'source': source,
            'order_no': (order_no or ''),
            'return_code': _rc or '',
            'result_code': _res or '',
            'mch_id': (channel or {}).get('mch_id', ''),
            'name': (channel or {}).get('name', ''),
        }
        try:
            from database import get_db as _gdb657
            _c657 = _gdb657()
            _cur657 = _c657.cursor()
            _cur657.execute(
                "INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s) "
                "ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                (_MCH_LAST_ERROR_KEY_PREFIX + str(cid), _json.dumps(_payload, ensure_ascii=False)))
            _c657.commit()
            _c657.close()
        except Exception as _we657:
            logger.warning('[S657] 落库 mch_last_error 失败(不影响支付): %s' % _we657)
        if _cls == 'account_restricted':
            # 账号级 -> 立即告警(不等到巡检; 文案/去重/闸门沿用 [S531] 原逻辑)
            try:
                _alert_mch_restricted(channel, _ec, _ed, trade_type)
            except Exception as _ae657:
                logger.error('[S657] 账号级告警失败: %s' % _ae657)
        return _cls
    except Exception as _e657:
        logger.error('[S657] _record_mch_last_error 异常(不影响支付): %s' % _e657)
        return 'other'

_mch_fail_poll_count = {}


# [S421-20260921] 账单里显示的【商品名称】= 微信统一下单的 body。
#   [S574-20260922 20260922_143627] 老板要求：把客服电话从商品名里【去掉】，恢复成纯商品名。
#   原因：电话/营销词（原「人工加急电话：4006981080」）属微信支付商品描述的风控弱信号，
#   而全站微信收款只有一个商户号（1750896171），一旦被限制=全站收不到钱，风险收益不划算。
#   只影响【下单时】的商品名 -> 只对新订单生效，已支付的老订单账单不变。改这一个常量即可全局生效。
PAY_GOODS_NAME = '寄存服务预付款，使用完剩余款项在小程序钱包进行余额提现'


def get_payment_params(order_id, order_no, deposit_amount, user_phone=None, openid=None,
                       payment_channel=None, payment_channel_id=None, _retry_count=0):
    """获取微信支付参数"""
    from wxpay import WxPay
    mock_mode = is_mock_mode()

    if mock_mode:
        return {'mode': 'mock', 'order_id': order_id, 'order_no': order_no, 'total_fee': int(round(deposit_amount * 100))}

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
            result = wxpay.unifiedorder(trade_type=third_party_type, body=PAY_GOODS_NAME,
                                         total_fee=int(round(deposit_amount * 100)), out_trade_no=order_no)
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

    total_fee = int(round(deposit_amount * 100))
    time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')

    result = wxpay.unifiedorder(trade_type=trade_type, body=PAY_GOODS_NAME,
                                 total_fee=total_fee, out_trade_no=order_no,
                                 notify_url=_wx_payurl(), openid=openid,
                                 scene_info=scene_info, time_expire=time_expire)

    # [S657-20260924] 被动记录本次【真实下单】结果(成功也记一笔 -> 可看"最后成功时间")。
    #   账号级受限会立即告警; 产品级只落库不告警。异常在函数内吞掉, 绝不影响支付链路。
    _record_mch_last_error(current_channel, result, trade_type, 'h5-unifiedorder', order_no=order_no)

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
                                  result.get('err_code_des') or result.get('return_msg', ''),
                                  trade_type)
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

    ⚠️ 连接归还规则（[S659-20260924] 修连接池泄漏，只动"归不归还"，别的都不动）：
       · 请求上下文：get_db() 返回的是 flask.g 复用的那条连接，调用链上别处可能还持有
         同一个 conn 对象在用 -> 本函数**绝不 close()**（close() 会 putconn 归还池子，
         归还后另一线程可能同时拿到同一条连接 = 串号/竞态），请求结束由 teardown 统一回收。
       · 非请求上下文（脚本/巡检）：get_db() 每次新建一条只属于本函数的连接 ->
         用完**必须** close() 归还池子。
       老注释曾说"非请求上下文最多每 60 秒漏 1 条，可忽略"——这条假设是错的：
       商户健康巡检线程每 60 秒跑一轮并走到这里（check_merchant_health ->
       get_channel_wxpay -> appid_by_openid -> 本函数），于是每 60 秒漏 1 条，
       约 50 分钟把 ThreadedConnectionPool(10,50) 漏干；池干后该 worker 的 get_db()
       全部失败 -> 3 次触发 SIGTERM 自愈重启。2026-09-24 生产实测：
       `connection pool exhausted` 333 条 / 自愈重启 21 次。
    """
    now = time.time()
    cached = _APPID_BY_PREFIX_CACHE.get('rows')
    if cached is not None and (now - _APPID_BY_PREFIX_CACHE.get('ts', 0.0)) < _APPID_BY_PREFIX_TTL:
        return cached
    rows = []
    conn = None
    # [S659-20260924] _own_conn = "这条连接归本函数归还"。判不出来时按"不归我"处理
    #   （保守：行为与改动前完全一致，绝不因为判不出上下文而误归还请求连接）。
    _own_conn = False
    try:
        from flask import has_request_context as _has_req_ctx
        _own_conn = not bool(_has_req_ctx())
    except Exception:
        _own_conn = False
    try:
        conn = get_db()
        if _own_conn:
            # 双保险：若存在"有 app 上下文但没有请求上下文"的调用点，get_db() 仍会复用
            #   flask.g 上的连接；只要这条连接就是 g 上那条，就一律不归还。
            try:
                from flask import g as _g
                if getattr(_g, '_db_conn', None) is conn:
                    _own_conn = False
            except Exception:
                pass
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
    finally:
        # [S659-20260924] 只归还【非请求上下文】下【本次新建】的连接；请求上下文与
        #   "有 app 上下文无请求上下文"两种情形一律不动（_own_conn=False）。
        #   归还失败只记日志，绝不吞掉主流程的异常，也绝不影响本次 return 的 rows。
        if _own_conn and conn is not None:
            try:
                conn.close()
            except Exception as _ce:
                logger.error('[S659] 归还 _appid_rows 的连接失败(不影响本次返回值): %s', _ce)
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
                        payment_channel_id=None, body=PAY_GOODS_NAME):
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
        # [S657-20260924] 同上: 被动记录本次真实下单结果(小程序支付 JSAPI)
        _record_mch_last_error(channel, result, 'JSAPI', 'mp-jsapi', order_no=order_no)
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
                # [S556-20260922] bug① 开回写：小程序微信【统一下单已经成功】-> channel 就是这笔
                #   真实的收款渠道。只在"订单当前渠道 != 下单渠道"时才 UPDATE（一致时什么都不写），
                #   并打 WARNING + 写 alarms，便于统计历史脏单。
                #   实证：订单 137663 挂支付宝 113、微信侧 mch 1750896171 实收 ¥21.70，
                #   本条日志当时只提示"当前未回写"，于是退款按支付宝发起必然 ACQ.TRADE_NOT_EXIST。
                if _rowc:
                    _s556_correct_order_channel(order_id, channel['id'], source='mp-jsapi-prepay',
                                                order_no=order_no, ch_type='wechat',
                                                mode='jsapi')
            except Exception as _e:
                logger.error('[mp-jsapi] 渠道一致性检查失败: %s', _e)
            # [S569-20260922] 统一下单已成功 = 微信已确认这个 mp_openid 就是付款人。
            #   若订单身份缺失（下单时客户端没带 openid -> user_id=0），用这个强键补回来，
            #   否则钱包页 calc_balance 按 user_id 现算会显示 0、客服自助也查不到单。
            #   正常单（user_id>0 且 mp_openid 非空）在函数内直接返回，一个字都不写。
            try:
                _backfill_order_identity(order_id, mp_openid=mp_openid, source='mp-jsapi-prepay')
            except Exception as _e569:
                logger.error('[S569] 统一下单后身份回填失败(不影响支付): %s', _e569)

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
# [S618] 支付宝「小程序」通道判据：小程序支付的 op_app_id 必须是小程序 appid，
#   而 payment_channels 里 113=支付宝小程序(2021006199688688) 与
#   120=支付宝-H5(2021006197675152) 两条 channel_type 都是 'alipay' 且都 is_active=1，
#   所以选通道时必须显式优先小程序通道，否则会把 H5 的 appid 当 op_app_id 发出去。
_ALIPAY_MP_APP_ID = '2021006199688688'


def _is_alipay_mp_channel(channel):
    """[S618] 该支付宝通道是否为【小程序】通道（小程序支付必须用它）。任一命中即为真：

      1) app_id == 小程序 appid 2021006199688688；
      2) cert_name == 'alipay_mp'（密钥文件名口径）；
      3) name 含「小程序」。

    H5 通道 120（app_id=2021006197675152 / cert_name=alipay_prod_user / 名称「支付宝-H5」）
    三条都不命中 → 不会被误判成小程序通道。
    """
    if not channel:
        return False
    if str(channel.get('app_id') or '').strip() == _ALIPAY_MP_APP_ID:
        return True
    if str(channel.get('cert_name') or '').strip().lower() == 'alipay_mp':
        return True
    return '小程序' in str(channel.get('name') or '')


def _pick_alipay_mp_channel_from_db():
    """[S618] 直接查库取【小程序】通道（含 is_active=0 的兜底）；取不到返回 None。

    只读 SELECT，不写库；异常只记日志并返回 None（绝不抛出）。
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payment_channels WHERE channel_type='alipay' "
                       "ORDER BY is_active DESC, auto_disabled ASC, rotation_index ASC, id ASC")
        rows = cursor.fetchall()
        conn.close()
        for _r in rows:
            if (_r.get('channel_type') or '') == 'alipay' and _is_alipay_mp_channel(_r):
                return dict(_r)
    except Exception as _e:
        logger.error('[alipay-mp] 查库挑支付宝小程序通道失败: %s', _e)
    return None


def _mp_pick_alipay_channel(channel_id=None):
    """只挑 channel_type='alipay' 的通道；挑不到返回 None。

    为什么单独一条：微信/支付宝通道【绝不能相互轮询】（选错类型直接付款失败），
    而 select_payment_channel() 会连微信通道一起算。

    [S618] 两个现状更正 + 小程序优先：
      · payment_channels 里 113（支付宝小程序 2021006199688688 / cert_name=alipay_mp）
        与 120（支付宝-H5 2021006197675152 / cert_name=alipay_prod_user）
        【都】是 channel_type='alipay' 且【都】is_active=1；
      · 所以选通道时必须优先返回【小程序】通道（见 _is_alipay_mp_channel）：
        小程序支付的 op_app_id 必须是小程序 appid，选到 120 就会传错 appid 被支付宝拒；
      · 一个活跃的都没有时，仍退回查库（含 is_active=0，仅告警不拦），同样优先小程序通道。
    """
    if channel_id:
        _ch = _get_payment_channel(channel_id)
        if _ch and (_ch.get('channel_type') or '') == 'alipay':
            # [S618] 显式指定的渠道若不是【小程序】通道（例如订单挂的是 120=H5），
            #   仍要改选小程序通道：小程序支付的 op_app_id 必须是小程序 appid。
            if _is_alipay_mp_channel(_ch):
                return _ch
            _mp = _pick_alipay_mp_channel_from_db()
            if _mp:
                logger.warning('[alipay-mp] 指定通道 id=%s 不是支付宝小程序通道，'
                               '改选小程序通道 id=%s', channel_id, _mp.get('id'))
                return _mp
            logger.warning('[alipay-mp] 库里没有支付宝小程序通道，沿用指定通道 id=%s', channel_id)
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
        if _is_alipay_mp_channel(_ch):
            return _ch
        # [S618] 选到的是 H5 通道 → 改选小程序通道
        #   （轮转/sequential 两种模式都可能选到 120）
        _mp = _pick_alipay_mp_channel_from_db()
        if _mp:
            logger.warning('[alipay-mp] 选到非小程序通道 id=%s，改选小程序通道 id=%s',
                           _ch.get('id'), _mp.get('id'))
            return _mp
        return _ch
    # 兜底：查库直接找 alipay 通道（包含 is_active=0 的 113，仅告警不拦）
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payment_channels WHERE channel_type='alipay' "
                       "ORDER BY is_active DESC, auto_disabled ASC, rotation_index ASC, id ASC")
        rows = cursor.fetchall()
        conn.close()
        for _r in rows:                     # [S618] 先找小程序通道
            if (_r.get('channel_type') or '') == 'alipay' and _is_alipay_mp_channel(_r):
                logger.warning('[alipay-mp] 没有 is_active=1 的支付宝通道，'
                               '退回使用未启用小程序通道 id=%s', _r.get('id'))
                return dict(_r)
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

        # [S618] 小程序支付官方必传：op_app_id = 唤起收银台支付所在的小程序 appid。
        #   取不到就明确报错并返回友好提示，绝不静默发一个缺必选参数的请求。
        _op_app_id = str(channel.get('app_id') or '').strip()
        if not _op_app_id:
            logger.error('[alipay-mp] 通道 id=%s 未配置 app_id，无法传 op_app_id（小程序支付必传）order=%s',
                         channel.get('id'), order_no)
            return {'ok': False, 'mode': 'error',
                    'error_msg': '支付宝商户配置不完整，请联系管理员'}

        client, ch_type = get_channel_wxpay(channel)
        if client is None or ch_type != 'alipay':
            logger.error('[alipay-mp] 支付宝通道实例化失败 channel=%s type=%s', channel.get('id'), ch_type)
            return {'ok': False, 'mode': 'error', 'error_msg': '支付宝渠道配置异常'}

        resp = client.trade_create(out_trade_no=order_no, total_amount=amount,
                                   subject=subject, buyer_open_id=alipay_uid,
                                   product_code='JSAPI_PAY', op_app_id=_op_app_id,
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


# ============================================
# [S673-20260927] 余额/身份改造 第一步止血 —— 开关（两个都默认 OFF = 现状）
# ============================================
S673_BALANCE_ID_AUDIT_KEY = 'balance_id_audit_guard'          # B2：默认 0=OFF
S673_BALANCE_STRICT_IDENTITY_KEY = 'balance_strict_identity'  # C 组：默认 0=OFF


def balance_id_audit_guard_enabled():
    """[S673-B2] 纯 ID 安全网：入账时【只留痕】（靠哪把钥匙命中 / user_id 是否符合 / 新建行）。

    老板口径（2026-09-27 定稿）：手机号不做识别、不判断"一样不一样"，只看 ID 是否符合；
    本安全网【只记录，不拦截、不改变入账成败】。
    默认 0=OFF：返回 False -> 不传 trace、不写审计，行为与改动前逐字节一致。
    """
    try:
        return str(get_setting(S673_BALANCE_ID_AUDIT_KEY, '0') or '0').strip().lower() \
            in ('1', 'true', 'yes', 'on')
    except Exception:
        return False


def balance_strict_identity_enabled():
    """[S673-C] 收窄后的 C 范围（老板 2026-09-27 定稿）：**只去掉"手机号兜底"这一档**。

    · 认人：resolve_user_identity 里三处 `not strong_keys and phone` 的 phone 兜底；
    · 钱  ：find_user_balance_row 末尾的 phone 兜底段 + unionid 段里"用 phone 辅助选行"；
    · **保留** unionid / mp_openid / openid 三段（预演显示：把 unionid 也删掉会一次废掉
      ¥702,068.90 的余额行可达性）；
    · unionid 段带"唯一才采纳"：指向多个 users / 多行 -> ambiguous 或落空，**绝不猜**。

    默认 0=OFF：返回 False，各处条件与改动前逐字节等价。本次【不打开】。
    """
    try:
        return str(get_setting(S673_BALANCE_STRICT_IDENTITY_KEY, '0') or '0').strip().lower() \
            in ('1', 'true', 'yes', 'on')
    except Exception:
        return False


def _s673_audit_id_hit(kind, row_id, key, in_user_id, row_user_id, signal, amount):
    """[S673-B2] 纯 ID 安全网留痕（只在开关 ON 时被调用）。

    · **只记录**：不判断手机号、不拦截、不改变入账成败（老板定稿口径）；
    · 落 system_settings 计数键 balance_id_audit（不新建表、不做 DDL）；
    · 独立连接 + 独立提交 + 全程 try/except —— 绝不可能污染入账事务；
    · 载荷只含 ID 类字段，**不含手机号**（手机号不做识别、也不在此留痕）。
    """
    import json as _j
    import time as _t
    _k = 'balance_id_audit'
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT setting_value FROM system_settings WHERE setting_key=%s", (_k,))
        r = cur.fetchone()
        if r is None:
            raw = ''
        elif isinstance(r, dict):
            raw = r.get('setting_value') or ''
        else:
            raw = r[0] or ''
        try:
            st = _j.loads(raw or '{}') or {}
        except Exception:
            st = {}
        if not isinstance(st, dict):
            st = {}
        st['count'] = int(st.get('count') or 0) + 1
        if kind == 'new_row':
            st['new_row'] = int(st.get('new_row') or 0) + 1
        else:
            st['hit'] = int(st.get('hit') or 0) + 1
        _by = st.get('by_key')
        if not isinstance(_by, dict):
            _by = {}
        _by[key] = int(_by.get(key) or 0) + 1
        st['by_key'] = _by
        if signal:
            _sg = st.get('signals')
            if not isinstance(_sg, dict):
                _sg = {}
            _sg[signal] = int(_sg.get(signal) or 0) + 1
            st['signals'] = _sg
        st['last_ts'] = _t.strftime('%Y-%m-%d %H:%M:%S')
        st['last'] = {'kind': kind, 'row_id': row_id, 'key': key, 'in_user_id': in_user_id,
                      'row_user_id': row_user_id, 'signal': signal,
                      'amount': round(float(amount or 0), 2)}
        cur.execute("INSERT INTO system_settings (setting_key, setting_value) VALUES (%s,%s) "
                    "ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                    (_k, _j.dumps(st, ensure_ascii=False)))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    # [S673-C 骨架] 开关 balance_strict_identity（默认 0=OFF）。
    #   OFF 时 _s673_nophone=False，下面三处条件与改动前逐字节等价。
    #   ON  时认人不再按手机号兜底（"认人只看卡"）——本次【不打开】。
    try:
        _s673_nophone = balance_strict_identity_enabled()
    except Exception:
        _s673_nophone = False
    out = {
        'user_id': 0,
        'unionid': unionid or '',
        'mp_openid': mp_openid or '',
        'phone': phone or '',
        'ambiguous': False,
        'reason': '',
    }
    # [S673-C] ON 时 unionid 必须"唯一才采纳"：指向多个 users -> 身份待确认，绝不猜。
    #   OFF 时这一段一条 SQL 都不执行，行为与改动前逐字节等价。本次【不打开】。
    if _s673_nophone and _clean(unionid):
        try:
            cursor.execute("SELECT count(*) FROM users WHERE unionid = %s AND id > 0", (unionid,))
            _s673_cu = cursor.fetchone()
            _s673_cu = (_s673_cu[0] if _s673_cu else 0)
            if _s673_cu and int(_s673_cu) > 1:
                out['ambiguous'] = True
                out['reason'] = 'multiple_users_by_unionid'
                return out
        except Exception:
            pass
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

    # [S680-20260927] 第2批：这里原本是「一个强键都没有时，按【手机号】查名册认人」，
    #   连带两段"手机号底下有多个身份/多个编号就报 ambiguous"的检查，整段删除。
    #   理由（老板 09-27 定稿）：手机号不做识别 —— 一个手机号最多挂过 3 张小程序的卡 /
    #   10 张公众号卡，且换号会被运营商回收给新人 -> 按手机号认人会认错人、甚至继承余额。
    #   删掉后：认人只用 UN / 小程序卡 / 公众号卡（+ 支付宝路径的 users.id）。

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
    # [S680-20260927] 第2批：原来这里还有一段"按手机号取 po_candidates"，一并删除（理由同 R1）。

    if po_candidates:
        # [S680-20260927] 第2批：删掉原来的「按手机号过滤候选行」（`if phone: po_candidates = [...]`）。
        #   手机号不做识别。下面的去重逻辑保留：同一 UN 多行且 unionid/openid 都一致 -> 取第一行；
        #   否则报 ambiguous（绝不猜）。实测同一个 UN 占多行的只有 66 个，其中仅 5 个带不同手机号。
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
            # [S680-20260927] 第2批：这里原本还有一支「按【手机号】去 user_balances 捞 unionid」
            #   （原注释：这一步正是"新 openid 查不到 -> 用手机号捞出老 unionid -> 认成老账号"的桥）。
            #   按老板"手机号不做识别"的红线，删除。保留 mp_openid / openid 两支（都是 ID）。
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
                          strict_identity=False, _s673_trace=None):
    """Find the balance row belonging to one identity. Returns dict or None.

    strict_identity=True（[S320] 新小程序专用）：跳过"最后按 phone 兜底认行"那一段，
      否则新小程序用户会按手机号命中老账号的 user_balances 行，余额/身份被串。
      默认 False -> 现有调用（含 H5、老小程序）行为一字不变。

    [S673-B2] _s673_trace：可选 dict。传入时把"本次靠哪把钥匙命中"写进
      _s673_trace['step']（user_id / unionid / unionid+phone / mp_openid / mp_openid+phone /
      openid / openid+phone / phone / phone(legacy)）。**默认 None 时行为与返回值都不变**
      （返回的行不带任何额外键）——只有 upsert 的留痕需要它。
    """
    def _m(_row, _step):
        # [S673-B2] 纯留痕：只写调用方给的 trace 容器，绝不改动返回的行本身
        if _s673_trace is not None:
            try:
                _s673_trace['step'] = _step
            except Exception:
                pass
        return _row

    # [S673-C] 开关 balance_strict_identity（默认 0=OFF）。
    #   OFF 时 _s673_nomoney=False，下面每一处条件与改动前逐字节等价。
    #   ON  时【只去掉"按手机号认行"这一档】：末尾 phone 段 + unionid 段里"用 phone 辅助选行"；
    #   保留 unionid / mp_openid / openid 三段；且 unionid 段要求"唯一才采纳"，多行绝不猜。
    #   本次【不打开】。
    try:
        _s673_nomoney = balance_strict_identity_enabled()
    except Exception:
        _s673_nomoney = False
    # [S680-20260927] 第2批：删掉原来的第 1 档「按 user_id 取第一行」。
    #   这是【盲命中】—— 不校验任何东西。而入账时传进来的正是【订单上的那个编号】，
    #   实测：orders.user_id 有 44,687 行(31.89%)指向 users 里不存在的编号；
    #   7,446 个编号名下跨多个 openid，最极端 user_id=36823 名下 35 张卡 / 470 单。
    #   靠它认行会把别人的记录认成你的 -> 串号。整档删除。
    if unionid:
        # [S680-20260927] 第2批：删掉「先用 (手机号, unionid) 试一行」那一档 —— 手机号不做识别。
        #   多行时也【不再用手机号挑】，改成取【余额最大】那行 + 告警留痕。
        #   为什么可以取余额最大：user_balances 的唯一约束是 (phone, unionid)，
        #   所以同一个 UN 的多行必然是【同一个人的不同手机号】（历史换号遗留），
        #   不是两个人。实测只有 3 个 UN 会走到多行分支。
        try:
            cursor.execute("SELECT * FROM user_balances WHERE unionid = %s ORDER BY id", (unionid,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return _m(rows[0], 'unionid')
            if len(rows) > 1:
                _best = max(rows, key=lambda r: float(r.get('balance') or 0))
                logger.warning('[S680] unionid=%s... 指向 %s 行，取余额最大行 id=%s 余额=%s（留痕）',
                               str(unionid)[:10], len(rows), _best.get('id'), _best.get('balance'))
                return _m(_best, 'unionid(multi)')
        except Exception:
            pass
    if mp_openid:
        # [S680-20260927] 第2批：多行时不再用手机号挑，改取余额最大 + 告警（理由同 R5）。
        #   实测同一 mp_openid 占 2 行的只有 8 张卡。
        try:
            cursor.execute("SELECT * FROM user_balances WHERE mp_openid = %s ORDER BY id", (mp_openid,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return _m(rows[0], 'mp_openid')
            if len(rows) > 1:
                _best = max(rows, key=lambda r: float(r.get('balance') or 0))
                logger.warning('[S680] mp_openid=%s... 指向 %s 行，取余额最大行 id=%s 余额=%s（留痕）',
                               str(mp_openid)[:10], len(rows), _best.get('id'), _best.get('balance'))
                return _m(_best, 'mp_openid(multi)')
        except Exception:
            pass
    if openid:
        # [S680-20260927] 第2批：多行时不再用手机号挑，改取余额最大 + 告警（理由同 R5）。
        #   注意 openid 列有唯一约束（user_balances_openid_uk），实际恒为 1 行。
        try:
            cursor.execute("SELECT * FROM user_balances WHERE openid = %s ORDER BY id", (openid,))
            rows = [dict(r) for r in cursor.fetchall()]
            if len(rows) == 1:
                return _m(rows[0], 'openid')
            if len(rows) > 1:
                _best = max(rows, key=lambda r: float(r.get('balance') or 0))
                logger.warning('[S680] openid=%s... 指向 %s 行，取余额最大行 id=%s 余额=%s（留痕）',
                               str(openid)[:10], len(rows), _best.get('id'), _best.get('balance'))
                return _m(_best, 'openid(multi)')
        except Exception:
            pass
    # [S680-20260927] 第2批：删掉原来的最后一档「按【手机号】兜底认行」
    #   （会把新小程序用户按手机号认到老账号的行上 -> 余额/身份被串）。
    #   到这里还找不到，就返回 None；上层用 INSERT ... ON CONFLICT 仍能把钱加到
    #   该手机号那一行（不会丢钱），只是"读"的时候认不出来 —— 这正是老板接受的取舍。
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
    # [S673-B2-20260927] 纯 ID 安全网 —— 【只留痕，不改变任何行为】。
    #   老板定稿口径：手机号不做识别、不判断"一样不一样"，只看 ID 是否符合；
    #   绝不拦截、绝不改成败。记录四件事：
    #     ① 本次靠哪把钥匙命中（user_id / unionid / mp_openid / openid / phone 兜底 / 新建行）
    #     ② 入参 user_id>0 且命中行 user_id>0 且两者不等  -> "弱键把人带到另一行"的信号
    #     ③ 入参 user_id=0 却命中了一个有主的行
    #     ④ 新建行（便于统计"新建了多少账户"）
    #   开关 balance_id_audit_guard，默认 0=OFF：OFF 时不传 trace、不写审计，
    #   入参与行为与改动前逐字节一致。
    # [S679-20260927] 第1批：trace 现在【总是】传下去 ——
    #   补位护栏必须知道"这条记录是靠哪把钥匙认到的"（step）。
    #   注意：审计留痕仍然只受 balance_id_audit_guard 开关控制（_s673_trace 仍为 None 时不写审计），
    #         所以这一处【不改变任何留痕行为】。
    _s679_probe = {}
    _s673_trace = None
    try:
        if balance_id_audit_guard_enabled():
            _s673_trace = _s679_probe
    except Exception:
        _s673_trace = None
    existing = find_user_balance_row(cursor, phone=phone, openid=openid, mp_openid=mp_openid,
                                    unionid=unionid, user_id=user_id, strict_identity=strict_identity,
                                    _s673_trace=_s679_probe)
    if existing:
        row_id = existing['id']
        if _s673_trace is not None:
            try:
                _s673_row_uid = int(existing.get('user_id') or 0)
                _s673_key = str(_s673_trace.get('step') or 'unknown')
                _s673_signal = ''
                if user_id > 0 and _s673_row_uid > 0 and _s673_row_uid != user_id:
                    _s673_signal = 'user_id_mismatch'
                    logger.warning(
                        '[S673][B2] 入参 user_id=%s 但命中行 user_id=%s 且不等 -> 弱键带到另一行(只留痕): '
                        'row_id=%s key=%s amount=%s',
                        user_id, _s673_row_uid, row_id, _s673_key, balance)
                elif user_id <= 0 and _s673_row_uid > 0:
                    _s673_signal = 'input_uid_zero_row_owned'
                    logger.warning(
                        '[S673][B2] 入参 user_id=0 却命中已属 user_id=%s 的行(只留痕): '
                        'row_id=%s key=%s amount=%s',
                        _s673_row_uid, row_id, _s673_key, balance)
                try:
                    _s673_audit_id_hit('hit', row_id, _s673_key, user_id, _s673_row_uid,
                                       _s673_signal, balance)
                except Exception as _s673_ae:
                    logger.warning('[S673][B2] 审计写入失败(不影响入账): %s', _s673_ae)
            except Exception as _s673_te:
                logger.warning('[S673][B2] 留痕失败(不影响入账): %s', _s673_te)
        # ============================================================
        # [S679-20260927] 第1批 v2：入账"补位" + 护栏（老板 09-27 拍定）
        # ============================================================
        # 改前：命中已有余额记录时【只加钱】，不补 openid/mp_openid/unionid
        #   -> 老记录（只有 UN + 公众号卡）的小程序卡格子永远是空的，
        #      用户从小程序口就永远查不到这条记录（实测 53,462 行 / ¥647,856.25）。
        # 改后：加钱的同时，把这次带来的钥匙【插到空着的格子里】；已有值一个字不动。
        #
        # 护栏①（哪些档位允许补）：主键是"通行证/卡"的档才补。
        #   ！！v1 的教训：v1 只放行 unionid / mp_openid / openid 三个【不带 phone】的档，
        #   但真实入账走的几乎都是 `unionid+phone`（find_user_balance_row 优先试
        #   (phone,unionid)）-> 补位实际一次都没发生。端到端实测才抓到，单元测试抓不到。
        #   现在的口径：+phone 只是"多行时消歧"，主键仍是 ID，所以放行；
        #   只挡两类：phone / phone(legacy)（纯手机号认行，换号会认错人）、
        #             user_id（订单上的脏编号）。
        # 护栏②（ID 唯一性）：命中用的那把 ID 若在余额表里指向【多行】，
        #   说明靠它认行本身有歧义 -> 保守不补 + 告警。（实测多行极少：UN 3 个、卡 8 张。）
        # 护栏③（目标值占用）：目标列有唯一约束（user_balances_openid_uk /
        #   idx_user_balances_phone_mp_openid / idx_user_balances_phone_unionid），
        #   撞了会让 UPDATE 抛异常 = 入账失败。所以先查，被占用就不补。
        #   用【命中行自己的手机号】去比，不用入参手机号。
        # 另外：本批【不删】原来那句 user_id 覆盖 —— 见下面说明。
        # 兜底：补位这段任何异常都只打日志，金额三栏照加，绝不影响入账。
        #
        # ！！为什么本批不动 user_id 覆盖（2026-09-27 部署前核实发现）：
        #   `calc_balance()` 里有一道闸 —— `if ident['user_id'] == 0: return 0.0`
        #   （余额是按"订单的 user_id"算的）；而 `/user/balance` 里会
        #   `ident['user_id'] = row.get('user_id')` 从余额行回捞编号补上。
        #   一旦入账不再写编号，"认不到人、只能靠手机号认行"的那部分用户
        #   ident['user_id'] 就会是 0 -> 余额显示 0。
        #   所以"编号停用"必须和"认人收窄（第 2 批）"一起做。
        _s679_step = str((_s679_probe or {}).get('step') or '')
        _s679_ok_steps = ('unionid', 'unionid+phone', 'mp_openid', 'mp_openid+phone',
                          'openid', 'openid+phone')
        _s679_backfill = _s679_step in _s679_ok_steps
        _s679_oa = _s679_mp = _s679_un = ''
        _s679_row_phone = str(existing.get('phone') or '')
        if _s679_backfill:
            # 护栏②：命中用的 ID 是否只对应这一行
            try:
                if _s679_step.startswith('unionid'):
                    _s679_id_col, _s679_id_val = 'unionid', unionid
                elif _s679_step.startswith('mp_openid'):
                    _s679_id_col, _s679_id_val = 'mp_openid', mp_openid
                else:
                    _s679_id_col, _s679_id_val = 'openid', openid
                if _s679_id_val:
                    cursor.execute(
                        "SELECT COUNT(*) AS c FROM user_balances WHERE " + _s679_id_col + " = %s",
                        (_s679_id_val,))
                    _s679_c = cursor.fetchone()
                    if isinstance(_s679_c, dict):
                        _s679_c = _s679_c.get('c') or 0
                    elif _s679_c:
                        _s679_c = _s679_c[0] or 0
                    else:
                        _s679_c = 0
                    if int(_s679_c) > 1:
                        logger.warning('[S679] %s=%s... 指向多行(%s)，保守不补位 row_id=%s',
                                       _s679_id_col, str(_s679_id_val)[:10], _s679_c, row_id)
                        _s679_backfill = False
            except Exception as _s679_ue:
                logger.warning('[S679] ID 唯一性校验失败，本次不补位(不影响入账): %s', _s679_ue)
                _s679_backfill = False
        if _s679_backfill:
            # 护栏③：目标值是否已被别的行占用
            try:
                _s679_cur_oa = str(existing.get('openid') or '').strip()
                _s679_cur_mp = str(existing.get('mp_openid') or '').strip()
                _s679_cur_un = str(existing.get('unionid') or '').strip()
                if openid and not _s679_cur_oa:
                    cursor.execute(
                        "SELECT 1 FROM user_balances WHERE openid = %s AND id <> %s LIMIT 1",
                        (openid, row_id))
                    if not cursor.fetchone():
                        _s679_oa = openid
                if mp_openid and not _s679_cur_mp:
                    cursor.execute(
                        "SELECT 1 FROM user_balances WHERE mp_openid = %s AND phone = %s AND id <> %s LIMIT 1",
                        (mp_openid, _s679_row_phone, row_id))
                    if not cursor.fetchone():
                        _s679_mp = mp_openid
                if unionid and not _s679_cur_un:
                    cursor.execute(
                        "SELECT 1 FROM user_balances WHERE unionid = %s AND phone = %s AND id <> %s LIMIT 1",
                        (unionid, _s679_row_phone, row_id))
                    if not cursor.fetchone():
                        _s679_un = unionid
            except Exception as _s679_ce:
                logger.warning('[S679] 补位前占用检查失败，本次不补位(不影响入账): %s', _s679_ce)
                _s679_oa = _s679_mp = _s679_un = ''
            if _s679_oa or _s679_mp or _s679_un:
                logger.info('[S679] 入账补位 row_id=%s step=%s 补公众号卡=%s 补小程序卡=%s 补通行证=%s',
                            row_id, _s679_step, bool(_s679_oa), bool(_s679_mp), bool(_s679_un))
        cursor.execute(
            """UPDATE user_balances SET
                balance = COALESCE(balance,0) + %s,
                total_deposited = COALESCE(total_deposited,0) + %s,
                total_withdrawn = COALESCE(total_withdrawn,0) + %s,
                wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                openid    = COALESCE(NULLIF(openid,''),    NULLIF(%s,'')),
                mp_openid = COALESCE(NULLIF(mp_openid,''), NULLIF(%s,'')),
                unionid   = COALESCE(NULLIF(unionid,''),   NULLIF(%s,''))
              WHERE id = %s""",
            (balance, total_deposited, total_withdrawn, wechat_name,
             _s679_oa, _s679_mp, _s679_un, row_id),
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
    if _s673_trace is not None:
        try:
            _s673_audit_id_hit('new_row', (row['id'] if row else None), 'new_row', user_id, 0,
                               '', balance)
        except Exception as _s673_ae2:
            logger.warning('[S673][B2] 新建行留痕失败(不影响入账): %s', _s673_ae2)
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


def deposit_already_refunded(order):
    """[S628-20260923] "结束订单->预付款计入余额" 前的一致性闸门。

    背景（2026-09-13 生产 4 笔微信单，合计 ¥81.21）：订单先被原路退款
    （投诉自动退款 / 提现退款 / 后台退款都会把 orders.refund_status 置成 'refunded'，
     helpers.do_real_refund 同时把已存在的 user_balance_details 置为 'withdrawn'），
    但退款发生时订单还在使用中(status=2)、余额明细行还没生成，那条 UPDATE 命中 0 行；
    之后用户取件、订单结束时又走一次"预付款进余额"——同一笔预付款既退回支付账户、
    又变成可提现余额。

    口径：
      refund_status == 'refunded' 且 refund_amount >= deposit_amount
        -> 预付款已全额原路退回，不再计入余额（本轮任务口径）。
      部分退款 0 < refund_amount < deposit_amount
        -> 这些调用点入账金额用的是整笔 deposit_amount，入账会多给，
           故一律保守跳过并打 WARNING（生产库当前 0 笔，见 S628 报告）。
      refund_status == 'refunded' 但 refund_amount <= 0
        -> 不拦（保持原行为；生产库 14 笔老单，明细均非 available，无重复入账风险）。

    返回 True = 跳过入账。任何取值异常一律返回 False（保持原行为，绝不影响正常结束订单）。
    """
    try:
        _o = order or {}
        _oid = _o.get('id')
        _rs = str(_o.get('refund_status') or '').strip().lower()
        _ra = float(_o.get('refund_amount') or 0)
        _da = float(_o.get('deposit_amount') or 0)
    except Exception:
        return False
    if _rs != 'refunded' or _da <= 0 or _ra <= 0:
        return False
    if _ra >= _da:
        logger.warning('[S628] order_id=%s 预付款已全额原路退款(refund_amount=%s >= deposit_amount=%s)，跳过计入余额', _oid, _ra, _da)
    else:
        logger.warning('[S628] order_id=%s 预付款已部分原路退款(0 < refund_amount=%s < deposit_amount=%s)，保守跳过计入余额，需人工核对', _oid, _ra, _da)
    return True


def refund_deposit_to_balance(cursor, order):
    """清柜/定时清柜统一退预付款到余额，返回 (是否退款, mp_openid)"""
    deposit = float(order.get('deposit_amount') or 0)
    phone = str(order.get('user_phone') or '')
    if deposit <= 0 or not phone:
        return False, '', False
    # [S628-20260923] 已原路退款的订单不再计入余额（否则同一笔预付款既退回支付账户又变成可提现余额）
    if deposit_already_refunded(order):
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
# [S541-20260922] 提现"预付款原路退回"开关（默认值全部 = 现状行为，上线后行为不变）
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


def _s556_write_alarm(alarm_type, content, device_id=None, level=1):
    """[S556-20260922] 写一条后台告警（alarms 表）。任何异常都吃掉，绝不影响主流程。"""
    try:
        from database import get_db as _gdb556
        _a = _gdb556()
        try:
            _c = _a.cursor()
            _c.execute("INSERT INTO alarms (type, device_id, content, status, created_at) "
                       "VALUES (%s, %s, %s, '0', NOW())",
                       (str(alarm_type)[:64], device_id, str(content)[:500]))
        finally:
            try:
                _a.close()
            except Exception:
                pass
        logger.warning('[S556] 告警已写: type=%s content=%s', alarm_type, str(content)[:200])
        return True
    except Exception as _e:
        logger.error('[S556] 写 alarms 失败(不影响主流程): %s', _e)
        return False


def _s556_correct_order_channel(order_id, new_channel_id, source='', order_no='',
                                transaction_id=None, ch_type='', mode='') -> bool:
    """[S556-20260922] bug① —— 用【真实收款渠道】纠正订单渠道。

    只在 "new_channel_id 非空 且 != 订单当前 payment_channel_id" 时才 UPDATE；
    相等时一个字都不写（正常同渠道支付的既有行为零变化）。
    纠正时同时打 WARNING + 写 alarms(type='payment_channel_mismatch')，便于统计历史脏单。
    """
    try:
        _nid = int(new_channel_id or 0)
    except Exception:
        return False
    if not order_id or _nid <= 0:
        return False
    conn = None
    try:
        from database import get_db as _gdb556
        conn = _gdb556()
        cur = conn.cursor()
        cur.execute('SELECT payment_channel_id FROM orders WHERE id=%s', (order_id,))
        _r = cur.fetchone()
        _old = None
        if _r:
            _old = _r.get('payment_channel_id') if hasattr(_r, 'get') else _r[0]
        try:
            _oldi = int(_old) if _old is not None else None
        except Exception:
            _oldi = None
        if _oldi == _nid:
            return False                      # 一致 -> 不写，零变化
        _oldname = str(_oldi) if _oldi is not None else 'NULL'
        if _oldi:
            try:
                cur.execute('SELECT name FROM payment_channels WHERE id=%s', (_oldi,))
                _r2 = cur.fetchone()
                if _r2:
                    _oldname = '%s(%s)' % (_oldi, (_r2.get('name') if hasattr(_r2, 'get') else _r2[0]))
            except Exception:
                pass
        cur.execute('UPDATE orders SET payment_channel_id=%s WHERE id=%s', (_nid, order_id))
        logger.warning('[S556] 订单渠道纠正: order=%s 订单记账=%s -> 实收=%s 来源=%s 渠道类型=%s',
                       ('%s(%s)' % (order_id, order_no)) if order_no else order_id,
                       _oldname, _nid, source or '?', ch_type or '?')
        conn.close()
        conn = None
        _s556_write_alarm(
            'payment_channel_mismatch',
            '订单渠道纠正 [%s] order_id=%s order_no=%s 订单记账渠道=%s -> 真实收款渠道=%s '
            '渠道类型=%s 交易号=%s（历史脏单，退款按真实渠道走）'
            % (source or '?', order_id, order_no or '', _oldname, _nid, ch_type or '?',
               transaction_id or ''))
        return True
    except Exception as _e:
        logger.error('[S556] 纠正订单渠道失败 order=%s new=%s err=%s', order_id, _nid, _e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ============================================
# [S569-20260922] 订单身份回填（支付环节，只用强键）
# ============================================
def _backfill_order_identity(order_id, mp_openid='', unionid='', openid='', source='') -> bool:
    """[S569-20260922] 支付环节把【下单时缺失的订单身份】补回来（只用强键，绝不用手机号）。

    根因（2026-09-22 订单 138288 / order_no=20260922095311408576 / ¥21.80 / phone=17723008231）：
      下单时客户端没带 openid -> 后端只能按手机号认人；该手机号名下挂着两套微信身份
      （新主体小程序 wx281a9540a6a5b64d 的 oXTD3x… + 7 月老身份 oLhbm2…/oWrA811Z…），
      resolve_user_identity 判出 multiple_phone_user_ids -> _resolve_user 拒绝自动选择
      -> 订单 user_id=0 / mp_openid='' / openid='' / unionid=''。
      钱包页 /api/user/balance 显示的是 calc_balance() 按身份【现算】的值（SQL 里
      AND o.user_id = <身份>），user_id=0 匹配不上 -> 账本上有钱却显示 0；客服自助页
      按订单 openid 匹配也查不到 -> 用户只能打电话。
    为什么在支付环节补：微信统一下单/支付回调都已经拿【付款人真实 openid】跟微信校验过
      （unifiedorder 用不匹配的 openid 会直接报错），这是比手机号强得多的身份证据。

    保护（正常单零变化）：
      1) 订单 user_id>0 且 mp_openid 非空 -> 一个字都不写，直接返回 False；
      2) 只用强键 mp_openid -> unionid -> openid 逐个调 resolve_user_identity，**绝不传手机号**；
      3) 任一步解出 ambiguous -> 立刻放弃（绝不猜另一个账号）；
      4) 解不出 user_id>0 -> 不写库；
      5) 只有【一条】UPDATE，且 WHERE 带 COALESCE(mp_openid,'')='' ；
         每列都是"只在当前为空时写"（COALESCE(NULLIF(列,''), 新值)），已有值绝不覆盖；
      6) 任何异常只记日志，绝不抛、绝不影响支付主流程。
    返回 True 表示确实写了库。
    """
    try:
        _oid = int(order_id or 0)
    except Exception:
        return False
    if _oid <= 0:
        return False
    _mp = _clean(mp_openid)
    _un = _clean(unionid)
    _op = _clean(openid)
    if not (_mp or _un or _op):
        return False
    _conn = None
    try:
        from database import get_db as _gdb569
        _conn = _gdb569()
        _cur = _conn.cursor()
        _cur.execute("""SELECT COALESCE(order_no, '') AS order_no,
                               COALESCE(user_id, 0) AS user_id,
                               COALESCE(mp_openid, '') AS mp_openid,
                               COALESCE(openid, '') AS openid,
                               COALESCE(unionid, '') AS unionid
                        FROM orders WHERE id = %s""", (_oid,))
        _row = _cur.fetchone()
        if not _row:
            return False
        _d = dict(_row)
        _order_no = _d.get('order_no') or ''
        if int(_d.get('user_id') or 0) > 0 and _clean(_d.get('mp_openid')):
            return False                       # 正常单：一个字都不写
        # 强键逐个认人（顺序固定 mp_openid -> unionid -> openid；绝不带 phone）
        _uid = 0
        _hit_mp = ''
        _hit_un = ''
        for _key, _val in (('mp_openid', _mp), ('unionid', _un), ('openid', _op)):
            if not _val:
                continue
            _kw = {'mp_openid': '', 'unionid': '', 'openid': '', 'user_id': 0}
            _kw[_key] = _val
            try:
                _ident = resolve_user_identity(_cur, **_kw)
            except Exception as _re:
                logger.warning('[S569] 身份回填：强键(%s)解析异常 order=%s err=%s', _key, _oid, _re)
                continue
            if _ident.get('ambiguous'):
                logger.warning('[S569] 身份回填：强键(%s)多义，拒绝猜测 order=%s/%s', _key, _oid, _order_no)
                return False
            if int(_ident.get('user_id') or 0) > 0:
                _uid = int(_ident['user_id'])
                _hit_mp = _clean(_ident.get('mp_openid'))
                _hit_un = _clean(_ident.get('unionid'))
                break
        if _uid <= 0 and _mp:
            # 退路：按 mp_openid 在 users 表里唯一命中一行 -> 那行的 id 就是 user_id
            try:
                _cur.execute('SELECT id FROM users WHERE mp_openid = %s AND id > 0 ORDER BY id', (_mp,))
                _rs = _cur.fetchall()
                if len(_rs) == 1:
                    _r0 = _rs[0]
                    _uid = int((_r0.get('id') if hasattr(_r0, 'get') else _r0[0]) or 0)
                    _hit_mp = _mp
            except Exception:
                _uid = 0
        if _uid <= 0:
            logger.info('[S569] 身份回填：强键解不出 user_id，不写库 order=%s/%s src=%s (mp=%s...)',
                        _oid, _order_no, source or '?', (_mp or '-')[:8])
            return False
        # 一条 UPDATE，逐列"只在空时写"，已有值绝不覆盖
        _sets = ['user_id = CASE WHEN COALESCE(user_id, 0) > 0 THEN user_id ELSE %s END']
        _params = [_uid]
        _new_mp = _mp or _hit_mp
        if _new_mp:
            _sets.append("mp_openid = COALESCE(NULLIF(mp_openid, ''), %s)")
            _params.append(_new_mp)
        _new_un = _un or _hit_un
        if _new_un:
            _sets.append("unionid = COALESCE(NULLIF(unionid, ''), %s)")
            _params.append(_new_un)
        if _op:
            _sets.append("openid = COALESCE(NULLIF(openid, ''), %s)")
            _params.append(_op)
        _params.append(_oid)
        _cur.execute('UPDATE orders SET ' + ', '.join(_sets)
                     + " WHERE id = %s AND COALESCE(mp_openid, '') = ''", tuple(_params))
        _n = int(getattr(_cur, 'rowcount', 0) or 0)
        if _n <= 0:
            logger.info('[S569] 身份回填：UPDATE 未命中（已被并发补全或 mp_openid 非空）order=%s', _oid)
            return False
        logger.info('[S569] 订单身份回填成功: order=%s/%s src=%s user_id=%s mp_openid=%s... unionid=%s...',
                    _oid, _order_no, source or '?', _uid, (_new_mp or '-')[:8], (_new_un or '-')[:8])
        _s556_write_alarm(
            'order_identity_backfilled',
            '订单身份回填 [%s] order_id=%s order_no=%s user_id=%s mp_openid=%s...(前8位) '
            'openid=%s...(前8位)（下单时身份缺失，支付环节用微信确认的付款人 openid 补回；'
            '此前钱包页显示 0、客服自助查不到单）'
            % (source or '?', _oid, _order_no, _uid, (_new_mp or '-')[:8], (_op or _mp or '-')[:8]))
        return True
    except Exception as _e:
        logger.error('[S569] 订单身份回填失败(不影响支付主流程): order=%s src=%s err=%s',
                     order_id, source or '?', _e)
        return False
    finally:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass


def _s556_detect_real_channel(order_no, cur_ch_type, amount=0, cur_channel_id=None):
    """[S556-20260922] bug② —— 用 out_trade_no 去【另一个渠道】查单，确认真实收款渠道。

    只认"渠道明确回单笔交易成功"才算数（微信 trade_state=SUCCESS / 支付宝 TRADE_SUCCESS）。
    返回 dict(channel_id, ch_type, name, transaction_id) 或 None。只读，不发起任何写请求。
    """
    if not order_no:
        return None
    try:
        from database import get_db as _gdb556
        conn = _gdb556()
        cur = conn.cursor()
        if cur_ch_type == 'alipay':
            cur.execute("SELECT * FROM payment_channels WHERE channel_type='wechat' "
                        "AND is_active=1 ORDER BY weight DESC, id ASC")
        elif cur_ch_type == 'wechat':
            cur.execute("SELECT * FROM payment_channels WHERE channel_type='alipay' "
                        "AND is_active=1 ORDER BY weight DESC, id ASC")
        else:
            cur.execute("SELECT * FROM payment_channels "
                        "WHERE channel_type IN ('wechat','alipay') AND is_active=1 "
                        "ORDER BY weight DESC, id ASC")
        cands = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as _e:
        logger.error('[S556] 反查候选渠道失败: %s', _e)
        return None
    for ch in cands:
        try:
            if int(ch.get('id') or 0) == int(cur_channel_id or 0):
                continue
            client, ctype = get_channel_wxpay(ch)
            if client is None or ctype not in ('wechat', 'alipay'):
                continue
            if ctype == 'wechat':
                r = client.order_query(out_trade_no=order_no) or {}
                _ok = (str(r.get('return_code') or '') == 'SUCCESS'
                       and str(r.get('trade_state') or '').upper() == 'SUCCESS')
                _txn = r.get('transaction_id') or ''
                _detail = 'trade_state=%s total_fee=%s' % (r.get('trade_state'),
                                                           r.get('total_fee'))
            else:
                r = client.query(out_trade_no=order_no) or {}
                _ok = (str(r.get('code') or '') == '10000'
                       and str(r.get('trade_status') or '').upper() in ('TRADE_SUCCESS',
                                                                        'TRADE_FINISHED'))
                _txn = r.get('trade_no') or ''
                _detail = 'trade_status=%s total_amount=%s' % (r.get('trade_status'),
                                                               r.get('total_amount'))
            logger.warning('[S556] 反查渠道%s(%s) 订单 %s -> %s%s', ch.get('id'), ctype, order_no,
                           ('命中 ' if _ok else ''), _detail)
            if _ok:
                return {'channel_id': ch.get('id'), 'ch_type': ctype,
                        'name': ch.get('name') or '', 'transaction_id': _txn or ''}
        except Exception as _e:
            logger.warning('[S556] 反查渠道%s异常(继续试下一个): %s', ch.get('id'), _e)
    return None


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
        # [S556-20260922] bug② 换渠道重试后会改写 payer/_s538_ch_type，这里先给初值
        _s556_alt = None
        _s556_swapped = False
        _s556_ord_ch = 0
        _s556_ch_used = 0
        _s556_retry_no = ''
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
                total_fee = int(round((float(order_row['deposit_amount']) + float(order_row.get('per_use_price') or 0)) * 100))
            else:
                total_fee = int(round(float(amount) * 100))
        else:
            total_fee = int(round(float(amount) * 100))
        refund_fee = int(round(float(amount) * 100))
        # [S641-20260923] 退款前先向微信【只读查单】拿真实收款金额, 用它当 total_fee, 并把
        #   refund_fee 夹到"不超过微信实际收款额"。微信侧的 total_fee 是【唯一可信】的金额来源。
        #   起因 order 137043(order_no 20260921111820684143, 预付款 20.24)：下单侧老缺陷
        #   int(20.24*100)=2023 让用户实际只付了 2023 分, 而我们库里记 20.24, 退款时我们报
        #   total_fee=2024/refund_fee=2024 -> 微信回
        #   "订单金额或退款金额与之前请求不一致，请核实后再试", 一直退不了。
        #   查单失败/超时/字段缺失/非微信渠道 一律【回落】到上面的原算法 —— 绝不因查单失败拒退或退错;
        #   查单是只读接口, 不改任何本地状态, 异常只记 warning。支付宝分支一个字都不动。
        if _s538_ch_type == 'wechat' and payer is not None:
            try:
                _s641_q = payer.order_query(out_trade_no=order_no) or {}
                _s641_tf = str(_s641_q.get('total_fee') or '').strip()
                if str(_s641_q.get('return_code') or '') == 'SUCCESS' and _s641_tf.isdigit() and int(_s641_tf) > 0:
                    _s641_real = int(_s641_tf)
                    logger.info('[S641] 微信实收查单: order=%s total_fee=%d 分 cash_fee=%s '
                                '(本地算出 total_fee=%d refund_fee=%d)',
                                order_no, _s641_real, _s641_q.get('cash_fee'), total_fee, refund_fee)
                    total_fee = _s641_real
                    if refund_fee > _s641_real:
                        logger.warning('[S641] 退款金额>微信实收, 按实收退: order=%s refund_fee %d -> %d 分',
                                       order_no, refund_fee, _s641_real)
                        refund_fee = _s641_real
                else:
                    logger.warning('[S641] 微信查单未取到 total_fee(回落原算法): order=%s rc=%s err=%s total_fee=%r',
                                   order_no, _s641_q.get('return_code'),
                                   _s641_q.get('err_code') or _s641_q.get('return_msg'),
                                   _s641_q.get('total_fee'))
            except Exception as _s641_qe:
                logger.warning('[S641] 微信查单异常(回落原算法): order=%s err=%s', order_no, _s641_qe)
        # [S556-20260922] bug② —— 跨渠道重试必须沿用【同一个确定性退款单号】才算渠道内幂等，
        #   所以微信(out_refund_no) 与支付宝(out_request_no) 共用下面这一个串。
        #   （原来支付宝走 'RF<id>_<分>'、微信走 wxpay 内部随机单号，两条串不一致 -> 重试那次
        #     在渠道侧是一笔全新退款，不再受幂等保护。）
        _s556_retry_no = out_refund_no or ('S556RF%s_%d' % (order_no, refund_fee))
        def _s556_attempt(ch_type, ch_payer):
            """[S556] 单次渠道退款调用。微信与支付宝共用【同一个确定性单号】_s556_retry_no：
            换渠道重试时两次必须同号才算渠道内幂等，否则重试那次在渠道侧是一笔全新退款。
            其余参数与 S541 口径逐字一致（支付宝 refund_amount 传元、微信传分）。"""
            if ch_type == 'alipay':
                _r = ch_payer.refund(out_trade_no=order_no, refund_amount=float(amount),
                                     out_request_no=_s556_retry_no, refund_reason='原路退款')
            else:
                _r = ch_payer.refund(out_trade_no=order_no, total_fee=total_fee,
                                     refund_fee=refund_fee, out_refund_no=_s556_retry_no)
            _b = ' '.join([str(_r.get('err_code') or ''), str(_r.get('err_code_des') or ''),
                           str(_r.get('return_msg') or ''), str(_r.get('sub_code') or ''),
                           str(_r.get('sub_msg') or ''), str(_r.get('msg') or ''),
                           str(_r.get('_raw_body') or '')[:600]]).upper()
            _ne = (('ORDERNOTEXIST' in _b) or ('ORDER_NOT_EXIST' in _b)
                   or ('TRADE_NOT_EXIST' in _b) or ('交易不存在' in _b) or ('订单不存在' in _b))
            return _r, _ne

        # [S541-20260922] 保留原名给下面支付宝的 fund_change 复核用（值就是那个确定性单号）
        _s538_req_no = _s556_retry_no
        result, _s556_not_exist = _s556_attempt(_s538_ch_type, payer)
        # [S556-20260922] bug② —— 退款渠道按【实际支付流水】自动识别，兜历史脏单：
        #   只有渠道【明确回"交易不存在"】时（这种失败绝无资金变动）才用 out_trade_no 反查另一渠道；
        #   查到真实收款渠道就用【同一个确定性退款单号】重试一次。
        if _s556_not_exist:
            try:
                _s556_alt = _s556_detect_real_channel(order_no, _s538_ch_type, amount=amount,
                                                      cur_channel_id=_s556_ord_ch)
            except Exception as _s556_de:
                _s556_alt = None
                logger.error('[S556] 反查真实收款渠道异常: order=%s err=%s', order_no, _s556_de)
            if _s556_alt:
                logger.warning('[S556] 渠道记账有误: order=%s 订单渠道=%s(%s) 但真实收款=%s(%s) '
                               '-> 换渠道重试退款（沿用同一个确定性退款单号 %s）',
                               order_no, _s556_ord_ch, _s538_ch_type,
                               _s556_alt.get('channel_id'), _s556_alt.get('ch_type'),
                               _s556_retry_no)
                _c2 = None
                try:
                    _c2 = get_db()
                    _c2c = _c2.cursor()
                    _c2c.execute('SELECT * FROM payment_channels WHERE id=%s',
                                 (_s556_alt.get('channel_id'),))
                    _r2 = _c2c.fetchone()
                    _c2.close()
                    _c2 = None
                    if _r2:
                        _p2, _t2 = get_channel_wxpay(dict(_r2))
                        if _p2 is not None and _t2:
                            payer = _p2
                            _s538_ch_type = _t2
                            _s556_swapped = True
                            _s556_ch_used = int(_s556_alt.get('channel_id') or 0)
                            result, _s556_not_exist = _s556_attempt(_t2, _p2)
                except Exception as _s556_ee:
                    logger.error('[S556] 换渠道重试异常(按本次结果处理): order=%s err=%s',
                                 order_no, _s556_ee)
                finally:
                    if _c2 is not None:
                        try:
                            _c2.close()
                        except Exception:
                            pass
            else:
                logger.error('[S556] 订单渠道 %s 报"交易不存在"，反查微信/支付宝也没查到真实收款: '
                             'order=%s（疑似未收款或交易号异常，不再盲目重试）',
                             _s556_ord_ch, order_no)
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
            # [S556-20260922] bug② —— 若这次成功是"换渠道重试"换来的，把订单渠道纠正成真实渠道
            if _s556_swapped:
                try:
                    _s556_correct_order_channel(
                        order_id, _s556_alt.get('channel_id'), source='refund-detect',
                        order_no=order_no, ch_type=_s556_alt.get('ch_type') or '',
                        transaction_id=_s556_alt.get('transaction_id') or '')
                except Exception as _s556_se:
                    logger.error('[S556] 退款后纠正渠道失败(不影响退款): %s', _s556_se)
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
    于是"只退了一部分却整张单标记已通过", 剩余预付款既没退给用户、余额明细又被隐藏(pending),
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
            amount=int(round(float(amount) * 100)),
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
    # [S649-20260924] 缓存键必须带 appid（根因）。
    #   原来读/写都用固定键 'wx_mp_access_token'（不含 appid）：后台把生效小程序切成
    #   另一个账号后，缓存里那条"切换前"的 token 仍会被当成本账号的 token 使用 ->
    #   微信返回 40003 invalid openid（2026-09-24 12:29~12:41 生产实测，
    #   该缓存直到 12:58:56 才过期）。
    #   改成 'wx_mp_access_token_<当前生效appid>'，与其它小程序互不干扰；
    #   旧键既不再读、也不再写。appid 只取一次，缓存键与请求体必定同源。
    #   除"缓存键"与"appid 只取一次"外，返回值 / 失败返回 None / 异常处理与改前一致。
    from datetime import datetime, timedelta
    try:
        _appid = _wx_mp_id()
        _token_key = 'wx_mp_access_token_' + (_appid or '')
        conn = get_db()
        cur = conn.cursor()
        if not force_refresh:
            cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s", (_token_key,))
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
        payload = dict(grant_type='client_credential', appid=_appid, secret=_wx_mp_secret(), force_refresh=force_refresh)
        resp = _r.post(url, json=payload, timeout=5)
        result = resp.json()
        if 'access_token' in result:
            token = result['access_token']
            ei = result.get('expires_in', 7200)
            ea = (datetime.now() + timedelta(seconds=ei)).isoformat()
            import json as _j2
            cd = _j2.dumps(dict(token=token, expires_at=ea))
            cur.execute("INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (%s, %s)", (_token_key, cd))
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


def check_whitelist(openid='', unionid='', phone=''):
    """提现免审白名单查询。

    [S588-20260922] 匹配优先级：**手机号 → unionid → openid**（老板口径）。
    原因：换小程序/换微信号后 openid 会变，老名单只按 openid/unionid 存就认不出本人，
    用户"手机号明明在白名单里却匹配不上"。手机号是稳定身份，故优先。
    边界（头号铁律）：phone 只用于本函数对 withdrawal_whitelist 表自身的匹配，
    不做任何跨表身份兜底 / 认人 / 合并账户；phone 为空时行为与改动前完全一致。
    """
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        # [S588] 手机号优先
        if phone:
            cur.execute("SELECT openid, source, remain_count, unionid, created_at FROM withdrawal_whitelist WHERE phone = %s AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1", (str(phone),))
            row = cur.fetchone()
            if row:
                conn.close()
                return row
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


def add_whitelist(openid, source, remain_count=-1, unionid='', expire_days=None, phone=''):
    """加入提现白名单。expire_days>0: N天后过期; 重复拉白时次数取较小值(不重置回满), 有效期不刷新(保留原值)

    [S588-20260922] 双写：有手机号就一并写入 phone 列（原来是 NULL，换微信号后按手机号认不出）。
    按 unionid/openid 命中已有行时也把 phone 补上（COALESCE 语义：不覆盖已有非空 phone）。
    边界（头号铁律）：phone 只写 withdrawal_whitelist 自己这一列，不写 orders/users/user_balances。
    """
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        if not unionid:
            unionid = _resolve_unionid(openid=openid)
        # 过期天数参数化(make_interval), 不能拼SQL字符串当参数传, 否则报timestamp语法错
        _exp_days = int(expire_days) if (expire_days is not None and expire_days > 0) else None
        # [S588] 手机号规范化：空串统一成 None 语义由 COALESCE(NULLIF(...)) 处理
        _phone = str(phone) if phone else ''
        if unionid:
            cur.execute("SELECT openid FROM withdrawal_whitelist WHERE unionid = %s LIMIT 1", (unionid,))
            exist = cur.fetchone()
            if exist:
                cur.execute("""UPDATE withdrawal_whitelist SET openid = %s, source = %s,
                               phone = COALESCE(NULLIF(%s, ''), phone),
                               remain_count = CASE
                                 WHEN withdrawal_whitelist.remain_count = -1 THEN %s
                                 WHEN %s = -1 THEN withdrawal_whitelist.remain_count
                                 ELSE LEAST(withdrawal_whitelist.remain_count, %s)
                               END
                               WHERE unionid = %s""",
                            (openid, source, _phone, remain_count, remain_count, remain_count, unionid))
                conn.commit()
                conn.close()
                return True
        sql = """INSERT INTO withdrawal_whitelist (openid, source, remain_count, unionid, created_at, expires_at, phone)
                 VALUES (%s, %s, %s, %s, NOW(), NOW() + make_interval(days => %s), %s)
                 ON CONFLICT (openid) DO UPDATE SET source = EXCLUDED.source,
                   phone = COALESCE(NULLIF(EXCLUDED.phone, ''), withdrawal_whitelist.phone),
                   remain_count = CASE
                     WHEN withdrawal_whitelist.remain_count = -1 THEN EXCLUDED.remain_count
                     WHEN EXCLUDED.remain_count = -1 THEN withdrawal_whitelist.remain_count
                     ELSE LEAST(withdrawal_whitelist.remain_count, EXCLUDED.remain_count)
                   END,
                   unionid = COALESCE(NULLIF(EXCLUDED.unionid, ''), withdrawal_whitelist.unionid),
                   expires_at = withdrawal_whitelist.expires_at"""
        cur.execute(sql, (openid, source, remain_count, unionid, _exp_days, _phone))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error("[add_whitelist] " + str(e))
        return False


def check_whitelist_today(openid='', unionid='', phone=''):
    """当天投诉白名单：source=complaint 且白名单创建于今天（北京时间）

    [S588-20260922] 匹配优先级：手机号 → unionid → openid（与 check_whitelist 一致）；
    phone 为空时执行路径与改动前逐字相同。
    """
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        conds = ["source = 'complaint'"]
        params = []
        if phone:
            # [S588] 手机号优先（只匹配白名单表自己的 phone 列）
            conds.append("phone = %s")
            params.append(str(phone))
        elif unionid:
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
            # [S588] 与白名单匹配口径取齐：本函数下方 count_today_whitelist_uses 本来就按 phone 计数，
            # 这里也把 phone 传进去，避免"按手机号命中的白名单在限额判断上漏看"。
            _wl = check_whitelist_today(openid, unionid, phone)
            if _wl and count_today_whitelist_uses(phone, openid) >= daily:
                return '今日使用次数已达上限'
    except Exception as e:
        logger.error("[check_use_limits] " + str(e))
    return None


def consume_whitelist(openid, phone=''):
    """消费一次白名单(限次来源扣1,扣完删除; -1不限次不扣; 过期不消费)

    [S588-20260922] 新增可选 phone：按手机号命中的白名单行，其 openid 往往是【老 openid】，
    只按当前 openid 扣次会扣不到（等于漏扣、白名单次数不清零），所以支持用手机号定位同一行。
    顺序：phone 优先 → openid；phone 为空时 _keys 只有 openid，SQL 与改动前逐字相同。
    边界（头号铁律）：phone 只用于定位 withdrawal_whitelist 自己的行。
    """
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        _keys = []
        if phone:
            _keys.append(('phone', str(phone)))
        if openid:
            _keys.append(('openid', openid))
        for _col, _val in _keys:
            cur.execute("UPDATE withdrawal_whitelist SET remain_count = remain_count - 1 WHERE " + _col + " = %s AND remain_count > 0 AND (expires_at IS NULL OR expires_at > NOW())", (_val,))
            if cur.rowcount > 0:
                cur.execute("DELETE FROM withdrawal_whitelist WHERE " + _col + " = %s AND remain_count <= 0", (_val,))
                conn.commit()
                conn.close()
                return True
            cur.execute("SELECT remain_count FROM withdrawal_whitelist WHERE " + _col + " = %s AND (expires_at IS NULL OR expires_at > NOW())", (_val,))
            row = cur.fetchone()
            if row is not None:
                if row["remain_count"] == -1:
                    conn.commit()
                    conn.close()
                    return True
                # 命中该键但已无可用次数：与改动前一致，直接结束(不再尝试下一个键，避免重复扣次)
                break
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
        # [S588] 老行为保留：查不到 openid 就不拉白(白名单表 openid 是主键且 NOT NULL，不能只写手机号)
        logger.warning("[add_whitelist_by_phone] phone=" + str(phone) + " no openid")
        return False
    unionid = _resolve_unionid(openid=openid, phone=phone)
    # [S588] 双写：手机号一并落库（原来只落 openid/unionid，换微信号后就按手机号认不出）
    return add_whitelist(openid, source, remain_count, unionid, expire_days, phone=phone)


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
            # [S588] 双写：有手机号就一并写入白名单 phone 列
            return add_whitelist(openid, source, remain, unionid or '', expire_days=days, phone=phone or '')
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
    'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA': 'oa_sub_general',   # 预付款退还
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


# ============================================================
# [S700-20260927] 公众号/小程序的【全部】已登记前缀（不看 is_active）
#   为什么要它：oa_openid_prefix() / mp_openid_prefix() 只返回"当前生效"的那一个，
#   而线上同时有 3 代公众号（oLhbm2/ov47M3/octN92）和 4 代小程序
#   （ooTcRx/oWrA8/oXTD3x/oQXFs3）的 openid 在库里，只认一个会漏掉其它代。
#   读不到库时返回空 -> 判定一律不通过 -> 只"少做"不"做错"，方向是安全的。
# ============================================================
_OA_ALL_PREFIX_CACHE = {'ts': 0.0, 'ps': ()}
_MP_ALL_PREFIX_CACHE = {'ts': 0.0, 'ps': ()}


def _all_prefixes(acct_type, cache):
    import time as _t700
    _now = _t700.time()
    if cache['ps'] and (_now - cache['ts']) < 60:
        return cache['ps']
    _ps = ()
    try:
        import wx_config as _wc700
        _ps = tuple(sorted({str(r.get('openid_prefix') or '').strip()
                            for r in (_wc700.list_accounts(acct_type) or [])
                            if str(r.get('openid_prefix') or '').strip()}, key=len, reverse=True))
    except Exception:
        _ps = ()
    cache['ts'] = _now
    cache['ps'] = _ps
    return _ps


def oa_prefixes_all():
    """全部已登记公众号的 openid 前缀（含已停用账号）"""
    return _all_prefixes('oa', _OA_ALL_PREFIX_CACHE)


def mp_prefixes_all():
    """全部已登记小程序的 openid 前缀（含已停用的旧号）"""
    return _all_prefixes('mp', _MP_ALL_PREFIX_CACHE)


def is_oa_openid_any(openid):
    """这个 openid 是不是【任意一代已登记公众号】的"""
    _x = (openid or '').strip()
    try:
        return bool(_x) and any(_x.startswith(p) for p in oa_prefixes_all())
    except Exception:
        return False


def is_mp_openid_any(openid):
    """这个 openid 是不是【任意一代已登记小程序】的"""
    _x = (openid or '').strip()
    try:
        return bool(_x) and any(_x.startswith(p) for p in mp_prefixes_all())
    except Exception:
        return False


def _s700_same_person(cur, new_openid, unionid):
    """同人护栏：unionid 非空、且换出来的 openid 确实挂在这个 unionid 名下，才算同一个人。
    确认不了就返回 False —— 宁可不换（发不出），也绝不发错人。"""
    if not new_openid or not unionid:
        return False
    try:
        cur.execute("SELECT 1 FROM users WHERE (openid=%s OR mp_openid=%s) AND unionid=%s LIMIT 1",
                    (new_openid, new_openid, unionid))
        return bool(cur.fetchone())
    except Exception:
        return False


def find_oa_openid(phone='', unionid=''):
    """找该用户的【公众号】openid（前缀 oLhbm2）：phone_openids.gzh_openid -> users.openid -> phone_openids.openid -> 按 unionid 跨手机号"""
    try:
        from database import get_db
        _c = get_db()
        _cur = _c.cursor()
        # [S700-20260927] 原来只找"当前生效"的那一个公众号前缀（生产实测 = ov47M3），
        #   而线上同时有 3 代公众号（oLhbm2/ov47M3/octN92）在用 -> 另外两代用户
        #   "找不到公众号身份"，连"小程序发不出就降级发公众号通知"那条兜底也一起失败。
        #   S699 A/B：抽 300 个手机号，旧口径找得到 7 个，新口径 243 个，零回退零污染。
        _prefs = [p + '%' for p in oa_prefixes_all()] or [oa_openid_prefix() + '%']
        _oid = ''
        if phone:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE phone=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE ANY(%s) ORDER BY id ASC LIMIT 1", (phone, _prefs))
            _r = _cur.fetchone()
            if _r and _r.get('gzh_openid'):
                _oid = _r['gzh_openid']
            if not _oid:
                _cur.execute("SELECT openid FROM users WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE ANY(%s) ORDER BY id ASC LIMIT 1", (phone, _prefs))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
            if not _oid:
                _cur.execute("SELECT openid FROM phone_openids WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE ANY(%s) ORDER BY id ASC LIMIT 1", (phone, _prefs))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
        if not _oid and unionid:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE unionid=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE ANY(%s) ORDER BY id ASC LIMIT 1", (unionid, _prefs))
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

    只对我们登记过映射的三个模板生效（寄存成功/预付款退还/退款成功），其它模板直接返回 False。
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
# [S231-20260917] "预付款退还通知" -> "账户余额通知" 换模板过渡用的两个 ID
# 换模板后老用户手里只有旧模板的授权（微信按模板 ID 记账），新模板会被拒(43101 无额度)，
# 所以发送失败时用旧模板再发一次，过渡期一条通知都不丢。
# ============================================
_TPL_ACCOUNT_NEW = 'ax-O5Qa05IWt7bbhRVk9Pb9A_SbXfIMfbhm0Hoh4gYc'   # 账户余额通知（新）
_TPL_DEPOSIT_OLD = 'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA'   # 预付款退还通知（旧，仅作过渡回退）


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
            # [S631-20260923] 微信侧拦掉的同时，补发支付宝小程序订阅消息。
            #   原来这里只拦不发 -> 支付宝用户这条"结束订单"通知两头都没有。
            #   覆盖：离线取包 routes/offline.py(2处) + 设备侧结束 routes/device.py。
            try:
                notify_alipay_order(order_id=order_id, biz='subscribe_general')
            except Exception as _s631_e:
                logger.warning('[S631] 支付宝订阅消息失败(不影响业务): %s', _s631_e)
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
# [S631-20260923] 支付宝订阅消息【接线】
#   背景：S525 已把"支付宝单"的微信侧通知全部拦掉（正确），但支付宝侧
#         send_alipay_subscribe_message 自 S526 引入后【0 个调用点】
#         -> 支付宝用户两头都收不到通知（订单 139006 实测）。
#   口径：本函数是支付宝订阅消息的【唯一接线点】。
#         · 闸门复用 order_notify_blocked()：与"微信侧被拦"严格互为反面，
#           即"微信侧被拦 <=> 这里发"，不会出现两边都发或两边都不发。
#         · uid 只用 orders.alipay_pay_uid / orders.alipay_mp_uid，
#           **绝不按手机号反查微信身份**；两者都空 -> warning 后跳过。
#         · template_id 一律传 ''，由 send_alipay_subscribe_message 按 biz 查库兜底。
#         · 本函数绝不抛异常、绝不改变调用方返回值，失败只记日志。
# ============================================================
def notify_alipay_order(order=None, order_id=None, order_ids=None, biz='',
                        data=None, page='pages/mine/mine', cursor=None):
    """[S631] 支付宝单 -> 发支付宝小程序订阅消息；非支付宝单 = 纯 no-op（返回 False）。

    biz : 'subscribe_general'（结束订单/预付款退还）| 'subscribe_refund'（退款成功）
    data: None 时按 biz 自动构造【支付宝真实模板关键词】（[S633-20260923] 已按只读接口实测改正）：
          general（c142ac..「账户余额通知」）
              -> keyword1 账户余额 / keyword2 变动时间 / keyword3 温馨提示 / keyword4 温馨提醒
          refund （de68d9..「寄存预付款退还通知」）
              -> keyword1 寄存单号 / keyword2 退还时间 / keyword3 退还状态 / keyword4 退还金额
          依据 = alipay.open.mini.message.template.batchquery(biz_type=sub_msg) 返回的
          keyword_desc：「账户余额,变动时间,温馨提示,温馨提醒」/「寄存单号,退还时间,退还状态,退还金额」；
          官方 data 的 notice：「选用模板时配置的关键字顺序与 keyword_x 相互对应」；
          个数不符 -> USER_TEMPLATE_LACK_KEYWORD；单个 value 上限 50 字符。
          ⚠️ amount1/time2/thing3/thing4/amount2/time5 是【微信】订阅消息的字段名，支付宝侧
             一次都不要用（S631b 曾把它们当支付宝字段名，那是错的）。
    """
    try:
        _biz = str(biz or '').strip()
        # 应急开关放最前面：关掉后本函数完全惰性（一次查询都不做、一个请求都不发）
        try:
            import wx_config as _wc631
            if str(_wc631.get_config('alipay_subscribe_enabled', 'true')).strip().lower() in (
                    '0', 'false', 'off', 'no'):
                logger.info('[S631] 开关 alipay_subscribe_enabled=off，notify_alipay_order 整体跳过')
                return False
        except Exception:
            pass
        _o = dict(order) if order else None
        _oid = order_id or ((_o or {}).get('id'))
        # 闸门 = "微信侧被拦"的同一判据（严格互为反面）
        if not order_notify_blocked(order=_o, order_id=_oid, order_ids=order_ids, cursor=cursor):
            return False
        # 只给了 order_id / order_ids 时，补一次只读 SELECT 取支付宝身份
        if _o is None:
            _qid = _oid
            if not _qid and order_ids:
                try:
                    _qid = list(order_ids)[0]
                except Exception:
                    _qid = None
            if _qid:
                try:
                    _c631 = cursor
                    _own631 = False
                    if _c631 is None:
                        from database import get_db as _g631
                        _c631 = _g631()
                        _own631 = True
                    _cu631 = _c631.cursor()
                    _cu631.execute("SELECT id, order_no, deposit_amount, refund_amount, "
                                   "alipay_mp_uid, alipay_pay_uid FROM orders WHERE id=%s", (_qid,))
                    _r631 = _cu631.fetchone()
                    if _r631:
                        _o = dict(_r631)
                    if _own631:
                        try:
                            _c631.close()
                        except Exception:
                            pass
                except Exception as _e631:
                    logger.warning('[S631] 补查订单失败 id=%s: %s', _qid, _e631)
        _o = _o or {}
        # uid：只认支付宝身份列，绝不使用手机号
        _uid = str(_o.get('alipay_pay_uid') or _o.get('alipay_mp_uid') or '').strip()
        if not _uid:
            logger.warning('[S631] 支付宝单缺 alipay uid，跳过订阅消息 '
                           'order_id=%s order_ids=%s（不按手机号反查微信身份）', _oid, order_ids)
            return False
        if data is None:
            # [S633-20260923] 键名/顺序按支付宝【真实模板关键词】构造（S631b 用微信字段名是错的）：
            #   subscribe_general（c142ac2357774daab8994a0f5a91faa4 账户余额通知）
            #       keyword1 账户余额 / keyword2 变动时间 / keyword3 温馨提示 / keyword4 温馨提醒
            #   subscribe_refund （de68d98e94c84477b1b9e116fcb8cbfa 寄存预付款退还通知）
            #       keyword1 寄存单号 / keyword2 退还时间 / keyword3 退还状态 / keyword4 退还金额
            #   依据：只读接口 alipay.open.mini.message.template.batchquery 的 keyword_desc
            #         =「账户余额,变动时间,温馨提示,温馨提醒」/「寄存单号,退还时间,退还状态,退还金额」；
            #         官方 data notice「选用模板时配置的关键字顺序与 keyword_x 相互对应」；
            #         官方错误码 USER_TEMPLATE_LACK_KEYWORD「必须有 keyword1~keywordN 的对象和 value」。
            #   键名与顺序【不要照搬微信】：amount*/time*/thing* 是微信订阅消息的字段名。
            #   value 上限 50 字符（USER_KEYWORD_LENGTH_ERROR），下面各值最长 22 字符。
            _now633 = datetime.now()
            if _biz == 'subscribe_refund':
                _amt631 = float(_o.get('refund_amount') or 0) or float(_o.get('deposit_amount') or 0)
                data = {
                    # keyword1 寄存单号：模板必需；order 是 SELECT o.* 时直接有，兜底用订单 id。
                    'keyword1': {'value': str(_o.get('order_no') or _oid or '0')[:32]},
                    'keyword2': {'value': _now633.strftime('%Y-%m-%d %H:%M:%S')},
                    'keyword3': {'value': '原路退回支付账户'},
                    'keyword4': {'value': '¥{:.2f}'.format(_amt631)},
                }
            else:
                _amt631 = float(_o.get('deposit_amount') or 0)
                data = {
                    'keyword1': {'value': '¥{:.2f}'.format(_amt631)},
                    'keyword2': {'value': _now633.strftime('%Y-%m-%d %H:%M')},
                    # keyword3 温馨提示 <- 原 thing3（长提示）；keyword4 温馨提醒 <- 原 thing4（短状态）
                    'keyword3': {'value': '请自行点击此通知消息跳转“我的钱包”提现'},
                    'keyword4': {'value': '已退还至小程序用户钱包'},
                }
        _ok631 = send_alipay_subscribe_message(_uid, '', data,
                                               page=page or 'pages/mine/mine', biz=_biz)
        if _ok631:
            logger.info('[S631] 支付宝订阅消息已发 order_id=%s biz=%s uid=%s...',
                        _oid, _biz, _uid[:8])
        else:
            logger.warning('[S631] 支付宝订阅消息未发出 order_id=%s biz=%s'
                           '（详见 [alipay_subscribe] 日志）', _oid, _biz)
        return bool(_ok631)
    except Exception as _e:
        logger.warning('[S631] notify_alipay_order 异常(不影响主流程): %s', _e)
        return False


# ============================================================
# [S526-20260921] 支付宝小程序【订阅消息】发送
#   与微信 send_wx_subscribe_message 一一对应，但口径不同：
#     · 收件人 = users.alipay_uid / phone_openids.alipay_uid / orders.alipay_*_uid。
#       该列存的是【47 位 openid】（/alipay/login 的 oauth_token 返回 open_id），
#       发送时由 alipay.AlipayClient.mini_template_message_send 按形状分流：
#       2088 开头 16 位 -> to_user_id；否则 -> to_open_id（[S633-20260923] 改正）。
#     · 模板走 wx_templates 里 channel='alipay' 的两条（account_id=0 通用）
#         subscribe_general = c142ac2357774daab8994a0f5a91faa4  账户余额通知
#         subscribe_refund  = de68d98e94c84477b1b9e116fcb8cbfa  寄存预付款退还通知
#       调用方取模板ID：wx_config.template_id('subscribe_general', 'alipay', '')
#     · data 的字段名 = 支付宝的 keyword1..keywordN，顺序 = 模板后台配置顺序：
#         subscribe_general：keyword1 账户余额 / keyword2 变动时间 /
#                            keyword3 温馨提示 / keyword4 温馨提醒
#         subscribe_refund ：keyword1 寄存单号 / keyword2 退还时间 /
#                            keyword3 退还状态 / keyword4 退还金额
#       ⚠️ amount1/time2/thing3/thing4（微信字段名）**支付宝侧一个都不要用**。
#   安全口径：任何异常只记日志、绝不抛出；alipay_uid 为空直接返回 False。
# ============================================================
_ALIPAY_TPL_GENERAL = 'c142ac2357774daab8994a0f5a91faa4'   # 账户余额通知（兜底值）
_ALIPAY_TPL_REFUND = 'de68d98e94c84477b1b9e116fcb8cbfa'    # 寄存预付款退还通知（兜底值）

# 微信字段名 -> 支付宝关键词名（改名表）。两条模板一律留空 {} = 不做改名的直通路径；
#   ★ 现在调用方直接给的就是支付宝 keyword1..keywordN（见上），所以这里不需要映射。
#   ⚠️⚠️ 勘误（[S633-20260923]，这是本文件历史上被写错两次的地方，后来人别再被带偏）：
#     ① S526 原注释写「调用方直接给 keyword1..keywordN」——**这句是对的**；
#     ② S631b 把它改成「支付宝这两条模板的关键词就是 amount1/time2/thing4/thing3」——**这句是错的**：
#        amount*/time*/thing* 是【微信】订阅消息的字段名（见 helpers.py 的 _SUBSCRIBE_FIELD_MAP
#        上方注释，那里明写「目标字段名来自微信官方 wxaapi/newtmpl/gettemplate 的 content」）。
#        S631b 把微信字段名原样搬到支付宝接口上，是张冠李戴。
#     ③ 支付宝侧的正确依据（官方三处 + 一条实测）：
#        · 官方 data example：{"keyword1":{"value":"12:00"},"keyword2":{...},"keyword3":{...}}
#        · 官方 data notice：「选用模板时配置的关键字顺序与 keyword_x 相互对应」「value 最长 50 字符」
#        · 官方错误码 USER_TEMPLATE_LACK_KEYWORD：「必须和上送的关键词匹配，例如申请了 5 个关键词，
#          则 data 数据域必须有 keyword1~keyword5 的对象和 value」
#        · 实测（只读接口 alipay.open.mini.message.template.batchquery，biz_type=sub_msg）：
#          c142ac… keyword_desc=「账户余额,变动时间,温馨提示,温馨提醒」
#          de68d9… keyword_desc=「寄存单号,退还时间,退还状态,退还金额」
#   历史值（仅作对照，勿再使用）：general 曾用 amount1/time2/thing4/thing3；
#     refund 曾用 amount2/time5/thing4/thing3。
_ALIPAY_SUBSCRIBE_FIELD_MAP = {
    _ALIPAY_TPL_GENERAL: {},
    _ALIPAY_TPL_REFUND: {},
}

# [S530-20260921] 微信字段名 -> biz：调用方没给 biz 时，用 data 里带的微信字段名反推
#   ⚠️ [S633-20260923] 现在 data 是 keyword1..keywordN，两组 biz 的键名完全相同、无法再按字段名区分，
#      所以这条反推对本项目已失效。**所有现存调用点都显式传 biz**（notify_alipay_order 恒传），
#      故实际不受影响；缺 biz 时会推不出 -> template_id 为空 -> 安全跳过（不发送），不会发错模板。
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
      alipay_uid  = 收件人支付宝标识（users.alipay_uid / phone_openids.alipay_uid）。
                    本列实际存的是 47 位 **openid**；由底层按形状分流到 to_open_id / to_user_id。
      template_id = wx_templates channel='alipay' 里那条的 template_id；
                    取法：wx_config.template_id('subscribe_general'|'subscribe_refund', 'alipay', '')
                    ★ [S530] 但该取法对 alipay 通道**永远返回空**（配置中心 CHANNELS 只有 mp/oa）。
                      为兼容旧调用方，参数保持原样；**传空时本函数自动按 biz 查库兜底**。
      data        = dict，键名必须是**支付宝的 keyword1..keywordN**，顺序 = 模板配置顺序
                    （[S633-20260923] 按只读接口 batchquery 实测改正；S631b 用微信字段名是错的）：
                    general（c142ac.. 账户余额通知）
                      -> {'keyword1': {'value': '¥30.00'},        # 账户余额
                          'keyword2': {'value': '2026-09-23 10:00'},  # 变动时间
                          'keyword3': {'value': '请自行点击此通知消息跳转“我的钱包”提现'},  # 温馨提示
                          'keyword4': {'value': '已退还至小程序用户钱包'}}                  # 温馨提醒
                    refund （de68d9.. 寄存预付款退还通知）
                      -> {'keyword1': {'value': '20260922233049039873'},  # 寄存单号（模板必需）
                          'keyword2': {'value': '2026-09-23 10:00:00'},   # 退还时间
                          'keyword3': {'value': '原路退回支付账户'},       # 退还状态
                          'keyword4': {'value': '¥30.00'}}                # 退还金额
                    ⚠️ 不要传 amount1/time2/thing3/thing4/amount2/time5 —— 那是【微信】的字段名，
                       支付宝侧会按 USER_TEMPLATE_LACK_KEYWORD 拒收。value 上限 50 字符。
                       _ALIPAY_SUBSCRIBE_FIELD_MAP 留空 = 不做改名，键名原样透传。
      page        = 点击消息跳转的小程序页，默认 pages/mine/mine
      dry_run     = True 时只构造 + 签名、不发网络请求（离线自检用）
      biz         = [S530] 可选。'subscribe_general' / 'subscribe_refund'；
                    template_id 为空时按它查库取模板ID。**必须显式传**（data 已无法反推 biz）。

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


def pick_order_mp_openid(order):
    """[S651] 从订单记录里取「确定属于某个已登记小程序账号」的 mp openid；取不到返回 ''。

    严格优先级：
      ① order['mp_openid'] —— 且能被 wx_config.account_id_by_openid() 解析出小程序账号
         （同时排除公众号前缀：oLhbm2/octN92 这类一律不当成小程序 openid）
      ② order['openid']    —— 同样条件（部分客户端把小程序 openid 放在 openid 字段里）
      ③ 都取不到 -> 返回 ''；调用方随即走【与改动前完全一样】的手机号/unionid 反查兜底

    为什么要有它：orders 表记录了下单那一刻的小程序 openid（近 2 天 98.7% 的订单都有）。
    部分通知调用点原来只按【手机号】反查收件 openid；当同一手机号名下存在两个小程序的
    openid（老 oXTD3x / 新 oQXFs3）时，反查可能命中【另一个】小程序的 openid ->
    通知发到用户没授权的那个小程序（收不到 / 跳转不对）。
    本函数保证：只要订单自己带着可用的小程序 openid，就【绝不】被反查结果覆盖。

    · order 可以是 dict / RealDictRow / None；非映射对象先尝试 dict(order)。
    · 任何异常一律返回 ''（绝不抛出）——异常时调用方行为与改动前完全一致。
    · 只读 wx_config 的账号前缀表，不写库、不联网、无副作用。
    """
    try:
        if not order:
            return ''
        try:
            _get = order.get
        except Exception:
            try:
                order = dict(order)
                _get = order.get
            except Exception:
                return ''
        try:
            import wx_config as _wc651
        except Exception:
            return ''
        _oa_pfx = ''
        try:
            _oa_pfx = str(_wc651.oa_openid_prefix() or '').strip()
        except Exception:
            _oa_pfx = ''
        for _key in ('mp_openid', 'openid'):
            try:
                _v = str(_get(_key) or '').strip()
            except Exception:
                _v = ''
            if not _v:
                continue
            if _oa_pfx and _v.startswith(_oa_pfx):
                continue
            try:
                if _wc651.account_id_by_openid(_v):
                    return _v
            except Exception:
                continue
        return ''
    except Exception:
        return ''


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
    # [S673-D-20260927] 只读断言（任务 D 防护，纯 logger，不改任何取值、不联网）：
    #   把"本次订阅消息的发送目标 openid + 它属于哪个已登记小程序账号 + 两个止血开关状态"
    #   记一条日志，用于在观察窗里证明【本次改动没有改变发送目标】。
    #   注意：本函数与 pick_order_mp_openid / _resolve_mp_openid / orders 身份字段
    #   在 S673 里【一行未动】；这里只新增读取性日志。
    try:
        _s673_aid = 0
        try:
            import wx_config as _wc673
            _s673_aid = _wc673.account_id_by_openid(openid or '') or 0
        except Exception:
            _s673_aid = 0
        try:
            _s673_guard = balance_id_audit_guard_enabled()
        except Exception:
            _s673_guard = False
        try:
            _s673_strict = balance_strict_identity_enabled()
        except Exception:
            _s673_strict = False
        logger.info('[S673][D] 订阅发送目标 openid=%s acct=%s phone=%s order_id=%s order_ids=%s '
                    'balance_id_audit_guard=%s balance_strict_identity=%s',
                    str(openid or '')[:10], _s673_aid, phone, order_id, order_ids,
                    _s673_guard, _s673_strict)
    except Exception:
        pass
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
        if _aid:
            logger.info('[subscribe_msg] openid 属于小程序(账号id=%s, 当前生效=%s)，走按账号发送路径', _aid, _eff_id)
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
                # [S700-20260927] 原来只排除"当前生效"那一个公众号前缀，别的代会被
                #   误当成小程序 openid 拿去发（微信 40003）。改成排除全部已登记公众号前缀。
                if _ub_row and _ub_row.get('mp_openid') and _ub_row['mp_openid'] not in ('', None) and not is_oa_openid_any(_ub_row['mp_openid']):
                    openid = _ub_row['mp_openid']
                if not openid:
                    _po_rows = phone_openid_rows(_cur, phone=phone, unionid=unionid or '')
                    if len(_po_rows) == 1 and _po_rows[0].get('mp_openid') and not is_oa_openid_any(_po_rows[0]['mp_openid']):
                        openid = _po_rows[0]['mp_openid']
                    elif len(_po_rows) > 1 and not unionid:
                        logger.warning(f'[subscribe_msg] 手机号绑定多个微信，缺少unionid，不猜测: phone={phone}')

                if not openid:
                    _cur.execute("""
                        SELECT mp_openid, unionid FROM phone_openids
                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL
                          AND NOT (mp_openid LIKE ANY(%s))
                        ORDER BY id ASC
                    """, (phone, [p + '%' for p in oa_prefixes_all()] or [oa_openid_prefix() + '%']))
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
        # [S700-20260927] 闸门从"只认当前生效公众号"放宽到"认全部已登记公众号"；
        #   替换候选从"只认当前生效小程序"放宽到"认全部已登记小程序"。
        #   并对【老口径本来放不进来、新口径才放进来的那批】加同人护栏：
        #   只有 unionid 能确认是同一个人时才替换，否则不换（宁可不发也不发错人）。
        #   老口径下本来就换成功的（S699 实测 130 笔）走 else 分支，行为一个字节没变。
        if openid and is_oa_openid_any(openid) and phone:
            _s700_newly = not str(openid).startswith(oa_openid_prefix())
            try:
                _conn3 = get_db()
                _cur3 = _conn3.cursor()
                _r3 = None
                _ub3 = find_user_balance_row(_cur3, phone=phone, unionid=unionid or '')
                if _ub3 and _ub3.get('mp_openid') and is_mp_openid_any(_ub3['mp_openid']):
                    _r3 = (_ub3['mp_openid'],)
                if not _r3:
                    _po3 = phone_openid_rows(_cur3, phone=phone, unionid=unionid or '')
                    if len(_po3) == 1 and _po3[0].get('mp_openid') and is_mp_openid_any(_po3[0]['mp_openid']):
                        _r3 = (_po3[0]['mp_openid'],)
                if _r3 and _r3[0] and _s700_newly and not _s700_same_person(_cur3, _r3[0], unionid):
                    logger.warning('[S700] 新口径才放进来的替换，unionid 确认不了同人 -> 不换(避免发错人): phone=%s', phone)
                    _r3 = None
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
            # [S231] 过渡期回退：换成新"账户余额"模板后，老用户只有旧"预付款退还"模板的授权 ->
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
        # [S682-20260927] 原来是 if/elif —— 【只用一把钥匙】，先按 user_id，
        #   认到了就完全忽略 unionid / 小程序卡 / 公众号卡。
        #   后果实例（生产实测）：测试号 A(18888889999) 名下 7 笔单的内部编号被写成
        #   118488，而 A 自己的编号是 94835 -> 按 94835 算出来 ¥0.00，
        #   按 unionid 算却是 ¥20.64。用户看到的正是"我明明有单，余额是 0"。
        # 现在改成：【通行证 / 小程序卡 / 公众号卡 三把卡级 ID 的 OR】，
        #   任何一把对上就算这笔单是本人的。
        #   · **不含手机号**（老板红线：手机号不做识别）；
        #   · **不把 user_id 放进 OR**（实测 31.89% 的订单编号指向不存在的用户、
        #     7,446 个编号跨多个 openid，放进去会把别人的单算给本人）；
        #   · 三把都空时才退回 user_id —— 保证原来靠编号能算到的人不会变少。
        cond = []
        params = []
        if ident['unionid']:
            cond.append('o.unionid = %s')
            params.append(ident['unionid'])
        if ident['mp_openid']:
            cond.append('o.mp_openid = %s')
            params.append(ident['mp_openid'])
        if openid:
            cond.append('o.openid = %s')
            params.append(openid)
        if not cond and ident['user_id']:
            cond.append('o.user_id = %s')
            params.append(ident['user_id'])
        if not cond:
            # 一把钥匙都没有 -> 匹配不到任何订单（返回 0），绝不放开成"全表"
            cond.append('o.openid = %s')
            params.append('')
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
            # [S273] 渠道隔离(方案B)：支付宝渠道付的预付款【不进】微信余额。
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
    """短信宝发送短信 (S106): 结束订单退预付款通知
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


# ============================================================
# [S684-20260927] 公众号菜单同步：让"存包/钱包/个人中心"三项都跳【当前使用的小程序】
# ============================================================
# 背景：公众号自定义菜单里"跳小程序"项的 appid 是写死在菜单里的。换小程序后，
#   老菜单还指着老小程序 -> 用户点菜单进不去/进错地方。
# 本函数把"菜单指向哪个小程序"改成【现读现用】（读 wx_config.mp_appid()），
#   所以以后切换小程序，只要重新调一次本函数，所有已关联的公众号菜单都会指向新号。
# 安全：① 逐个号先 menu/get 备份原菜单；② 逐号独立 try（一个号失败不影响其它号）；
#       ③ 微信侧"创建失败不会覆盖原菜单"，所以没关联的号菜单保持原样，无下行风险；
#       ④ 全程只调用微信官方接口，不写数据库。
def sync_oa_menus(oa_id=None, dry_run=False, backup=True, backup_dir='/home/ubuntu/oa_menu_backup'):
    """把公众号菜单同步成"三项都跳当前使用的小程序"。

    :param oa_id:  只处理某个公众号账号 id；None = 全部
    :param dry_run: True = 只看要做什么，不调用任何写接口
    :param backup:  是否先把原菜单备份到 backup_dir
    :return: (results, text)  results=[{id,name,result,detail}], text=给人看的一句话
    """
    import os as _os
    import json as _json
    import urllib.request as _ur
    import psycopg2 as _pg
    try:
        from psycopg2.extras import RealDictCursor as _RDC
    except Exception:
        _RDC = None
    _API = 'https://api.weixin.qq.com/cgi-bin/'
    _H5 = 'https://kelaiwei.top'

    def _call(path, token, body=None, method='GET'):
        _url = _API + path + '?access_token=' + token
        if method == 'GET' and body is None:
            _req = _ur.Request(_url)
        else:
            _req = _ur.Request(_url, data=_json.dumps(body or {}, ensure_ascii=False).encode('utf-8'),
                               headers={'Content-Type': 'application/json'})
        return _json.loads(_ur.urlopen(_req, timeout=15).read().decode())

    # 当前使用的小程序（现读现用）
    try:
        import wx_config as _wc
        _mp_appid = _wc.mp_appid() or ''
        try:
            _mine = _wc.get_config('mp_mine_path') or 'pages/mine/mine'
        except Exception:
            _mine = 'pages/mine/mine'
    except Exception as _e:
        return [], '公众号菜单同步：读不到当前小程序编号（%s），未处理' % _e
    if not _mp_appid:
        return [], '公众号菜单同步：当前小程序编号为空，未处理'

    _paths = [('存包', 'pages/index/index', _H5 + '/store'),
              ('钱包', 'pages/wallet/wallet', _H5 + '/static/user-h5.html?page=wallet'),
              ('个人中心', _mine, _H5 + '/static/user-h5.html')]

    # [S684 修正] helpers 里没有 DATABASE_URL，而且 get_db() 的游标本身就返回 dict 行
    #   （app 其它地方如 find_user_balance_row 就是直接 dict(row) 用的），
    #   所以这里统一走 get_db()，不要自己 psycopg2.connect。
    _conn = get_db()
    _cur = _conn.cursor()
    if oa_id:
        _cur.execute("SELECT id,name,appid,secret FROM wx_accounts WHERE acct_type='oa' AND id=%s", (oa_id,))
    else:
        _cur.execute("SELECT id,name,appid,secret FROM wx_accounts WHERE acct_type='oa' ORDER BY id")
    _rows = [dict(r) for r in _cur.fetchall()]
    _conn.close()
    _rows = [r for r in _rows if r.get('appid') and not str(r['appid']).startswith('REPLACE')]

    _results = []
    for _a in _rows:
        _r = {'id': _a['id'], 'name': _a['name'], 'result': '', 'detail': ''}
        try:
            _tk = get_access_token_for(_a['appid'], _a['secret'] or '')
        except Exception as _e:
            _r['result'] = 'token失败'; _r['detail'] = str(_e)[:120]
            _results.append(_r); continue
        if not _tk:
            _r['result'] = 'token失败'; _r['detail'] = '取 token 返回空'
            _results.append(_r); continue
        if not dry_run and backup:
            try:
                _old = _call('menu/get', _tk)
                _os.makedirs(backup_dir, exist_ok=True)
                _fn = _os.path.join(backup_dir, 'oa_menu_%s_%s.json'
                                    % (_a['id'], __import__('time').strftime('%Y%m%d_%H%M%S')))
                with open(_fn, 'w', encoding='utf-8') as _f:
                    _json.dump(_old, _f, ensure_ascii=False, indent=2)
                _r['detail'] = '备份 ' + _os.path.basename(_fn)
            except Exception as _e:
                _r['detail'] = '备份失败(继续) ' + str(_e)[:60]
        if dry_run:
            _r['result'] = 'dry-run'
            _results.append(_r); continue
        try:
            _res = _call('menu/create', _tk,
                         {"button": [{"type": "miniprogram", "name": _nm, "url": _u,
                                      "appid": _mp_appid, "pagepath": _pp}
                                     for _nm, _pp, _u in _paths]}, method='POST')
        except Exception as _e:
            _r['result'] = '异常'; _r['detail'] = str(_e)[:120]
            _results.append(_r); continue
        if _res.get('errcode') == 0:
            _r['result'] = 'ok'
        elif _res.get('errcode') == 45064:
            _r['result'] = '没关联小程序'
        else:
            _r['result'] = '失败%s' % _res.get('errcode')
            _r['detail'] = str(_res.get('errmsg') or '')[:80]
        _results.append(_r)

    _ok = [x for x in _results if x['result'] == 'ok']
    _bad = [x for x in _results if x['result'] not in ('ok', 'dry-run')]
    _txt = '公众号菜单已同步到小程序 %s：成功 %d 个' % (_mp_appid, len(_ok))
    if _bad:
        _txt += '；未成功 %d 个（' % len(_bad) + '、'.join(
            '%s:%s' % (x['name'], x['result']) for x in _bad) + '）'
    if dry_run:
        _txt = '[dry-run] ' + _txt
    return _results, _txt
