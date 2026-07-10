"""
智能寄存柜系统 v2 - 模块化版本
主入口：注册所有Blueprints + 静态文件 + WebSocket + 全局异常处理
"""
import os
import logging
import sys
import json
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from werkzeug.middleware.proxy_fix import ProxyFix
from flask import Flask, send_from_directory, redirect, request, jsonify
from flask_socketio import SocketIO

from config import SECRET_KEY, DATABASE, DEBUG, HOST, PORT
import config
from database import init_db
from helpers import logger, connected_devices, json_response, merchant_health_scheduler


# ============================================
# 创建Flask应用
# ============================================
app = Flask(__name__, static_folder='static', static_url_path='/static')


@app.route('/h5/merchant/<path:subpath>')
def h5_merchant(subpath):
    return send_from_directory('static', 'merchant.html')

@app.route('/h5/merchant/open-logs')
def h5_merchant_devices():
    return send_from_directory('static', 'merchant-devices.html')

@app.route('/h5/merchant/open-logs')
def h5_merchant_open_logs():
    return send_from_directory('static', 'open-logs.html')

app.secret_key = SECRET_KEY
app.config['DATABASE'] = DATABASE
app.config['DEBUG'] = DEBUG

# ============================================
# WebSocket
# ============================================
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='gevent')
# V2 admin API
try:
    from routes.admin_v2 import bp as admin_v2_bp
    app.register_blueprint(admin_v2_bp, url_prefix='/api')
    logger.info('[注册] admin_v2 blueprint 加载成功')
except Exception as e:
    pass

# ========= 连接池泄漏防护：每次请求结束自动回收数据库连接 =========
@app.teardown_appcontext
def _auto_close_db_conns(exc):
    try:
        from database import close_all_request_conns
        close_all_request_conns()
    except Exception:
        pass

# ============================================
# 注册所有Blueprints（逐个try-except,一个失败不影响其他）
# ============================================
blueprints = []

# 导入Blueprints
try:
    from routes.user import bp as user_bp
    blueprints.append((user_bp, '/api'))
    logger.info('[注册] user blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载user blueprint失败: {e}')

try:
    from routes.admin import bp as admin_bp
    blueprints.append((admin_bp, '/api'))
    logger.info('[注册] admin blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载admin blueprint失败: {e}')

try:
    from routes.merchant import bp as merchant_bp
    blueprints.append((merchant_bp, '/api'))
    logger.info('[注册] merchant blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载merchant blueprint失败: {e}')

try:
    from routes.payment import bp as payment_bp
    blueprints.append((payment_bp, '/api'))
    logger.info('[注册] payment blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载payment blueprint失败: {e}')

try:
    from routes.stats import bp as stats_bp
    blueprints.append((stats_bp, '/api'))
    logger.info('[注册] stats blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载stats blueprint失败: {e}')

try:
    from routes.offline import bp as offline_bp
    blueprints.append((offline_bp, '/api'))
    logger.info('[注册] offline blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载offline blueprint失败: {e}')

try:
    from routes.webhook import bp as webhook_bp
    blueprints.append((webhook_bp, '/api'))
    logger.info('[注册] webhook blueprint 加载成功')

except Exception as e:
    logger.error(f'[注册] 加载webhook blueprint失败: {e}')

try:
    from routes.device import bp as device_bp
    blueprints.append((device_bp, '/api'))
    logger.info('[注册] device blueprint 加载成功')
except Exception as e:
    logger.error(f'[注册] 加载device blueprint失败: {e}')

# 注册所有Blueprint
for bp, prefix in blueprints:
    try:
        app.register_blueprint(bp, url_prefix=prefix)
        logger.info(f'[注册] 蓝图 {bp.name} 注册成功 (prefix={prefix})')
    except Exception as e:
        logger.error(f'[注册] 注册蓝图 {bp.name} 失败: {e}')

# ============================================

# 兼容微信支付投诉回调路径（商户1747572495配置的URL前缀不同）
@app.route('/api/admin_v2/wechat-complaint/notify', methods=['POST'])
def wechat_complaint_notify_alias():
    from routes.admin_v2 import wechat_complaint_notify
    return wechat_complaint_notify()

# 注册WebSocket事件处理器
# ============================================
try:
    from routes.websocket import register_websocket_handlers
    register_websocket_handlers(socketio)
    logger.info('[注册] WebSocket处理器注册成功')
except Exception as e:
    logger.error(f'[注册] WebSocket处理器注册失败: {e}')

try:
    from ws_middleware import fix_websocket
    fix_websocket(app)
    logger.info("[注册] WebSocket中间件注册成功")
    logger.info('[注册] 原始WebSocket /ws/ 端点注册成功')
except Exception as e:
    logger.error(f'[注册] 原始WebSocket注册失败: {e}')

# ============================================
# 初始化数据库
# ============================================
try:
    init_db()
    logger.info('[启动] 数据库初始化完成')
except Exception as e:
    logger.error(f'[启动] 数据库初始化失败: {e}')

# ============================================
# 后台定时任务：清理超时未付款订单（每60秒）
# ============================================
def _cleanup_expired_orders():
    import threading, sqlite3
    from config import DATABASE
    while True:
        try:
            threading.Event().wait(60)
            db = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
            cur = db.cursor()
            cur.execute("SELECT o.id, o.slot_id, o.order_no FROM orders o WHERE o.status IN (0,1) AND o.store_time < NOW()::timestamp - INTERVAL '1 minute'")
            expired = cur.fetchall()
            released = 0
            for order in expired:
                if order['slot_id']:
                    cur.execute('UPDATE cabinet_slots SET status = 1 WHERE id = %s AND status = 2', (order['slot_id'],))
                    if cur.rowcount > 0:
                        released += 1
                cur.execute('UPDATE orders SET status = 5 WHERE id = %s', (order['id'],))
            if expired:
                db.commit()
                logger.info(f'[超时清理] 清理{len(expired)}笔未付款订单,释放{released}个柜格')
            db.close()
        except Exception as e:
            logger.error(f'[超时清理] 异常: {e}')
        finally:
            try:
                db.close()
            except Exception:
                pass

import threading
t = threading.Thread(target=_cleanup_expired_orders, daemon=True)
t.start()
logger.info('[启动] 超时订单清理任务已启动(每60秒,threading)')

# ============================================
# 全局异常处理
# ============================================

@app.errorhandler(404)
def not_found(e):
    """404页面"""
    path = request.path
    logger.warning(f'[404] 未找到资源: {path}')
    return json_response(message='请求的资源不存在', code=404)


@app.errorhandler(500)
def server_error(e):
    """500服务器错误"""
    logger.error(f'[500] 服务器内部错误: {e}')
    return json_response(message='服务器内部错误', code=500)


@app.errorhandler(Exception)
def handle_exception(e):
    """通用异常捕获"""
    logger.error(f'[异常] 未捕获异常: {e}', exc_info=True)
    return json_response(message='服务器异常', code=500)


# ============================================
# 静态文件/页面路由
@app.route("/favicon.ico")
def favicon():
    return "", 204

# ============================================

@app.route('/')
def index():
    """首页"""
    resp = send_from_directory('static', 'deposit.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/admin')
def admin():
    """管理后台"""
    return send_from_directory('static', 'admin-v2.html')


@app.route('/admin-v2')
@app.route('/admin-v2/')
@app.route('/admin-v2/<path:subpath>')
def admin_v2(subpath=None):
    resp = send_from_directory('static', 'admin-v2.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp



@app.route('/admin-v2-debug')
def admin_v2_debug():
    return send_from_directory('static', 'admin-v2-debug.html')

@app.route('/profile')
def profile():
    return send_from_directory('static', 'profile.html')



@app.route('/static/deposit.html')
def deposit_html_redirect():
    if request.args.get('page') == 'profile':
        qs = request.query_string.decode() if request.query_string else ''
        return redirect('/static/h5/index.html' + ('?' + qs if qs else ''))
    return send_from_directory('static', 'deposit.html')
@app.route('/user-center')
def user_center():
    return send_from_directory('static', 'user-h5.html')


@app.route('/merchant')
def merchant():
    """商户端"""
    return send_from_directory('static', 'merchant.html')


@app.route('/screen')
def screen():
    """柜体屏幕端 - 带缓存解决方案"""
    import time
    from flask import redirect, request as _req
    if not _req.args.get('sc_t'):
        return redirect('/screen?sc_t=' + str(int(__import__('time').time())))
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'screen.html')
    try:
        with open(tpl_path, 'r', encoding='utf-8') as f:
            html = f.read()
        from flask import make_response as _mr
        resp = _mr(html)
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        resp.headers['ETag'] = ''
        return resp
    except:
        return '页面加载失败', 500


@app.route('/store', strict_slashes=False)
def store_page():
    """存包页面 - 服务端渲染柜子信息"""
    _v = request.args.get('v', '')
    if not _v:
        import urllib.parse as _up
        _qs = dict(request.args)
        _qs['v'] = str(int(__import__('time').time()))
        return redirect('/store?' + _up.urlencode(_qs), code=302)
    device = request.args.get('device', '')
    openid = request.args.get('openid', '')
    if not openid and 'MicroMessenger' in request.headers.get('User-Agent', ''):
        import urllib.parse
        current_url = request.url.replace('http://', 'https://')
        return redirect(f'/api/wx/oauth?redirect_uri={urllib.parse.quote(current_url)}')
    _ver = str(int(__import__('time').time()))
    
    # 服务端查询柜子信息,直接渲染到页面
    _ssr = {"site_name":"","site_addr":"","deposit_amount":0,"charge_mode":"deposit","allow_h5_to_mp":0,"mp_appid":"","mp_path":"","is_online":True}
    _cabinet_id = request.args.get('cabinet_id', '')
    try:
        import sqlite3 as _sq
        conn = _sq.connect(DATABASE)
        conn.row_factory = _sq.Row
        c = conn.cursor()
        if device:
            c.execute("SELECT c.id,c.name,c.deposit_amount,c.charge_mode,c.per_use_price,c.mainboard_device_id,c.last_heartbeat,l.name as loc_name,l.address as loc_addr,l.allow_h5_to_mp FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.mainboard_device_id=%s", (device,))
        elif _cabinet_id:
            c.execute("SELECT c.id,c.name,c.deposit_amount,c.charge_mode,c.per_use_price,c.mainboard_device_id,c.last_heartbeat,l.name as loc_name,l.address as loc_addr,l.allow_h5_to_mp FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.id=%s", (_cabinet_id,))
        else:
            c.execute("SELECT c.id,c.name,c.deposit_amount,c.charge_mode,c.per_use_price,c.mainboard_device_id,c.last_heartbeat,l.name as loc_name,l.address as loc_addr,l.allow_h5_to_mp FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.id=8")
        row = c.fetchone()
        if row:
            _ssr["site_name"] = row["loc_name"] or row["name"] or ""
            _ssr["site_addr"] = row["loc_addr"] or ""
            _ssr["deposit_amount"] = row["deposit_amount"] or 0
            _ssr["charge_mode"] = row["charge_mode"] or "deposit"
            _ssr["per_use_price"] = row["per_use_price"] or 0
            _ssr["allow_h5_to_mp"] = row["allow_h5_to_mp"] or 0
            if row["allow_h5_to_mp"]:
                import config as _cfg
                _ssr["mp_appid"] = _cfg.WX_MP_APP_ID
                _ssr["mp_path"] = "pages/subscribe/subscribe"
#            # 每次有人扫码加载存包页面就刷新心跳
#            try:
#                _up = conn.cursor()
# _up.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE id=%s", (row["id"],))
            # 计算设备在线状态
            if row.get("last_heartbeat"):
                try:
                    from datetime import datetime
                    hb = datetime.strptime(str(row["last_heartbeat"])[:19], "%Y-%m-%d %H:%M:%S")
                    _ssr["is_online"] = (datetime.now() - hb).total_seconds() < 300
                except:
                    _ssr["is_online"] = False
            else:
                _ssr["is_online"] = False
            # SSR时显示第一个可用格子
            if _cabinet_id:
                try:
                    c2 = conn.cursor()
                    c2.execute("SELECT slot_label FROM cabinet_slots WHERE cabinet_id=%s AND status=1 ORDER BY slot_number LIMIT 1", (_cabinet_id,))
                    r2 = c2.fetchone()
                    if r2:
                        _ssr["door_number"] = str(r2["slot_label"])
                except:
                    pass
            if not _cabinet_id and row["id"]:
                _cabinet_id = str(row["id"])
        conn.close()
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f'store SSR error: {e}')
    
    import json as _json
    _ssr_json = _json.dumps(_ssr, ensure_ascii=False)
    
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'deposit.html')
    try:
        with open(tpl_path, 'r', encoding='utf-8') as f:
            html = f.read().replace("{device}", device).replace("{openid}", openid).replace("{_ver}", _ver).replace("{ssr_cabinet}", _ssr_json).replace("{cabinet_id}", _cabinet_id).replace("{deposit_amount}", str(int(_ssr["deposit_amount"]) if _ssr["deposit_amount"] and _ssr["deposit_amount"] == int(_ssr["deposit_amount"]) else _ssr["deposit_amount"]))
        from flask import make_response as _mr
        resp = _mr(html)
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp
    except:
        return "页面加载失败", 500


