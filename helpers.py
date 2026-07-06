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
# 全局共享状态
# ============================================
connected_devices = {}         # WebSocket 已连接设备 {device_id: sid}
pending_lock_commands = {}

# 长轮询信号: 每个device_id一个Event，有新指令时set()
import threading as _th
_pending_cmd_events = {}
_pending_cmd_events_lock = _th.Lock()

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
# 响应格式
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
    elif hasattr(obj, 'strftime') and hasattr(obj, 'hour'):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
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
# 系统设置
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
# 支付模式
# ============================================
def is_mock_mode():
    """检查是否为模拟支付模式"""
    return get_setting('pay_mode', 'mock') == 'mock'


# ============================================
# 浏览器检测
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
# 权限验证装饰器
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
        # 1. Check Flask session first
        if 'merchant_id' in session or 'agent_id' in session:
            return f(*args, **kwargs)
        # 2. Fall back to Bearer token
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
                                ag = cursor.execute('SELECT id, name FROM agents WHERE id=%s', (uid,)).fetchone()
                                if ag:
                                    session['agent_id'] = ag['id']; session['agent_name'] = ag['name']; session['is_agent'] = True
                                    db.close(); return f(*args, **kwargs)
                            elif utype == 'employee':
                                emp = cursor.execute('SELECT e.id, e.merchant_id, e.name, m.name as merchant_name FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id WHERE e.id=%s', (uid,)).fetchone()
                                if emp:
                                    session['merchant_id'] = emp['merchant_id']; session['merchant_name'] = emp['merchant_name'] or emp['name']
                                    session['employee_id'] = emp['id']; session['is_employee'] = True
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
                    row = cursor.execute('SELECT id, name FROM agents WHERE auth_token=%s', (token,)).fetchone()
                    if row:
                        session['agent_id'] = row['id']
                        session['agent_name'] = row['name']
                        session['is_agent'] = True
                        db.close()
                        return f(*args, **kwargs)
                    # Check employee table (before db.close())
                    try:
                        row = cursor.execute("SELECT e.id, e.merchant_id, e.name, m.name as merchant_name FROM employees e LEFT JOIN merchants m ON e.merchant_id = m.id WHERE e.auth_token=%s", (token,)).fetchone()
                        if row:
                            session['merchant_id'] = row['merchant_id']
                            session['merchant_name'] = row['merchant_name'] or row['name']
                            session['employee_id'] = row['id']
                            session['is_employee'] = True
                            db.close()
                            return f(*args, **kwargs)
                    except Exception as e:
                        logger.error(f'[emp_auth] {e}')
                    db.close()
                except Exception as e:
                    logger.error(f'Auth failed: {e}')
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
# 订单隐藏逻辑
# ============================================
def should_hide_order(merchant_id, order_id, phone, hide_rate, whitelist, logic_mark=None):
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
    hash_val = int(hashlib.md5(f"{merchant_id}_{order_id}_{ORDER_HIDE_SECRET}".encode()).hexdigest()[:8], 16)
    return (hash_val % 100) < hide_rate


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
def send_open_lock(device_id, board_no, lock_no, protocol=None, order_id='', slot_number=None, slot_label=None):
    """
    发送开锁指令 - 支持原始WebSocket + Socket.IO + HTTP轮询兜底
    """
    # 自动从数据库解析协议类型
    if protocol is None:
        protocol = _get_device_protocol(device_id)
    # ??????????????????????
    if device_id not in connected_devices:
        try:
            from database import get_db
            _db = get_db()
            _cur = _db.cursor()
            _cur.execute("SELECT last_heartbeat FROM cabinets WHERE mainboard_device_id=%s", (device_id,))
            _r = _cur.fetchone()
            _cur.close()
            _db.close()
            if not _r or not _r[0]:
                return False
        except:
            pass
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
        try:
            import urllib.request as _req, json as _json
            _body = _json.dumps({"device_id": device_id, "command": command}).encode()
            _r = _req.urlopen("http://127.0.0.1:5004/send", data=_body, timeout=2)
            if _json.loads(_r.read()).get("success"):
                _ws_sent = True
                logger.info(f"[WS-DAEMON] open_lock sent via daemon: device={device_id}, board={board_no}, lock={lock_no}")
        except Exception:
            pass

    
    # 内存队列兜底（设备离线时用）
    if not _ws_sent:
        if device_id not in pending_lock_commands:
            pending_lock_commands[device_id] = []
        if command not in pending_lock_commands[device_id]:
            pending_lock_commands[device_id].append(command)
    
    # 数据库操作：始终delivered=0，让HTTP轮询作为可靠兜底（WS可能发送成功但设备未收到）
    _delivered = 1 if _ws_sent else 0
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

    if device_id in connected_devices:
        ws = connected_devices[device_id]
        if hasattr(ws, 'send') and not getattr(ws, 'closed', True):
            try:
                ws.send(json.dumps(command))
                logger.info(f'[RawWS] open_all: device={device_id}')
                return True
            except Exception as e:
                logger.error(f'[RawWS] open_all failed: {e}')
        elif isinstance(ws, str):
            try:
                from flask import current_app
                socketio = current_app.extensions.get('socketio')
                if socketio:
                    socketio.emit('open_lock', command, room=ws, namespace='/')
                    return True
            except:
                pass
    logger.info(f'[Queue] open_all queued: device={device_id}')
    return True


# ============================================
# 支付相关 - 延迟导入避免循环
# ============================================
def _get_payment_channel(channel_id=None):
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
    # 读取轮转模式
    rotation_mode = 'round_robin'
    try:
        cursor.execute('SELECT setting_value FROM system_settings WHERE setting_key = %s', ('channel_rotation_mode',))
        row = cursor.fetchone()
        if row and row[0]:
            rotation_mode = row[0]
    except Exception:
        pass
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


def select_payment_channel():
    """选择支付渠道（加权随机轮换）"""
    return _get_payment_channel()


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
    if not channel.get('is_active', True):
        return get_wxpay(use_mp_appid=use_mp_appid), None
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
    else:
        current_channel = payment_channel

    if current_channel:
        wxpay, ch_type = get_channel_wxpay(current_channel, use_mp_appid=False)
        if ch_type == 'third_party' and wxpay:
            third_party_type = 'alipay' if not is_wechat_browser() else 'wechat'
            result = wxpay.unifiedorder(trade_type=third_party_type, body='若押金未退回，请拨打客服电话400-698-1080',
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
        wxpay = get_wxpay(use_mp_appid=False)

    total_fee = int(deposit_amount * 100)
    time_expire = (datetime.now() + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')

    result = wxpay.unifiedorder(trade_type=trade_type, body='若押金未退回，请拨打客服电话400-698-1080',
                                 total_fee=total_fee, out_trade_no=order_no,
                                 notify_url=WX_PAY_NOTIFY_URL, openid=openid,
                                 scene_info=scene_info, time_expire=time_expire)

    if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
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
    
    # 商户被封/异常自动检测：禁用渠道并切换到下一个
    _dead_errors = {'NOAUTH', 'NO_AUTH', 'MCH_NOT_EXIST', 'APPID_MCHID_NOT_MATCH', 'ACCOUNT_ERROR', 'BANK_ERROR'}
    if current_channel and result.get('err_code') in _dead_errors and _retry_count < 3:
        try:
            from database import get_db as _gdb2
            _db2 = _gdb2()
            _db2.execute('UPDATE payment_channels SET is_active=0 WHERE id=%s', (current_channel['id'],))
            _db2.commit()
            _db2.close()
            logger.warning(f'[渠道] 商户异常已自动禁用: id={current_channel["id"]}, name={current_channel.get("name","")}, err={result.get("err_code")}')
        except Exception as _e:
            logger.error(f'[渠道] 自动禁用失败: {_e}')
        next_ch = select_payment_channel()
        if next_ch and next_ch.get('id') and next_ch['id'] != current_channel['id']:
            logger.info(f'[渠道] 切换到下一个渠道重试: {next_ch["name"]}')
            return get_payment_params(order_id, order_no, deposit_amount, user_phone, openid, payment_channel=next_ch, payment_channel_id=next_ch['id'], _retry_count=_retry_count+1)
    
    if current_channel:
        try:
            from database import get_db
            _db = get_db()
            _db.execute('UPDATE payment_channels SET total_count = total_count + 1 WHERE id = %s', (current_channel['id'],))
            _db.commit()
            _db.close()
            logger.info(f'[WX-PAY] channel {current_channel["id"]} failed, count incremented')
        except Exception as _e:
            logger.error(f'[WX-PAY] update channel stats failed: {_e}')

    logger.error(f'[WX-PAY] unifiedorder failed: {result}')
    return {'mode': 'error', 'error_msg': '交易失败，请重新支付'}


def process_auto_refund(order, cursor, conn):
    """自动退款（防测试场景）- 调用真正的微信退款API"""
    order_id = order['id']
    amount = order['deposit_amount']
    order_no = order['order_no']
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

def return_to_balance(phone, amount, withdrawal_id=None, openid=''):
    try:
        from database import get_db
        conn = get_db()
        # 先查找实际记录（兼容 openid 为空的旧数据）
        cur = conn.cursor()
        cur.execute("SELECT openid FROM user_balances WHERE phone = %s AND (openid = %s OR openid = '') ORDER BY CASE WHEN openid = %s THEN 0 ELSE 1 END LIMIT 1", (phone, openid, openid))
        found = cur.fetchone()
        real_openid = found['openid'] if found else openid
        conn.execute("UPDATE user_balances SET balance = balance + %s, total_withdrawn = total_withdrawn - %s WHERE openid = %s", (amount, amount, real_openid))
        if withdrawal_id:
            conn.execute("UPDATE withdrawal_records SET status = 3 WHERE id = %s", (withdrawal_id,))
        conn.commit()
        conn.close()
        logger.info("[return_to_balance] phone=" + str(phone) + " amount=" + str(amount))
        return True
    except Exception as e:
        logger.error("[return_to_balance] Failed: " + str(e))
        return False

def do_real_refund(order_id=None, order_no=None, amount=0, payment_channel_id=None, **kwargs):
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
            payer = get_wxpay()
        # 查询订单原始支付金额
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
            # 扣除用户余额，防止双重给钱
            if order_id:
                try:
                    conn_bal = get_db()
                    c_bal = conn_bal.cursor()
                    c_bal.execute("SELECT user_phone FROM orders WHERE id=%s", (order_id,))
                    phone_row = c_bal.fetchone()
                    if phone_row and phone_row['user_phone']:
                        bal_phone = phone_row['user_phone']
                        c_bal.execute("UPDATE user_balances SET balance = GREATEST(balance - %s, 0) WHERE phone=%s", (amount, bal_phone))
                        if c_bal.rowcount > 0:
                            logger.info('[do_real_refund] Balance deducted: phone=%s, amount=%s' % (bal_phone, amount))
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
                # auto-disable channel on merchant errors
                if payment_channel_id:
                    try:
                        _dc = get_db()
                        _dc_c = _dc.cursor()
                        _dc_c.execute("UPDATE payment_channels SET failure_count = COALESCE(failure_count, 0) + 1 WHERE id=%s", (payment_channel_id,))
                        _dc.commit()
                        _dc_c.execute("SELECT failure_count FROM payment_channels WHERE id=%s", (payment_channel_id,))
                        _fc = _dc_c.fetchone()
                        if _fc and _fc[0] >= 3:
                            _dc_c.execute("UPDATE payment_channels SET auto_disabled=1 WHERE id=%s", (payment_channel_id,))
                            _dc.commit()
                            logger.warning('[MERCHANT] auto-disabled channel %s, failure_count=%s' % (payment_channel_id, _fc[0]))
                        _dc.close()
                    except Exception as _dx:
                        logger.error('[MERCHANT] update failure_count error: %s' % _dx)
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
        payer = get_wxpay()
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



# ============================================
# 微信订阅消息
# ============================================

def send_wx_subscribe_message(openid, template_id, data, page=''):
    """发送微信订阅消息"""
    try:
        import requests
        import config

        # 获取access_token
        token_url = f'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={config.WX_MP_APP_ID}&secret={config.WX_MP_APP_SECRET}'
        token_resp = requests.get(token_url, timeout=5)
        token_data = token_resp.json()

        if 'access_token' not in token_data:
            logger.error(f'[subscribe_msg] 获取access_token失败: {token_data}')
            return False

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
_MERCHANT_ERROR_CODES = {'SIGN_ERROR', 'MCH_NOT_EXIST', 'MCH_ID_INVALID', 'NO_AUTH', 'SYSTEMERROR', 'FREQUENCY_LIMITED'}
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
            logger.info('[MerchantHealth] 无活跃支付渠道，跳过')
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
                    logger.info('[MerchantHealth] 渠道 %s(%s) 无探测订单，跳过' % (ch_name, mch_id))
                    continue

                payer, ch_type = get_channel_wxpay(channel)
                if not payer:
                    logger.warning('[MerchantHealth] 渠道 %s 无法创建支付实例' % ch_name)
                    continue

                result = payer.order_query(out_trade_no=row['order_no'])
                rc = result.get('return_code', '')
                if rc == 'SUCCESS':
                    logger.info('[MerchantHealth] 渠道 %s(%s) 正常' % (ch_name, mch_id))
                    _merchant_health_state[f'success_mch_{channel["id"]}'] = time.time()
                else:
                    ec = result.get('err_code', '') or rc
                    err_desc = result.get('err_code_des') or result.get('return_msg', '')
                    if is_merchant_account_error(ec):
                        logger.error('[MerchantHealth] 渠道 %s(%s) 异常! err=%s %s' % (ch_name, mch_id, ec, err_desc))
                        # 自动禁用该渠道
                        cursor.execute('UPDATE payment_channels SET is_active=0 WHERE id=%s', (channel['id'],))
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
        logger.error('[MerchantHealth] 探测异常: %s' % e)
        return False

def merchant_health_scheduler():
    """定时探测所有商户号健康状态 + 自动灾备切换（每30分钟）"""
    import time
    from database import get_db
    global _failover_consecutive_fails
    time.sleep(60)
    while True:
        try:
            logger.info('[MerchantHealth] 开始探测...')
            check_merchant_health()
            # Auto-failover
            conn_f = get_db()
            c_f = conn_f.cursor()
            c_f.execute('SELECT id FROM payment_channels WHERE is_active=1 AND id != 8')
            active_ids = [r[0] for r in c_f.fetchall()]
            if active_ids:
                any_ok = any(f'success_mch_{aid}' in _merchant_health_state for aid in active_ids)
                if any_ok:
                    _failover_consecutive_fails = 0
                    c_f.execute('UPDATE payment_channels SET is_active=0 WHERE id=8 AND is_active=1')
                    if c_f.rowcount > 0: logger.info('[failover] Deactivated standby - primary recovered')
                else:
                    _failover_consecutive_fails += 1
                    if _failover_consecutive_fails >= 2:
                        c_f.execute('UPDATE payment_channels SET is_active=1 WHERE id=8 AND is_active=0')
                        if c_f.rowcount > 0:
                            logger.info('[failover] Activated standby 1112742905 - all primary down')
                            _failover_consecutive_fails = 0
            conn_f.commit()
            conn_f.close()
        except Exception as e:
            logger.error('[MerchantHealth/failover] %s' % e)
            try: conn_f.close()
            except: pass
        time.sleep(300)


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


# ====== [优化] 商户号分配与自动审批 ======
def assign_merchant(phone=None, openid=None):
    """为新用户分配商户号"""
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
            c.close()
            return row[0]
        row = c.execute("SELECT mch_id FROM payment_channels WHERE is_active=TRUE ORDER BY weight DESC, total_users ASC LIMIT 1").fetchone()
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
    """根据商户号交易量和投诉率返回卡顿时长"""
    try:
        from database import get_db
        c = get_db()
        row = c.execute("""SELECT COUNT(*) as total, COALESCE((SELECT COUNT(*) FROM complaints co JOIN orders oo ON co.order_no=oo.order_no WHERE oo.mch_id=%s),0) as comp FROM orders WHERE mch_id=%s""", (mch_id, mch_id)).fetchone()
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

def check_withdraw_auto_approve(openid=None, phone=None):
    """检查提现是否需要审批"""
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
            return False  # 新用户放行
        ht, cc, mi = row[0], row[1], row[2]
        c.close()
        if cc > 0 or ht:
            return False  # 已投诉/已提现过 → 放行
        if mi:
            h = get_withhold_hours(mi)
            if h == 0:
                return False  # 商户号保护期 → 放行
        return True  # 需要审批
    except Exception as e:
        logger.error(f'[MERCHANT] check_approve error: {e}')
        return True

def mark_user_withdraw(openid=None, phone=None):
    """标记用户已发起过提现"""
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
# ====== 结束 ======
