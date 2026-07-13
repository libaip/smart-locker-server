"""
智能寄存柜系统 - 设备注册与配置API
竞品模式：APK只需输入设备ID即可完成激活
"""
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

bp = Blueprint('device', __name__)

# 主板类型 → 串口映射
BOARD_SERIAL_MAP = {
    "YBM": {"serial_port": "/dev/ttyS4", "baud_rate": 9600},
    "WT":  {"serial_port": "/dev/ttyS3", "baud_rate": 115200},
    "QM":  {"serial_port": "/dev/ttyS2", "baud_rate": 9600},
}

# 默认设备配置（服务端兜底）
DEFAULT_CONFIG = {
    "serial_port": BOARD_SERIAL_MAP["YBM"]["serial_port"],
    "baud_rate": BOARD_SERIAL_MAP["YBM"]["baud_rate"],
    "serial_type": "BaseSerial",
    "protocol": "YBM",
    "board_start": 1,
    "board_count": 1,
    "server_url": "https://locker.cqdyxl.com",
    "websocket_url": "ws://locker.cqdyxl.com/ws/"
}

def get_board_config(protocol):
    """根据主板类型获取串口配置"""
    board = BOARD_SERIAL_MAP.get(protocol.upper())
    if board:
        return board["serial_port"], board["baud_rate"], protocol.upper()
    return DEFAULT_CONFIG["serial_port"], DEFAULT_CONFIG["baud_rate"], DEFAULT_CONFIG["protocol"]


@bp.route('/device/register', methods=['POST'])
def register_device():
    """设备注册 - APK首次启动时调用"""
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id', '').strip()
    protocol = data.get('protocol', '').strip()
    serial_port, baud_rate, resolved_protocol = get_board_config(protocol)

    if not device_id:
        return jsonify({'code': 400, 'message': '缺少设备ID', 'data': None}), 400

    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()

        cursor.execute('SELECT * FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()

        if cabinet:
            cursor.execute("UPDATE cabinets SET last_heartbeat = NOW(), business_status='active' WHERE mainboard_device_id = %s",
                          (device_id,))
            db.commit()

            cursor.execute('SELECT * FROM mainboards WHERE cabinet_id = %s ORDER BY board_index LIMIT 1',
                          (cabinet['id'],))
            mainboard = cursor.fetchone()

            # Get actual slot count from cabinet_slots table
            cursor.execute('SELECT COUNT(*) as total, SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) as available FROM cabinet_slots WHERE cabinet_id=%s', (cabinet['id'],))
            slot_info = cursor.fetchone()
            total_slots = slot_info['total'] if slot_info and slot_info['total'] else cabinet['total_slots']
            available_slots = slot_info['available'] if slot_info and slot_info['available'] else total_slots

            config = {
                "device_id": device_id,
                "cabinet_id": cabinet['id'],
                "serial_port": mainboard['serial_port'] if mainboard else serial_port,
                "baud_rate": mainboard['baud_rate'] if mainboard else baud_rate,
                "protocol": cabinet['mainboard_source'] or resolved_protocol,
                "board_start": 1,
                "board_count": cabinet['total_slots'] // 16 + 1 if cabinet['total_slots'] else 1,
                "total_slots": total_slots,
                "available_slots": available_slots,
                "server_url": DEFAULT_CONFIG['server_url'],
                "websocket_url": DEFAULT_CONFIG['websocket_url'],
                "store_name": cabinet['name'] or "",
                "customer_phone": cabinet['customer_phone'] or "",
                "business_hours": cabinet['business_hours'] or "8:00~22:00",
                "status": "registered"
            }
            db.close()
            return jsonify({'code': 200, 'message': '设备已注册', 'data': config})

        cursor.execute('''INSERT INTO cabinets (cabinet_code, name, mainboard_device_id, mainboard_source,
                         total_slots, deposit_amount, business_status, last_heartbeat)
                         VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW())''',
                      (device_id, f'柜机-{device_id}', device_id, resolved_protocol,
                       12, 20))
        cabinet_id = cursor.lastrowid

        cursor.execute('''INSERT INTO mainboards (cabinet_id, board_index, slot_count, serial_port, baud_rate)
                         VALUES (%s, %s, %s, %s, %s)''',
                      (cabinet_id, 1, 16, serial_port, baud_rate))
        mainboard_id = cursor.lastrowid

        for slot_num in range(1, 13):
            cursor.execute('''INSERT INTO cabinet_slots (cabinet_id, mainboard_id, slot_number, status,
                             board_no, lock_no)
                             VALUES (%s, %s, %s, 1, 1, %s)''',
                          (cabinet_id, mainboard_id, slot_num, slot_num))

        db.commit()
        db.close()

        config = {
            "device_id": device_id,
            "cabinet_id": cabinet_id,
            "serial_port": serial_port,
            "baud_rate": baud_rate,
            "protocol": resolved_protocol,
            "board_start": 1,
            "board_count": 1,
            "total_slots": 12,
            "available_slots": 12,
            "server_url": DEFAULT_CONFIG['server_url'],
            "websocket_url": DEFAULT_CONFIG['websocket_url'],
            "store_name": f'柜机-{device_id}',
            "status": "new"
        }

        logger.info(f'[设备注册] 新设备注册成功: device_id={device_id}, cabinet_id={cabinet_id}')
        return jsonify({'code': 200, 'message': '注册成功', 'data': config})

    except Exception as e:
        logger.error(f'[设备注册] 失败: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': f'注册失败: {str(e)}', 'data': None}), 500


@bp.route('/device/config/<device_id>', methods=['GET', 'POST'])
def get_device_config(device_id):
    """获取设备配置"""
    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()

        cursor.execute('SELECT * FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            db.close()
            return jsonify({'code': 404, 'message': '设备未找到', 'data': None}), 404
        # 每次状态查询也刷新心跳
        cursor.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
        db.commit()

        cursor.execute('SELECT * FROM mainboards WHERE cabinet_id = %s ORDER BY board_index LIMIT 1',
                      (cabinet['id'],))
        mainboard = cursor.fetchone()

        config = {
            "device_id": device_id,
            "serial_port": mainboard['serial_port'] if mainboard else DEFAULT_CONFIG['serial_port'],
            "baud_rate": mainboard['baud_rate'] if mainboard else DEFAULT_CONFIG['baud_rate'],
            "protocol": cabinet['mainboard_source'] or DEFAULT_CONFIG['protocol'],
            "board_start": 1,
            "board_count": cabinet['total_slots'] // 16 + 1 if cabinet['total_slots'] else 1,
            "server_url": DEFAULT_CONFIG['server_url'],
            "websocket_url": DEFAULT_CONFIG['websocket_url'],
            "version": cabinet['app_version'] or '',
            "version_code": cabinet['app_version_code'] or 0,
            "store_name": cabinet['name'] or ''
        }
        db.close()
        return jsonify({'code': 200, 'message': 'success', 'data': config})

    except Exception as e:
        logger.error(f'[设备配置] 获取失败: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500


@bp.route('/device/update-app', methods=['POST'])
def update_device_app():
    """APK版本更新接口"""
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id', '').strip()
    version = data.get('version', '')
    version_code = data.get('version_code', 0)
    apk_url = data.get('apk_url', '/static/smart-locker.apk')

    if not device_id:
        return jsonify({'code': 400, 'message': '缺少设备ID', 'data': None}), 400

    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()
        cursor.execute("UPDATE cabinets SET app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                      (version, version_code, device_id))
        db.commit()
        db.close()
        return jsonify({
            'code': 200, 'message': 'success',
            'data': {
                'has_update': False,
                'latest_version': version,
                'apk_url': apk_url
            }
        })
    except Exception as e:
        logger.error(f'[设备更新] 失败: {e}')
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500

# ========== Extra device APIs to append ==========

@bp.route('/device/status', methods=['GET', 'POST'])
def device_status():
    """设备状态查询 - APK定期调用"""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '').strip()
    else:
        device_id = request.args.get('device_id', '').strip()
    if not device_id:
        return jsonify({'code': 400, 'message': '缺少设备ID', 'data': None}), 400

    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT * FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            db.close()
            return jsonify({'code': 404, 'message': '设备未找到', 'data': None}), 404
        # 每次状态查询也刷新心跳
        cursor.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
        db.commit()

        cursor.execute('SELECT * FROM mainboards WHERE cabinet_id = %s ORDER BY board_index LIMIT 1', (cabinet['id'],))
        mainboard = cursor.fetchone()

        # 已禁用设备端自动更新检查
        #         cursor.execute('SELECT version_name, version_code, download_url FROM apk_version ORDER BY version_code DESC LIMIT 1')
        #         latest_apk = cursor.fetchone()

        data = {
            'device_id': device_id,
            'status': 'online',
            'serial_port': mainboard['serial_port'] if mainboard else DEFAULT_CONFIG['serial_port'],
            'baud_rate': mainboard['baud_rate'] if mainboard else DEFAULT_CONFIG['baud_rate'],
            'protocol': cabinet['mainboard_source'] or DEFAULT_CONFIG['protocol'],
            'board_start': 1,
            'board_count': cabinet['total_slots'] // 16 + 1 if cabinet['total_slots'] else 1,
            'server_url': DEFAULT_CONFIG['server_url'],
            'websocket_url': DEFAULT_CONFIG['websocket_url'],
            'store_name': cabinet['name'] or '',
            'app_version': cabinet['app_version'] or '',
            'app_version_code': cabinet['app_version_code'] or 0,
            'has_update': False,
            'latest_version': '',
            'latest_version_code': 0,
            'apk_url': ''
        }

        #         if latest_apk and cabinet['app_version_code'] < latest_apk['version_code']:
        #             data['has_update'] = True
        #             data['latest_version'] = latest_apk['version_name']
        #             data['latest_version_code'] = latest_apk['version_code']
        #             data['apk_url'] = latest_apk['download_url'] or '/static/smart-locker.apk'

        db.close()
        return jsonify({'code': 200, 'message': 'success', 'data': data})

    except Exception as e:
        logger.error(f'[设备状态] 查询失败: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500


@bp.route('/device/heartbeat', methods=['POST', 'GET'])
def device_heartbeat():
    """设备心跳 - APK定期上报"""
    if request.method == 'GET':
        device_id = request.args.get('device_id', '').strip()
        app_version = request.args.get('version', '')
        app_version_code = int(request.args.get('version_code', 0))
    else:
        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '').strip()
        app_version = data.get('version', '')
        app_version_code = data.get('version_code', 0)

    if not device_id:
        return jsonify({'code': 400, 'message': '缺少设备ID', 'data': None}), 400

    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT id, name FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if cabinet:
            cursor.execute(
                "UPDATE cabinets SET last_heartbeat=NOW(), business_status='active', app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                (app_version, app_version_code, device_id))
            db.commit()
            db.close()
            return jsonify({'code': 200, 'message': 'ok', 'data': {'status': 'online'}})
        else:
            db.close()
            return jsonify({'code': 404, 'message': '设备未注册', 'data': None}), 404

    except Exception as e:
        logger.error(f'[设备心跳] 失败: {e}')
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500


@bp.route('/pending-commands/<device_id>', methods=['GET'])
def pending_commands(device_id):
    """获取待执行的指令 - APK HTTP轮询调用"""
    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()

        # 查找该设备的柜体
        cursor.execute('SELECT id, name FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            db.close()
            return jsonify({'code': 200, 'data': {'commands': [], 'orders': []}})

        cabinet_name = cabinet.get('name', '')
        # 更新心跳时间（设备每次轮询都刷新）
        cursor.execute("UPDATE cabinets SET last_heartbeat=NOW() WHERE mainboard_device_id=%s", (device_id,))
        db.commit()

        # 不跳过WS在线设备，APK通过HTTP轮询获取所有命令

        commands = []
        # 从PostgreSQL读取pending命令
        _cur2 = db.cursor()
        _cur2.execute('SELECT * FROM pending_lock_cmds WHERE device_id=%s AND delivered=0 ORDER BY id', (device_id,))
        pending_rows = _cur2.fetchall()
        for row in pending_rows:
            cmd_json = row['command'] if row['command'] else ''
            if cmd_json and ('force_update' in cmd_json or 'usage_rules' in cmd_json or 'update_config' in cmd_json or 'unbind' in cmd_json):
                # JSON类型的控制命令，直接透传给APK
                try:
                    import json as _json
                    cmd_obj = _json.loads(cmd_json)
                    commands.append(cmd_obj)
                except:
                    pass
                # 标记已投递
                _cur2.execute('UPDATE pending_lock_cmds SET delivered=1 WHERE id=%s', (row['id'],))
            else:
                commands.append({
                    'order_id': str(row['order_id']) if row['order_id'] else '',
                    'board_no': row['board_no'],
                    'lock_no': row['lock_no'],
                    'action': 'open',
                    'protocol': row['protocol']
                })
                # 标记开锁指令已投递，防止重复开门
                _cur2.execute('UPDATE pending_lock_cmds SET delivered=1 WHERE id=%s', (row['id'],))
        db.commit()

        # 查询该柜体下所有主板配置，随轮询返回给APK（设备自动同步）
        _cur3 = db.cursor()
        _cur3.execute('SELECT board_index, serial_port, baud_rate, protocol FROM mainboards WHERE cabinet_id=%s ORDER BY board_index', (cabinet['id'],))
        mb_rows = _cur3.fetchall()
        mainboard_config = []
        for mb in mb_rows:
            mainboard_config.append({
                'board_index': mb['board_index'],
                'serial_port': mb['serial_port'],
                'baud_rate': mb['baud_rate'],
                'protocol': mb['protocol'] or 'YBM'
            })

        db.close()
        return jsonify({'code': 200, 'data': {'commands': commands, 'orders': [], 'cabinet_name': cabinet_name, 'mainboard_config': mainboard_config}})

    except Exception as e:
        logger.error(f'[待执行指令] 查询失败: {e}', exc_info=True)
        return jsonify({'code': 200, 'data': {'commands': [], 'orders': []}})

@bp.route('/pending-update/<device_id>', methods=['GET'])
def pending_update(device_id):
    """???? - ??force_update????3?????"""
    try:
        from database import get_db
        from helpers import logger
        db = get_db()
        cursor = db.cursor()

        cursor.execute("SELECT id, name FROM cabinets WHERE mainboard_device_id = %s", (device_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            db.close()
            return jsonify({"code": 200, "data": {"commands": [], "orders": []}})

        cursor.execute("SELECT * FROM pending_lock_cmds WHERE device_id=%s AND delivered=0 ORDER BY id", (device_id,))
        rows = cursor.fetchall()
        commands = []
        for row in rows:
            cmd_json = row["command"] if row["command"] else ""
            if cmd_json and "force_update" in cmd_json:
                import json as _json
                try:
                    cmd_obj = _json.loads(cmd_json)
                    commands.append(cmd_obj)
                except:
                    pass
                cursor.execute("UPDATE pending_lock_cmds SET delivered=1 WHERE id=%s", (row["id"],))
        db.commit()
        db.close()
        return jsonify({"code": 200, "data": {"commands": commands, "orders": []}})

    except Exception as e:
        logger.error(f"[?????] ????: {e}")
        return jsonify({"code": 200, "data": {"commands": [], "orders": []}})

@bp.route("/scan")
def scan_page():
    device = request.args.get("device", "")
    if not device:
        return jsonify({"code": 400, "message": "missing device"}), 400
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM cabinets WHERE mainboard_device_id=%s", (device,))
    cabinet = cursor.fetchone()
    db.close()
    if not cabinet:
        return jsonify({"code": 404, "message": "device not found"}), 404
    store_name = cabinet["name"] or "\u667a\u80fd\u5bc4\u5b58\u67dc"
    return render_template_string(SCAN_HTML, device=device, store_name=store_name)

SCAN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>\u667a\u80fd\u5bc4\u5b58\u67dc - \u5b58\u5305</title>
<style>
body{font-family:sans-serif;margin:0;padding:20px;background:#f0f2f5;text-align:center}
.card{background:#fff;border-radius:12px;padding:30px;max-width:400px;margin:40px auto;box-shadow:0 4px 20px rgba(0,0,0,0.1)}
h2{color:#333;margin-bottom:10px}
p{color:#666;font-size:14px}
.btn{display:inline-block;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:14px 40px;border-radius:8px;text-decoration:none;font-size:16px;margin-top:20px}
.info{margin:15px 0;color:#999;font-size:13px}
</style>
</head>
<body>
<div class="card">
<h2>\u667a\u80fd\u5bc4\u5b58\u67dc</h2>
<p>\u8bbe\u5907\u53f7: {{ device }}</p>
<p>\u7f51\u70b9: {{ store_name }}</p>
<div class="info">\u8bf7\u70b9\u51fb\u4e0b\u65b9\u6309\u94ae\u5f00\u59cb\u5b58\u5305</div>
<a class="btn" href="#">\u626b\u7801\u5b58\u5305</a>
</div>
</body>
</html>"""


@bp.route('/debug/pending/<device_id>', methods=['GET'])
def debug_pending(device_id):
    from helpers import pending_lock_commands, connected_devices, logger
    import json
    logger.info(f'[DEBUG_ROUTE] id(pending)={id(pending_lock_commands)}, keys={list(pending_lock_commands.keys())}')
    pending = pending_lock_commands.get(device_id, [])
    return jsonify({
        'device_id': device_id,
        'pending_count': len(pending),
        'pending_commands': pending,
        'connected_devices': list(connected_devices.keys()),
        'all_pending_keys': list(pending_lock_commands.keys())
    })


@bp.route("/device/lock-result", methods=["POST"])
@bp.route("/lock-result", methods=["POST"])
def device_lock_result():
    """设备上报开锁结果"""
    from database import get_db
    try:
        data = request.get_json(force=True)
        device_id = data.get("device_id", "")
        board_no = data.get("board_no", 0)
        lock_no = data.get("lock_no", 0)
        success = data.get("success", False)
        
        if not device_id:
            return jsonify({"code": 400, "message": "缺少device_id"}), 400
        
        db = get_db()
        slot = db.execute(
            "SELECT cs.id, cs.slot_number FROM cabinet_slots cs "
            "JOIN cabinets c ON cs.cabinet_id = c.id "
            "WHERE c.mainboard_device_id = %s AND cs.board_no = %s AND cs.lock_no = %s",
            (device_id, board_no, lock_no)
        ).fetchone()
        
        slot_id = slot["id"] if slot else None
        slot_number = slot["slot_number"] if slot else str(lock_no)
        
        db.execute(
            "INSERT INTO remote_open_logs (device_id, slot_id, slot_number, result, success, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (device_id, slot_id, slot_number, "success" if success else "failed", 1 if success else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        
        # 根据logical_mark决定动作: end=结束订单, mid=中途取物
        if success and slot_id:
            try:
                _c2 = db.cursor()
                _c2.execute("SELECT id,logical_mark FROM orders WHERE slot_id=%s AND status=2 ORDER BY id DESC LIMIT 1", (slot_id,))
                _o2 = _c2.fetchone()
                if _o2 and _o2["logical_mark"] == "mid":
                    db.execute("UPDATE orders SET logical_mark='N' WHERE id=%s", (_o2["id"],))
                    logger.info(f"[lock_result] action=mid: slot={slot_id} mid-retrieve (no end)")
                elif _o2 and _o2["logical_mark"] == "end":
                    db.execute("UPDATE orders SET status=3,retrieve_time=NOW(),logical_mark='N',refund_mark=1,refund_amount=deposit_amount WHERE id=%s AND status=2", (_o2["id"],))
                    db.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s", (slot_id,))
                    logger.info(f"[lock_result] action=end: slot={slot_id} ended (free slot)")
                else:
                    logger.info(f"[lock_result] no action mark: slot={slot_id} (mark={_o2.get('logical_mark','?')})")
            except Exception as _e2:
                logger.warning(f"[lock_result] check action failed: {_e2}")
        logger.info(f"[lock_result] 开锁: device={device_id} slot_id={slot_id}")
        
        db.commit()
        logger.info(f"[lock_result] device={device_id} board={board_no} lock={lock_no} success={success}")
        return jsonify({"code": 200, "message": "ok"})
    except Exception as e:
        logger.error(f"[lock_result] {e}")
        return jsonify({"code": 500, "message": str(e)}), 500


@bp.route("/device/orders", methods=["GET"])
def device_orders():
    """设备同步订单"""
    from database import get_db
    try:
        device_id = request.args.get("device_id", "")
        if not device_id:
            return jsonify({"code": 400, "message": "缺少device_id"}), 400
        
        db = get_db()
        orders = db.execute(
            "SELECT o.id, o.order_no, o.slot_id, o.compartment_number, o.status, o.deposit_amount, "
            "o.created_at, o.retrieve_time, o.access_code as retrieve_code "
            "FROM orders o "
            "JOIN cabinets c ON o.cabinet_id = c.id "
            "WHERE c.mainboard_device_id = %s AND o.status IN (\"active\", \"overdue\") "
            "ORDER BY o.created_at DESC LIMIT 200",
            (device_id,)
        ).fetchall()
        
        result = []
        for o in orders:
            result.append({
                "id": o["id"], "order_no": o["order_no"],
                "slot_id": o["slot_id"], "compartment_number": o["compartment_number"],
                "status": o["status"], "deposit_amount": o["deposit_amount"],
                "created_at": o["created_at"], "retrieve_time": o["retrieve_time"],
                "retrieve_code": o["retrieve_code"]
            })
        
        return jsonify({"code": 200, "data": result, "message": "success"})
    except Exception as e:
        logger.error(f"[device_orders] {e}")
        return jsonify({"code": 500, "message": str(e)}), 500
