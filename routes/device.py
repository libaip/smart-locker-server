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
            cursor.execute("UPDATE cabinets SET last_heartbeat = NOW(), business_status=CASE WHEN business_status IN ('inactive','closed') THEN business_status ELSE 'active' END WHERE mainboard_device_id = %s",
                          (device_id,))
            db.commit()

            cursor.execute('SELECT * FROM mainboards WHERE cabinet_id = %s ORDER BY board_index LIMIT 1',
                          (cabinet['id'],))
            mainboard = cursor.fetchone()

            # Get actual slot count from cabinet_slots table
            cursor.execute('SELECT COUNT(*) as total, SUM(CASE WHEN status=1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id = cabinet_slots.id AND o2.status = 2) THEN 1 ELSE 0 END) as available FROM cabinet_slots WHERE cabinet_id=%s', (cabinet['id'],))
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

        if latest_apk and cabinet['app_version_code'] < latest_apk['version_code']:
            data['has_update'] = True
            data['latest_version'] = latest_apk['version_name']
            data['latest_version_code'] = latest_apk['version_code']
            data['apk_url'] = latest_apk['download_url'] or '/static/smart-locker.apk'

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
                "UPDATE cabinets SET last_heartbeat=NOW(), business_status=CASE WHEN business_status IN ('inactive','closed') THEN business_status ELSE 'active' END, app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
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


@bp.route('/device/local-open', methods=['POST'])
def device_local_open():
    """APK本地“再次开门”上报，服务端强制计数"""
    try:
        data = request.get_json(silent=True) or {}
        order_id = data.get('order_id')
        if not order_id:
            return jsonify({'code': 400, 'message': '订单ID不能为空', 'data': None}), 400
        from database import get_db
        db = get_db()
        cursor = db.cursor()
        cursor.execute("""SELECT o.id, o.reopen_count, c.mainboard_device_id, c.reopen_times,
                                 l.reopen_times as location_reopen_times
                          FROM orders o
                          JOIN cabinets c ON o.cabinet_id = c.id
                          LEFT JOIN locations l ON c.location_id = l.id
                          WHERE o.id = %s FOR UPDATE OF o, c""", (order_id,))
        order = cursor.fetchone()
        if not order:
            db.close()
            return jsonify({'code': 404, 'message': '订单不存在', 'data': None}), 404
        device_id = str(data.get('device_id') or '')
        if device_id and str(order['mainboard_device_id'] or '') != device_id:
            db.close()
            return jsonify({'code': 400, 'message': '订单不属于当前设备', 'data': None}), 400
        limit = order['reopen_times']
        if limit is None or limit == '' or int(limit) <= 0:
            limit = order['location_reopen_times']
        used = int(order['reopen_count'] or 0)
        if limit is not None and limit != '' and int(limit) > 0 and used >= int(limit):
            db.close()
            return jsonify({'code': 400, 'message': '已超过再次开门次数上限', 'data': {
                'reopen_count': used,
                'reopen_times': int(limit),
                'reopen_remaining': 0,
            }}), 400
        cursor.execute('UPDATE orders SET reopen_count = COALESCE(reopen_count, 0) + 1 WHERE id = %s', (order_id,))
        cursor.execute('SELECT cs.board_no, cs.lock_no FROM orders o JOIN cabinet_slots cs ON o.slot_id = cs.id WHERE o.id = %s', (order_id,))
        slot = cursor.fetchone()
        if slot:
            try:
                cursor.execute("INSERT INTO door_records (device_id, board_no, lock_no, order_id, open_type) VALUES (%s,%s,%s,%s,%s)",
                               (order['mainboard_device_id'], slot['board_no'] or 1, slot['lock_no'] or 1, str(order_id), 'local_reopen'))
            except Exception as dre:
                logger.warning('[local-open] door_records写入失败: %s', dre)
        new_count = used + 1
        remaining = -1
        limit_val = -1
        if limit is not None and limit != '' and int(limit) > 0:
            limit_val = int(limit)
            remaining = max(0, limit_val - new_count)
        db.commit()
        db.close()
        return jsonify({'code': 200, 'message': 'ok', 'data': {
            'reopen_count': new_count,
            'reopen_times': limit_val,
            'reopen_remaining': remaining,
        }})
    except Exception as e:
        logger.error(f'[local-open] error: {e}')
        try:
            db.close()
        except Exception:
            pass
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500


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

        cursor.execute("SELECT * FROM pending_lock_cmds WHERE device_id=%s AND (delivered=0 OR status='pending') ORDER BY id", (device_id,))
        rows = cursor.fetchall()
        commands = []
        for row in rows:
            cmd_json = row["command"] if row["command"] else ""
            if not cmd_json:
                continue
            # 放行 open_lock（开门）与 force_update（升级）指令；跳过其他类型
            if "open_lock" not in cmd_json and "force_update" not in cmd_json:
                continue
            import json as _json
            try:
                cmd_obj = _json.loads(cmd_json)
                commands.append(cmd_obj)
            except:
                pass
            cursor.execute("UPDATE pending_lock_cmds SET delivered=1, status='completed' WHERE id=%s", (row['id'],))
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
        cmd_id = data.get("cmd_id", "")
        
        if not device_id:
            return jsonify({"code": 400, "message": "缺少device_id"}), 400
        
        db = get_db()
        
        # === 指令回执标记: 按cmd_id把对应的pending指令标记为已完成, 防止重复下发 ===
        if cmd_id:
            try:
                _ack_cur = db.execute(
                    "UPDATE pending_lock_cmds SET delivered=1, status='completed' "
                    "WHERE command LIKE %s AND device_id=%s AND (delivered=0 OR status IN ('pending','sent'))",
                    ('%' + cmd_id + '%', device_id)
                )
                if _ack_cur and _ack_cur.rowcount > 0:
                    logger.info(f"[lock_result] cmd_id回执标记完成: device={device_id}, cmd_id={cmd_id}, rows={_ack_cur.rowcount}")
                else:
                    # 没匹配到pending指令(可能已标记或指令不存在), 不报错
                    logger.info(f"[lock_result] cmd_id回执无匹配pending: device={device_id}, cmd_id={cmd_id}")
            except Exception as _ack_e:
                logger.warning(f"[lock_result] cmd_id回执标记失败: {_ack_e}")
        
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
        
        # === 幽灵开门监控：检测APK上报了但服务器没下过命令的开锁 ===
        try:
            _now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _phantom_check = db.execute(
                "SELECT id FROM door_records WHERE device_id=%s AND board_no::text=%s::text AND lock_no::text=%s::text "
                "AND create_time BETWEEN %s::timestamp - INTERVAL '60 seconds' AND %s::timestamp + INTERVAL '60 seconds' "
                "LIMIT 1",
                (device_id, str(board_no), str(lock_no), _now_ts, _now_ts)
            ).fetchone()
            if not _phantom_check:
                import json as _pjson
                _req_dump = _pjson.dumps(data, ensure_ascii=False, default=str)[:500]
                _order_id = data.get("order_id", "")
                _ts = data.get("timestamp", "")
                _phantom_line = (
                    f"PHANTOM|{_now_ts}|dev={device_id}|board={board_no}|lock={lock_no}"
                    f"|slot={slot_number}|order_id={_order_id}|ts={_ts}"
                    f"|req={_req_dump}"
                )
                logger.warning(f"[PHANTOM_OPEN] {_phantom_line}")
                try:
                    with open("/home/ubuntu/smart-locker/phantom_open.log", "a") as _pf:
                        _pf.write(_phantom_line + "\n")
                except:
                    pass
        except Exception as _pe:
            logger.warning(f"[phantom_check] error: {_pe}")
        # === 幽灵开门监控 END ===
        
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
                    _end_cur = db.execute("UPDATE orders SET status=3,retrieve_time=NOW(),logical_mark='N',refund_mark=1,refund_amount=deposit_amount WHERE id=%s AND status=2", (_o2["id"],))
                    if _end_cur.rowcount > 0:
                        db.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s", (slot_id,))
                        logger.info(f"[lock_result] action=end: slot={slot_id} ended (free slot)")
                    else:
                        logger.warning(f"[lock_result] action=end but order update matched 0 rows: slot={slot_id} order={_o2['id']}, slot NOT released")
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
