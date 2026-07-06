"""
api_netpoint.py — 网点设备查询模块
POST /api/netpoint/findDeviceInfo
"""
import sqlite3
from flask import Blueprint, request, jsonify

netpoint_bp = Blueprint('netpoint', __name__)


def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn


@netpoint_bp.route('/netpoint/findDeviceInfo', methods=['POST'])
def find_device_info():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')

        if not device_id:
            return jsonify({
                'code': -1,
                'msg': 'device_id不能为空'
            })

        conn = get_db()

        # 查询设备信息
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM devices WHERE device_id=%s',
            (device_id,)
        )
        device = cursor.fetchone()

        if not device:
            conn.close()
            return jsonify({
                'code': -1,
                'msg': '设备不存在'
            })

        # 查询该设备下的所有箱子
        cursor.execute(
            'SELECT * FROM cabinets WHERE device_id=%s ORDER BY board_no, lock_no',
            (device_id,)
        )
        cabinets = [dict(row) for row in cursor.fetchall()]

        conn.close()

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {
                'device': dict(device),
                'cabinets': cabinets,
                'cabinet_count': len(cabinets)
            }
        })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })