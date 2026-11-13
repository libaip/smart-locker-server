"""api_box.py — 箱子模块 POST /api/box/updateDeviceBox"""
import sqlite3
from flask import Blueprint, request, jsonify
from database import get_db

box_bp = Blueprint('box', __name__)

@box_bp.route('/box/updateDeviceBox', methods=['POST'])
def update_device_box():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        board_no = data.get('board_no', 0)
        lock_no = data.get('lock_no', 0)
        status = data.get('status', '')
        if not all([device_id, status]):
            return jsonify({'code': -1, 'msg': '参数不完整'})
        status_map = {'empty': 1, 'used': 2, 'available': 1, 'occupied': 2, '1': 1, '2': 2}
        int_status = status_map.get(status, int(status) if str(status).isdigit() else 1)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            conn.close()
            return jsonify({'code': -1, 'msg': '设备未找到'})
        cabinet_id = cabinet['id']
        cursor.execute('UPDATE cabinet_slots SET status = %s WHERE cabinet_id = %s AND board_no = %s AND lock_no = %s',
                       (int_status, cabinet_id, board_no, lock_no))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        if affected > 0:
            return jsonify({'code': 0, 'msg': 'success', 'data': {'affected': affected}})
        else:
            return jsonify({'code': -1, 'msg': '未找到匹配的柜格'})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})
