# -*- coding: utf-8 -*-
# 妯℃嫙 merchant_order_detail 瀵硅鍗?77444 鐨勮繑鍥?import psycopg2, psycopg2.extras, json
from config import DATABASE_URL

def simulate(order_id):
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT o.*, c.cabinet_code, c.name as cabinet_name, l.name as location_name FROM orders o JOIN cabinets c ON o.cabinet_id = c.id JOIN locations l ON c.location_id = l.id WHERE o.id = %s", (order_id,))
    order = dict(cur.fetchone())
    out = {}
    out['order'] = {
        'id': order['id'], 'order_no': order['order_no'], 'status': order['status'],
        'user_phone': order['user_phone'], 'store_time': str(order['store_time']),
        'retrieve_time': str(order['retrieve_time']) if order.get('retrieve_time') else None,
        'deposit_amount': str(order['deposit_amount']),
        'access_code': order.get('access_code'), 'compartment_number': order.get('compartment_number'),
        'cabinet_name': order.get('cabinet_name'), 'location_name': order.get('location_name')
    }
    cur.execute("SELECT dr.*, cs.slot_label FROM door_records dr LEFT JOIN cabinet_slots cs ON dr.device_id = (SELECT mainboard_device_id FROM cabinets WHERE id = %s) AND cs.slot_number = CAST(dr.lock_no AS integer) AND cs.cabinet_id = %s WHERE dr.order_id IN (%s, %s) ORDER BY dr.create_time", (order['cabinet_id'], order['cabinet_id'], str(order_id), order.get('order_no') or ''))
    door_records = [dict(d) for d in cur.fetchall()]
    _first_proto = True
    _st = order.get('store_time') or order.get('created_at')
    _st_ts = _st.timestamp() if hasattr(_st, 'timestamp') else None
    _rt = order.get('retrieve_time')
    _rt_ts = _rt.timestamp() if hasattr(_rt, 'timestamp') else None
    for _dr in door_records:
        _ct = _dr.get('create_time')
        _ct_ts = _ct.timestamp() if hasattr(_ct, 'timestamp') else None
        if _ct_ts is None:
            continue
        _ot = _dr.get('open_type')
        if _ot in ('YBM', 'store', 'WT'):
            if _rt_ts is not None and abs(_ct_ts - _rt_ts) <= 600:
                _dr['open_type'] = 'end_storage'
            elif _first_proto:
                _first_proto = False
                _dr['open_type'] = 'store_open'
            else:
                _dr['open_type'] = 'mid_offline'
        elif _ot == 'local':
            if _rt_ts is not None and abs(_ct_ts - _rt_ts) <= 300:
                _dr['open_type'] = 'end_storage'
            else:
                _dr['open_type'] = 'mid_offline'
    out['door_records'] = [{'id': d['id'], 'open_type': d.get('open_type'), 'create_time': str(d['create_time']), 'operator': d.get('operator') or ''} for d in door_records]
    conn.close()
    return out

r = simulate(77444)
print(json.dumps(r, ensure_ascii=False, indent=1, default=str))

