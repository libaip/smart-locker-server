"""api_order.py — 订单与开门记录模块（3个接口）"""
import sqlite3
from flask import Blueprint, request, jsonify
order_bp = Blueprint('order', __name__)
def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn

@order_bp.route('/opendoor/createOpenDoorRecord', methods=['POST'])
def create_open_door_record():
    try:
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        board_no = data.get('board_no', 0)
        lock_no = data.get('lock_no', 0)
        order_id = data.get('order_id', '')
        open_type = data.get('open_type', '')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS door_records (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, board_no INTEGER, lock_no INTEGER, order_id TEXT, open_type TEXT, create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        cursor.execute('INSERT INTO door_records (device_id, board_no, lock_no, order_id, open_type) VALUES (%s, %s, %s, %s, %s)', (device_id, board_no, lock_no, order_id, open_type))
        conn.commit()
        record_id = cursor.lastrowid
        conn.close()
        return jsonify({'code': 0, 'msg': 'success', 'data': {'record_id': record_id}})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})

@order_bp.route('/order/findOrderByPwd', methods=['POST'])
def find_order_by_pwd():
    try:
        data = request.get_json(force=True)
        access_code = data.get('access_code', '')
        if not access_code:
            return jsonify({'code': -1, 'msg': 'access_code不能为空'})
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT, access_code TEXT, device_id TEXT, board_no INTEGER, lock_no INTEGER, status TEXT DEFAULT "待取件", create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        cursor.execute('SELECT * FROM orders WHERE access_code=%s ORDER BY id DESC LIMIT 1', (access_code,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return jsonify({'code': 0, 'msg': 'success', 'data': dict(row)})
        else:
            return jsonify({'code': -1, 'msg': '未找到该取件码对应的订单'})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})

@order_bp.route('/order/finishOrder', methods=['POST'])
def finish_order():
    try:
        data = request.get_json(force=True)
        order_id = data.get('order_id', '')
        order_no = data.get('order_no', '')
        if not order_id and not order_no:
            return jsonify({'code': -1, 'msg': 'order_id或order_no不能为空'})
        conn = get_db()
        cursor = conn.cursor()
        if order_id:
            cursor.execute('UPDATE orders SET status="已完成" WHERE id=%s', (order_id,))
        else:
            cursor.execute('UPDATE orders SET status="已完成" WHERE order_no=%s', (order_no,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        if affected > 0:
            return jsonify({'code': 0, 'msg': 'success', 'data': {'affected': affected}})
        else:
            return jsonify({'code': -1, 'msg': '未找到匹配的订单'})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})
