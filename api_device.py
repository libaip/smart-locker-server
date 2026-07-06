"""
api_device.py — 设备管理模块（5个接口）
1. POST /api/device/checkDeviceLogs
2. POST /api/device/createDeviceParam
3. POST /api/device/getToken
4. POST /api/device/updateApp
5. POST /api/device/updateDevice
"""
import sqlite3
import uuid
from flask import Blueprint, request, jsonify

device_new_bp = Blueprint('device_new', __name__)


def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 接口1: checkDeviceLogs ----------
@device_new_bp.route('/device/checkDeviceLogs', methods=['POST'])
def check_device_logs():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        logs = data.get('logs', [])

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS device_logs ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'device_id TEXT, '
            'logs TEXT, '
            'create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        )

        # 如果有日志内容则插入
        if logs and device_id:
            import json
            cursor.execute(
                'INSERT INTO device_logs (device_id, logs) VALUES (%s, %s)',
                (device_id, json.dumps(logs, ensure_ascii=False))
            )
            conn.commit()

        conn.close()
        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {'handled': True}
        })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })


# ---------- 接口2: createDeviceParam ----------
@device_new_bp.route('/device/createDeviceParam', methods=['POST'])
def create_device_param():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        params = data.get('params', {})

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS device_params ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'device_id TEXT, '
            'params TEXT, '
            'create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        )

        import json
        cursor.execute(
            'INSERT INTO device_params (device_id, params) VALUES (%s, %s)',
            (device_id, json.dumps(params, ensure_ascii=False))
        )
        conn.commit()
        param_id = cursor.lastrowid
        conn.close()

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {'id': param_id}
        })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })


# ---------- 接口3: getToken ----------
@device_new_bp.route('/device/getToken', methods=['POST'])
def get_token():
    try:
        token = str(uuid.uuid4())
        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {'token': token}
        })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })


# ---------- 接口4: updateApp ----------
@device_new_bp.route('/device/updateApp', methods=['POST'])
def update_app():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        current_version = data.get('current_version', '')

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS apk_version ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'version_name TEXT, '
            'version_code INTEGER, '
            'download_url TEXT, '
            'update_desc TEXT, '
            'create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        )

        cursor.execute(
            'SELECT * FROM apk_version ORDER BY id DESC LIMIT 1'
        )
        row = cursor.fetchone()
        conn.close()

        if row and row['version_name'] != current_version:
            return jsonify({
                'code': 0,
                'msg': 'success',
                'data': {
                    'has_update': True,
                    'version_name': row['version_name'],
                    'version_code': row['version_code'],
                    'download_url': row['download_url'],
                    'update_desc': row['update_desc']
                }
            })
        else:
            return jsonify({
                'code': 0,
                'msg': 'success',
                'data': {
                    'has_update': False
                }
            })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })


# ---------- 接口5: updateDevice ----------
@device_new_bp.route('/device/updateDevice', methods=['POST'])
def update_device():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')

        if not device_id:
            return jsonify({
                'code': -1,
                'msg': 'device_id不能为空'
            })

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS devices ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'device_id TEXT UNIQUE, '
            'device_name TEXT, '
            'device_type TEXT, '
            'status TEXT, '
            'remark TEXT, '
            'update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        )

        # 构建动态更新字段
        update_fields = []
        update_values = []
        for key in ['device_name', 'device_type', 'status', 'remark']:
            if key in data:
                update_fields.append(f'{key}=%s')
                update_values.append(data[key])

        if update_fields:
            update_values.append(device_id)
            sql = f'UPDATE devices SET {", ".join(update_fields)}, update_time=CURRENT_TIMESTAMP WHERE device_id=%s'
            cursor.execute(sql, update_values)
        else:
            # 没有额外字段则尝试插入默认记录
            cursor.execute(
                'INSERT OR IGNORE INTO devices (device_id) VALUES (%s)',
                (device_id,)
            )

        conn.commit()
        affected = cursor.rowcount
        conn.close()

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {'affected': affected}
        })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })

# ---------- 管理后台: 强制推送更新 ----------
@device_new_bp.route('/admin/force-update', methods=['POST'])
def admin_force_update():
    """管理后台推送更新到指定设备"""
    try:
        from helpers import connected_devices, logger
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        
        if not device_id:
            return jsonify({'code': -1, 'msg': '缺少device_id'})
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM apk_version ORDER BY id DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return jsonify({'code': -1, 'msg': '没有可用的APK版本'})
        
        update_msg = {
            'type': 'force_update',
            'version_name': row['version_name'],
            'version_code': row['version_code'],
            'download_url': row['download_url'],
            'update_desc': row['update_desc'] or '',
            'timestamp': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        if device_id in connected_devices:
            ws = connected_devices[device_id]
            try:
                import json
                ws.send(json.dumps(update_msg))
                logger.info('[FORCE_UPDATE] push ok: device=%s version=%s' % (device_id, row['version_name']))
                return jsonify({'code': 0, 'msg': '推送成功', 'data': {'version': row['version_name']}})
            except Exception as e:
                logger.error('[FORCE_UPDATE] push fail: %s' % str(e))
                return jsonify({'code': -1, 'msg': 'WebSocket发送失败: %s' % str(e)})
        else:
            return jsonify({'code': -1, 'msg': '设备不在线，无法推送'})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})
