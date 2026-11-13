"""api_device.py — 设备管理模块（5个接口）"""
import sqlite3, uuid
from flask import Blueprint, request, jsonify
device_new_bp = Blueprint('device_new', __name__)
def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn

@device_new_bp.route('/device/checkDeviceLogs', methods=['POST'])
def check_device_logs():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        logs = data.get('logs', [])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS device_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, logs TEXT, create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        if logs and device_id:
            import json
            cursor.execute('INSERT INTO device_logs (device_id, logs) VALUES (%s, %s)', (device_id, json.dumps(logs, ensure_ascii=False)))
            conn.commit()
        conn.close()
        return jsonify({'code': 0, 'msg': 'success', 'data': {'handled': True}})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})

@device_new_bp.route('/device/createDeviceParam', methods=['POST'])
def create_device_param():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        params = data.get('params', {})
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS device_params (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, params TEXT, create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        import json
        cursor.execute('INSERT INTO device_params (device_id, params) VALUES (%s, %s)', (device_id, json.dumps(params, ensure_ascii=False)))
        conn.commit()
        pid = cursor.lastrowid
        conn.close()
        return jsonify({'code': 0, 'msg': 'success', 'data': {'id': pid}})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})

@device_new_bp.route('/device/getToken', methods=['POST'])
def get_token():
    try:
        return jsonify({'code': 0, 'msg': 'success', 'data': {'token': str(uuid.uuid4())}})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})

@device_new_bp.route('/device/updateApp', methods=['POST'])
def update_app():
    # 已禁用设备端自动更新检查，始终返回无更新
    return jsonify({'code': 0, 'msg': 'success', 'data': {'has_update': False}})

@device_new_bp.route('/device/updateDevice', methods=['POST'])
def update_device():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        if not device_id:
            return jsonify({'code': -1, 'msg': 'device_id不能为空'})
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT UNIQUE, device_name TEXT, device_type TEXT, status TEXT, remark TEXT, update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        update_fields, update_values = [], []
        for key in ['device_name', 'device_type', 'status', 'remark']:
            if key in data:
                update_fields.append(f'{key}=%s')
                update_values.append(data[key])
        if update_fields:
            update_values.append(device_id)
            cursor.execute(f'UPDATE devices SET {", ".join(update_fields)}, update_time=CURRENT_TIMESTAMP WHERE device_id=%s', update_values)
        else:
            cursor.execute('INSERT OR IGNORE INTO devices (device_id) VALUES (%s)', (device_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return jsonify({'code': 0, 'msg': 'success', 'data': {'affected': affected}})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})
