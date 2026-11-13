"""
api_order.py — 订单与开门记录模块（3个接口）
1. POST /api/opendoor/createOpenDoorRecord
2. POST /api/order/findOrderByPwd
3. POST /api/order/finishOrder
"""
import sqlite3
from flask import Blueprint, request, jsonify

order_bp = Blueprint('order', __name__)


def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 接口1: createOpenDoorRecord ----------
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
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS door_records ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'device_id TEXT, '
            'board_no INTEGER, '
            'lock_no INTEGER, '
            'order_id TEXT, '
            'open_type TEXT, '
            'create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        )

        cursor.execute(
            'INSERT INTO door_records (device_id, board_no, lock_no, order_id, open_type) '
            'VALUES (%s, %s, %s, %s, %s)',
            (device_id, board_no, lock_no, order_id, open_type)
        )
        conn.commit()
        record_id = cursor.lastrowid
        conn.close()

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {'record_id': record_id}
        })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })


# ---------- 接口2: findOrderByPwd ----------
@order_bp.route('/order/findOrderByPwd', methods=['POST'])
def find_order_by_pwd():
    try:
        data = request.get_json(force=True)
        pickup_code = data.get('pickup_code', '')

        if not pickup_code:
            return jsonify({
                'code': -1,
                'msg': 'pickup_code不能为空'
            })

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS orders ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'order_no TEXT, '
            'pickup_code TEXT, '
            'device_id TEXT, '
            'board_no INTEGER, '
            'lock_no INTEGER, '
            'status TEXT DEFAULT "待取件", '
            'create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        )

        cursor.execute(
            'SELECT * FROM orders WHERE pickup_code=%s ORDER BY id DESC LIMIT 1',
            (pickup_code,)
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({
                'code': 0,
                'msg': 'success',
                'data': dict(row)
            })
        else:
            return jsonify({
                'code': -1,
                'msg': '未找到该取件码对应的订单'
            })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })


# ---------- 接口3: finishOrder ----------
@order_bp.route('/order/finishOrder', methods=['POST'])
def finish_order():
    try:
        data = request.get_json(force=True)
        order_id = data.get('order_id', '')
        order_no = data.get('order_no', '')

        if not order_id and not order_no:
            return jsonify({
                'code': -1,
                'msg': 'order_id或order_no不能为空'
            })

        conn = get_db()
        cursor = conn.cursor()

        if order_id:
            cursor.execute(
                'UPDATE orders SET status="已完成" WHERE id=%s',
                (order_id,)
            )
        else:
            cursor.execute(
                'UPDATE orders SET status="已完成" WHERE order_no=%s',
                (order_no,)
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
                'msg': '未找到匹配的订单'
            })
    except Exception as e:
        return jsonify({
            'code': -1,
            'msg': str(e)
        })
@order_bp.route("/order/<int:order_id>/store/end", methods=["POST"])
def store_end(order_id):
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
        slot_id = dict(row).get("slot_id")
        if slot_id:
            c.execute("UPDATE cabinet_slots SET status=1 WHERE id=%s", (slot_id,))
        c.execute("UPDATE orders SET status=3, retrieve_time=CURRENT_TIMESTAMP WHERE id=%s", (order_id,))
        conn.commit()
        conn.close()
        return jsonify({"code": 0, "msg": "success", "data": {"order_id": order_id}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": -1, "msg": "end order failed: " + str(e)})