@app.route('/retrieve')
def retrieve_page():
    """取包页面"""
    device = request.args.get('device', '')
    cabinet_id = ''
    if device:
        import sqlite3 as _sq
        _db = _sq.connect(DATABASE)
        _db.row_factory = _sq.Row
        _cur = _db.cursor()
        _cur.execute('SELECT id FROM cabinets WHERE mainboard_device_id=%s', (device,))
        _row = _cur.fetchone()
        _db.close()
        if _row:
            cabinet_id = str(_row['id'])
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>取包</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;min-height:100vh;color:#333}
.page{padding:20px}.header{text-align:center;font-size:20px;font-weight:700;padding:16px 0;background:#fff;border-bottom:1px solid #eee}
.form-group{margin:24px 0}.form-group label{display:block;font-size:16px;color:#666;margin-bottom:8px}
.form-group input{width:100%;height:52px;border:1px solid #ddd;border-radius:8px;padding:0 16px;font-size:18px;outline:none}
.form-group input:focus{border-color:#4caf50}
.btn-submit{width:100%;height:52px;background:#4caf50;color:#fff;font-size:18px;font-weight:700;border:none;border-radius:8px;margin-top:36px;cursor:pointer}
.result{text-align:center;padding:40px 20px;display:none}
.result-icon{width:64px;height:64px;background:#4caf50;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:36px;margin-bottom:20px}
.result-title{font-size:24px;font-weight:700;margin-bottom:16px}
.result-info{font-size:18px;color:#666;margin-bottom:8px}
</style>
</head>
<body>
<div class="page" id="inputPage">
<div class="header">取包</div>
<div class="form-group"><label>手机号</label><input type="tel" id="phone" maxlength="11" placeholder="请输入存包时的手机号"></div>
<div class="form-group"><label>取包密码</label><input type="tel" id="pwd" maxlength="4" placeholder="请输入4位取包密码"></div>
<button class="btn-submit" onclick="doRetrieve()">取包</button>
</div>
<div class="result" id="resultPage">
<div class="result-icon">&#10003;</div>
<div class="result-title">取包成功</div>
<div class="result-info" id="resultSlot"></div>
</div>
<script>
var cabinetId='""" + cabinet_id + """';
function doRetrieve(){
  var phone=document.getElementById('phone').value.trim();
  var pwd=document.getElementById('pwd').value.trim();
  if(!phone||!pwd){alert('请输入手机号和密码');return;}
  fetch('/api/retrieve/verify',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cabinet_id:parseInt(cabinetId),phone:phone,access_code:pwd})
  }).then(function(r){return r.json();}).then(function(d){
    if(d.code==200){
      fetch('/api/retrieve/confirm',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({order_id:d.data.order_id,action:'end'})
      });
      document.getElementById('inputPage').style.display='none';
      document.getElementById('resultPage').style.display='block';
      document.getElementById('resultSlot').textContent='柜门号：'+(d.data.compartment_number||'--')+'号';
    }else{alert(d.message||'取包失败');}
  }).catch(function(e){alert('网络错误');});
}
</script>
</body>
</html>"""

@app.route('/scan')
def scan_qr():
    device = request.args.get('device', '')
    if not device:
        return jsonify({'code': 400, 'message': 'missing device'}), 400
    try:
        import sqlite3
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        cur = db.cursor()
        cur.execute('SELECT * FROM cabinets WHERE mainboard_device_id=%s', (device,))
        cabinet = cur.fetchone()
        db.close()
        if not cabinet:
            return jsonify({'code': 404, 'message': 'device not found'}), 404
        store = cabinet['name'] or ''
        # 检查设备在线状态
        from helpers import connected_devices
        device_id = cabinet['mainboard_device_id'] or ''
        is_online = device_id and device_id in connected_devices
        if not is_online:
            return '<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content="width=device-width,initial-scale=1.0"><title>设备离线</title><style>body{font-family:sans-serif;margin:0;padding:0;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}.card{background:#fff;border-radius:12px;padding:40px 30px;max-width:360px;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,.1)}.icon{font-size:64px;margin-bottom:16px}.card h2{color:#f56c6c;margin-bottom:8px;font-size:20px}.card p{color:#999;font-size:14px;margin-top:8px;line-height:1.6}</style></head><body><div class=card><div class=icon>📡</div><h2>设备未在线</h2><p>该寄存柜当前处于离线状态<br>暂时无法使用,请稍后再试</p></div></body></html>', 200
        html = '<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content=width=device-width,initial-scale=1.0><title>智能寄存柜</title><style>body{font-family:sans-serif;margin:0;padding:20px;background:#f0f2f5;text-align:center}.card{background:#fff;border-radius:12px;padding:30px;max-width:400px;margin:40px auto;box-shadow:0 4px 20px rgba(0,0,0,0.1)}h2{color:#333;margin-bottom:10px}p{color:#666;font-size:14px}.btn{display:inline-block;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:14px 40px;border-radius:8px;text-decoration:none;font-size:16px;margin-top:20px}.info{margin:15px 0;color:#999;font-size:13px}</style></head><body><div class=card><h2>智能寄存柜</h2>'
        # Build full page with store + retrieve buttons
        html = '''<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>智能寄存柜</title><style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:-apple-system,sans-serif;background:#f0f2f5;min-height:100vh}
    .header{background:linear-gradient(135deg,#1A73E8,#0d47a1);color:#fff;padding:24px 20px;text-align:center}
    .header h1{font-size:22px;margin-bottom:4px}
    .header .sub{font-size:13px;opacity:0.8}
    .info{background:#fff;margin:12px;border-radius:12px;padding:16px}
    .info-row{display:flex;align-items:center;padding:8px 0;font-size:14px;color:#333;border-bottom:1px solid #f0f0f0}
    .info-row:last-child{border:none}
    .info-row .label{color:#999;width:80px;flex-shrink:0}
    .status{display:flex;gap:12px;margin:12px}
    .status-card{flex:1;background:#fff;border-radius:12px;padding:16px;text-align:center}
    .status-card .num{font-size:36px;font-weight:bold;color:#1A73E8}
    .status-card .num.green{color:#34A853}
    .status-card .txt{font-size:13px;color:#999;margin-top:4px}
    .actions{margin:16px 12px;display:flex;gap:12px}
    .actions a{flex:1;display:block;padding:18px;border-radius:12px;text-align:center;color:#fff;font-size:20px;font-weight:bold;text-decoration:none}
    .btn-store{background:linear-gradient(135deg,#1A73E8,#0d47a1)}
    .btn-retrieve{background:linear-gradient(135deg,#34A853,#1b5e20)}
    .footer{text-align:center;padding:20px;font-size:12px;color:#bbb}
    </style></head><body>
    <div class="header"><h1>''' + store + '''</h1><div class="sub">智能寄存柜 · 安全便捷</div></div>
    <div class="info">
    <div class="info-row"><span class="label">设备号</span><span>''' + device + '''</span></div>
    <div class="info-row"><span class="label">网点</span><span>''' + store + '''</span></div>
    </div>
    <div class="actions">
    <a class="btn-store" href="/store?device=''' + device + '''">存 物</a>
    <a class="btn-retrieve" href="/retrieve?device=''' + device + '''">取 物</a>
    </div>
    <div class="footer">locker.cqdyxl.com</div>
    </body></html>'''
        return html
    finally:
        try:
            db.close()
        except Exception:
            pass


@app.route('/locker')
def locker_page():
    """柜机交互页面 - 重定向到/store新版SPA"""
    device = request.args.get('device', '')
    if not device:
        return "缺少设备编号", 400
    openid = request.args.get('openid', '')
    if not openid and 'MicroMessenger' in request.headers.get('User-Agent', ''):
        import urllib.parse
        current_url = request.url.replace('http://', 'https://')
        return redirect(f'/api/wx/oauth?redirect_uri={urllib.parse.quote(current_url)}')
    # 新版SPA包含首页+存包+取包,直接跳转
    import time
    return redirect(f"/store?device={device}&openid={openid}&_t={int(time.time())}")
@app.route('/static/<path:filename>')
def static_files(filename):
    """静态文件"""
    return send_from_directory('static', filename)


@app.route('/templates/<path:filename>')
def template_files(filename):
    """模板文件"""
    return send_from_directory('templates', filename)


# ============================================
# WSGI入口 (gunicorn 用)
# ============================================
# gunicorn 启动: gunicorn -k gevent -w 1 -b 127.0.0.1:5001 app:app

# ============================================
# 主启动
# ============================================

# 注册竞品兼容API（新增）

    logger.error(f'[注册] admin_v2失败: {e}')

from routes.register_apis import register_new_api_blueprints
register_new_api_blueprints(app)

# 仅在 gunicorn master 进程中启动 WebSocket 服务（避免多 worker 抢端口）
import os as _os
if not _os.environ.get('GUNICORN_WORKER_ID'):
    try:
        from ws_server import start_ws_server
        _ws_thread = threading.Thread(target=start_ws_server, daemon=True)
        _ws_thread.start()
        logging.getLogger(__name__).info("[启动] WebSocket 服务线程已启动（master进程）")
    except Exception as _e:
        logging.getLogger(__name__).warning(f"[启动] WebSocket 服务线程启动失败: {_e}")

logger.info('[注册] 竞品兼容API注册成功')



# ---------- 微信JS-SDK签名API ----------
@app.route('/api/wx/jsapi-signature', methods=['GET'])
def wx_jsapi_signature():
    """为H5页面提供微信JS-SDK签名,用于wx-open-launch-weapp开放标签"""
    try:
        url = request.args.get('url', '')
        if not url:
            return jsonify({'code': -1, 'msg': '缺少url参数'})
        
        import hashlib, time
        from config import WX_APP_ID, WX_APP_SECRET
        
        # 获取access_token
        token_key = 'wx_jsapi_access_token'
        token_data = getattr(app, '_wx_jsapi_token', None)
        if not token_data or time.time() - token_data.get('ts', 0) > 7000:
            import urllib.request
            token_url = f'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={WX_APP_ID}&secret={WX_APP_SECRET}'
            with urllib.request.urlopen(token_url, timeout=10) as resp:
                token_json = json.loads(resp.read().decode())
            access_token = token_json.get('access_token', '')
            if not access_token:
                return jsonify({'code': -1, 'msg': '获取access_token失败: ' + str(token_json)})
            app._wx_jsapi_token = {'token': access_token, 'ts': time.time()}
        else:
            access_token = token_data['token']
        
        # 获取jsapi_ticket
        ticket_key = 'wx_jsapi_ticket'
        ticket_data = getattr(app, '_wx_jsapi_ticket', None)
        if not ticket_data or time.time() - ticket_data.get('ts', 0) > 7000:
            import urllib.request
            ticket_url = f'https://api.weixin.qq.com/cgi-bin/ticket/getticket?access_token={access_token}&type=jsapi'
            with urllib.request.urlopen(ticket_url, timeout=10) as resp:
                ticket_json = json.loads(resp.read().decode())
            jsapi_ticket = ticket_json.get('ticket', '')
            if not jsapi_ticket:
                return jsonify({'code': -1, 'msg': '获取jsapi_ticket失败: ' + str(ticket_json)})
            app._wx_jsapi_ticket = {'ticket': jsapi_ticket, 'ts': time.time()}
        else:
            jsapi_ticket = ticket_data['ticket']
        
        # 生成签名
        nonce_str = hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
        timestamp = str(int(__import__('time').time()))
        sign_str = f'jsapi_ticket={jsapi_ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={url}'
        signature = hashlib.sha1(sign_str.encode()).hexdigest()
        
        return jsonify({
            'code': 200,
            'data': {
                'appId': WX_APP_ID,
                'timestamp': timestamp,
                'nonceStr': nonce_str,
                'signature': signature
            }
        })
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})


@app.route('/api/wx/generate-scheme', methods=['POST'])
def wx_generate_scheme():
    try:
        import json as _json, urllib.request as _urllib, time as _time, logging as _logging
        from config import WX_MP_APP_ID as _appid, WX_MP_APP_SECRET as _secret
        try:
            _raw = request.get_json(force=True, silent=True) or {}
        except:
            _raw = {}
        path = _raw.get('path', '/pages/deposit/deposit')
        if path.startswith(chr(47)):
            path = path[1:]
        query = _raw.get('query', '')

        _tk_key = 'wx_scheme_token'
        _td = getattr(app, _tk_key, None)
        if _td and _time.time() - _td['ts'] < 7000:
            _at = _td['token']
        else:
            _tu = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=' + _appid + '&secret=' + _secret
            _tr = _urllib.urlopen(_tu, timeout=5)
            _tj = _json.loads(_tr.read())
            _at = _tj.get('access_token', '')
            if not _at:
                return jsonify({'code': -1, 'msg': 'token failed'})
            setattr(app, _tk_key, {'token': _at, 'ts': _time.time()})

        if '%s' in path and not query:
            query = path.split('%s', 1)[1]
            path = path.split('%s', 1)[0]
        _jw = {'path': path, 'env_version': 'release'}
        if query:
            _jw['query'] = query
        _bd = _json.dumps({'jump_wxa': _jw, 'is_expire': True, 'expire_type': 1, 'expire_interval': 30})

        _su = 'https://api.weixin.qq.com/wxa/generatescheme?access_token=' + _at
        _req = _urllib.Request(_su, data=_bd.encode(), headers={'Content-Type': 'application/json'})
        _sr = _urllib.urlopen(_req, timeout=5)
        _sj = _json.loads(_sr.read())

        if _sj.get('errcode') == 0 and (_sj.get('scheme') or _sj.get('openlink')):
            return jsonify({'code': 200, 'data': {'scheme': _sj.get('scheme') or _sj.get('openlink', '')}})
        return jsonify({'code': -1, 'msg': str(_sj)})
    except Exception as e:
        _logging.getLogger().error('[scheme] ' + str(e))
        return jsonify({'code': -1, 'msg': str(e)}), 500

@app.route('/api/app/version', methods=['GET'])
@app.route('/api/app/version', methods=['GET'])
def api_app_version():
    from config import LATEST_VERSION_CODE, LATEST_VERSION_NAME, APK_DOWNLOAD_URL
    return jsonify({
        'data': {
            'version_code': LATEST_VERSION_CODE,
            'version_name': LATEST_VERSION_NAME,
            'download_url': APK_DOWNLOAD_URL,
        }
    })




@app.route('/api/order/<int:order_id>/store/end', methods=['POST'])
def store_end_app(order_id):
    import sys as _sys, traceback
    _sys.path.insert(0, "/home/ubuntu/smart-locker")
    try:
        from database import get_db
        data = request.get_json(force=True) or {}
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"code": -1, "msg": "order not found"})
        order_dict = dict(row)
        if order_dict.get("status") != 2:
            conn.close()
            return jsonify({"code": -1, "msg": "order status invalid"})
        slot_id = order_dict.get("slot_id")
        if slot_id:
            c.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s", (slot_id,))
        c.execute("UPDATE orders SET status=3, retrieve_time=CURRENT_TIMESTAMP WHERE id=%s", (order_id,))
        conn.commit()
        conn.close()
        return jsonify({"code": 0, "msg": "success", "data": {"order_id": order_id}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": -1, "msg": "end order failed: " + str(e)})

@app.route('/api/order/<int:order_id>/minimal', methods=['GET'])
def get_order_minimal(order_id):
    """Return minimal order info for mini program display"""
    import sqlite3
    try:
        conn = sqlite3.connect("/home/ubuntu/smart-locker/locker.db")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT o.user_phone, o.access_code FROM orders o WHERE o.id=%s", (order_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return jsonify({"code": 200, "data": {"phone": row["user_phone"], "code": row["access_code"]}})
        return jsonify({"code": 404, "message": "order not found"})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route("/push-update", methods=["POST"])
def push_update():
    """推送更新到指定设备"""
    from helpers import connected_devices, logger
    import json
    import gevent
    try:
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id", "")
        if not device_id:
            return jsonify({"code": -1, "msg": "缺少device_id"})
        from config import LATEST_VERSION_CODE, LATEST_VERSION_NAME, APK_DOWNLOAD_URL
        if device_id in connected_devices:
            ws = connected_devices[device_id]
            msg = json.dumps({"type": "force_update", "device_id": device_id, "version_code": LATEST_VERSION_CODE, "version_name": LATEST_VERSION_NAME, "download_url": APK_DOWNLOAD_URL, "force": True})
            try:
                with gevent.Timeout(5):
                    ws.send(msg)
                logger.info(f"[Push] force_update sent to {device_id} v{LATEST_VERSION_NAME}")
                return jsonify({"code": 200, "msg": "SENT"})
            except Exception as e:
                logger.warning(f"[Push] ws.send failed for {device_id}, stale connection cleaned: {e}")
                try:
                    del connected_devices[device_id]
                except KeyError:
                    pass
        try:
            import sqlite3
            conn_db = sqlite3.connect(DATABASE)
            cur_db = conn_db.cursor()
            cur_db.execute("SELECT last_heartbeat FROM cabinets WHERE mainboard_device_id=%s", (device_id,))
            row = cur_db.fetchone()
            conn_db.close()
            if row and row[0]:
                cmd_obj = {"type": "force_update", "version_code": LATEST_VERSION_CODE, "version_name": LATEST_VERSION_NAME, "download_url": APK_DOWNLOAD_URL, "force": True}
                conn2 = sqlite3.connect(DATABASE)
                conn2.execute("INSERT INTO pending_lock_cmds (cabinet_id, command, status) VALUES (%s,%s,%s)", (device_id, json.dumps(cmd_obj), "pending"))
                conn2.commit()
                conn2.close()
                logger.info(f"[Push] force_update queued via pending_lock_cmds for {device_id} v{LATEST_VERSION_NAME}")
                return jsonify({"code": 200, "msg": "QUEUED"})
        except Exception as he:
            logger.error(f"[Push] queue fallback error: {he}")
        finally:
            try:
                conn_db.close()
            except Exception:
                pass
        return jsonify({"code": -1, "msg": "设备不在线"})
    except Exception as e:
        logger.error(f"[Push] error: {e}")
        return jsonify({"code": -1, "msg": str(e)})

def app_version():
    import sqlite3
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        cur = db.execute("SELECT * FROM apk_version ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        db.close()
        if row:
            return jsonify({
                'code': 200,
                'data': {
                    'version_code': row['version_code'],
                    'version_name': row['version_name'],
                    'download_url': row['download_url'],
                    'update_desc': row['update_desc'] or ''
                }
            })
        else:
            return jsonify({'code': -1, 'msg': 'no version'})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})
    finally:
        try:
            db.close()
        except Exception:
            pass



# ============================================
# 定时任务：余额超时隐藏（每小时执行）
# ============================================
def _balance_hide_scheduler():
    import time
    while True:
        time.sleep(3600)  # 每小时执行一次
        try:
            import sqlite3
            from config import DATABASE
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT id, balance_hide_days FROM locations WHERE balance_hide_enabled = 1 AND balance_hide_days > 0")
            locations = c.fetchall()
            total_hidden = 0
            for loc in locations:
                days = loc['balance_hide_days']
                c.execute(
                    "UPDATE user_balance_details SET status = 'pending' "
                    "WHERE status = 'available' "
                    "AND order_id IN ("
                    "  SELECT o.id FROM orders o"
                    "  JOIN cabinets cab ON o.cabinet_id = cab.id"
                    "  WHERE cab.location_id = %s"
                    ") AND source_time < NOW() - INTERVAL '%s days'"
                    % (loc['id'], days)
                )
                hidden = c.rowcount
                if hidden > 0:
                    total_hidden += hidden
                    logger.info('[余额隐藏] Location %s: 隐藏 %s 条超 %s 天余额明细' % (loc['id'], hidden, days))
            conn.commit()
            conn.close()
            if total_hidden > 0:
                logger.info('[余额隐藏] 本次共隐藏 %d 条余额明细' % total_hidden)
        except Exception as e:
            logger.error('[余额隐藏] 异常: %s' % e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

_balance_hide_thread = threading.Thread(target=_balance_hide_scheduler, daemon=True)
_balance_hide_thread.start()
logger.info('[启动] 余额超时隐藏任务已启动(每小时)')

# ============================================
# 定时任务：自动清柜（每分钟检查，连接PostgreSQL）
# ============================================
def _auto_clear_cabinet_scheduler():
    import time
    while True:
        time.sleep(60)
        try:
            import psycopg2
            import psycopg2.extras
            from datetime import datetime, timedelta
            from config import DATABASE_URL
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            c.execute("SELECT id, clear_box_time, clear_box_cycle, last_clear_date FROM locations WHERE enable_clear_box = 1 AND clear_box_time IS NOT NULL")
            locations = c.fetchall()

            for loc in locations:
                loc_id = loc["id"]
                clear_time = loc["clear_box_time"] or "03:00"
                cycle = loc["clear_box_cycle"] or 1
                last_clear = str(loc["last_clear_date"] or "")

                # 【修复1】时间窗口：只在 clear_time ~ clear_time+1小时 内执行
                try:
                    clear_h, clear_m = map(int, clear_time.split(":"))
                except:
                    clear_h, clear_m = 3, 0
                clear_start = now.replace(hour=clear_h, minute=clear_m, second=0, microsecond=0)
                clear_end = clear_start + timedelta(hours=1)
                if not (clear_start <= now < clear_end):
                    continue

                # 【修复5】用数据库字段代替内存变量
                if last_clear == today_str:
                    continue
                if last_clear:
                    try:
                        last_dt = datetime.strptime(last_clear, "%Y-%m-%d").date()
                        today_dt = now.date()
                        if (today_dt - last_dt).days < cycle:
                            continue
                    except:
                        pass

                # 【修复4】每个location独立try/except
                try:
                    cutoff_time = now - timedelta(days=cycle)

                    c.execute("""
                        SELECT o.id, o.slot_id, o.user_phone, o.deposit_amount, o.openid, o.unionid
                        FROM orders o
                        JOIN cabinets c ON o.cabinet_id = c.id
                        WHERE c.location_id = %s AND o.status = 2 AND o.store_time < %s
                    """, (loc_id, cutoff_time))
                    active_orders = c.fetchall()

                    total_ended = 0
                    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

                    for o in active_orders:
                        c.execute("UPDATE orders SET status=3, retrieve_time=%s, pickup_time=%s, updated_at=%s, refund_mark=1 WHERE id=%s",
                                  (now_str, now_str, now_str, o["id"]))

                        if o["slot_id"]:
                            c.execute("UPDATE cabinet_slots SET status=0 WHERE id=%s", (o["slot_id"],))

                        deposit_amount = float(o["deposit_amount"] or 0)
                        if deposit_amount > 0 and o["user_phone"]:
                            c.execute("SELECT id FROM user_balances WHERE phone=%s", (o["user_phone"],))
                            ub = c.fetchone()
                            u_openid = o["openid"] or ""
                            u_unionid = o["unionid"] or ""
                            if ub:
                                c.execute("UPDATE user_balances SET balance=balance+%s, total_deposited=total_deposited+%s, openid=COALESCE(NULLIF(openid,''),%s), unionid=COALESCE(NULLIF(unionid,''),%s) WHERE phone=%s",
                                          (deposit_amount, deposit_amount, u_openid, u_unionid, o["user_phone"]))
                            else:
                                c.execute("INSERT INTO user_balances (phone, openid, unionid, balance, total_deposited, total_withdrawn, first_use_time) VALUES (%s,%s,%s,%s,%s,0,NOW())",
                                          (o["user_phone"], u_openid, u_unionid, deposit_amount, deposit_amount))
                            c.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s,%s,%s,'available') ON CONFLICT (order_id) DO NOTHING",
                                      (o["user_phone"], o["id"], deposit_amount))

                        if o.get("openid"):
                            try:
                                from helpers import send_wx_subscribe_message
                                sub_data = {
                                    "amount6": {"value": "¥{:.2f}".format(deposit_amount)},
                                    "time4": {"value": now_str},
                                    "thing7": {"value": "已退还至小程序用户钱包"},
                                    "thing2": {"value": "请自行点击此通知消息跳转\u201c我的钱包\u201d提现"}
                                }
                                send_wx_subscribe_message(o["openid"], "5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A", sub_data, phone=o["user_phone"])
                            except Exception as e:
                                logger.error('[自动清柜] 发送通知失败')

                        total_ended += 1

                    conn.commit()

                    # 【修复2】commit成功后才更新数据库标记
                    c.execute("UPDATE locations SET last_clear_date=%s WHERE id=%s", (today_str, loc_id))
                    conn.commit()

                    if total_ended > 0:
                        logger.info("[自动清柜] Location %s: 结束 %s 个订单(周期=%s天,截止时间=%s)" % (loc_id, total_ended, cycle, cutoff_time))

                except Exception as e:
                    logger.error("[自动清柜] Location %s 处理异常: %s" % (loc_id, e))
                    try:
                        conn.rollback()
                    except:
                        pass

            # 【修复3】正常关闭连接
            conn.close()
        except Exception as e:
            logger.error("[自动清柜] 异常: %s" % e)
            try:
                conn.close()
            except:
                pass

_auto_clear_thread = threading.Thread(target=_auto_clear_cabinet_scheduler, daemon=True)
_auto_clear_thread.start()
logger.info("[启动] 自动清柜任务已启动(每分钟检查,PostgreSQL连接)")


# Start merchant health scheduler
logger.info('[启动] 自动清柜任务已启动(每分钟检查)')


# Start merchant health scheduler
threading.Thread(target=merchant_health_scheduler, daemon=True).start()
logger.info('[启动] 商户号健康巡检任务已启动(每30分钟)')


if __name__ == '__main__':
    print("=" * 50)
    print("智能寄存柜系统 v2 已启动")
    print(f"访问地址: http://localhost:{PORT}")

@app.route('/fFBR3J2qOh.txt')
def merchant_verify():
    return send_from_directory('static', 'fFBR3J2qOh.txt')
