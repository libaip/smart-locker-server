"""
api_box.py — 设备箱子模块
POST /api/box/updateDeviceBox
"""
import sqlite3
from flask import Blueprint, request, jsonify

box_bp = Blueprint('box', __name__)


def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn


@box_bp.route('/box/updateDeviceBox', methods=['POST'])
def update_device_box():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        board_no = data.get('board_no', 0)
        lock_no = data.get('lock_no', 0)
        status = data.get('status', '')

        if not all([device_id, status]):
            return jsonify({
                'code': -1,
                'msg': '参数不完整'
            })

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE cabinets SET status=%s WHERE device_id=%s AND board_no=%s AND lock_no=%s',
            (status, device_id, board_no, lock_no)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()

        if affected > 0:
            return jsonify({
                'code': 0,
                'msg': 'success',
                'data': {'affected': affected}
            })
        else:
            return jsonify({
                'code': -1,
                'msg': '未找到匹配的记录'
            })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })