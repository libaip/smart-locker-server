import psycopg2
from psycopg2.extras import RealDictCursor
import json
import os
"""
管理后台V2 API - 补全admin_v2前端所需的所有接口
包括：仪表盘统计、设备列表、订单管理、会员管理、提现管理等
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db
import threading, uuid
from helpers import json_response, manage_user_tokens, require_auth, logger, connected_devices, should_hide_order
from config import WX_API_V3_KEY, WX_MCH_ID, WX_CERT_SERIAL_NO, WX_KEY_PATH, WX_CERT_PATH, WX_MP_APP_ID, WX_MP_APP_SECRET, WX_APP_ID, WX_APP_SECRET, WX_API_KEY
def _fmt_time(t):
    """格式化时间: YYYY-MM-DD HH:MM:SS"""
    if not t:
        return ''
    if isinstance(t, datetime):
        return f'{t.year}-{t.month:02d}-{t.day:02d} {t.hour:02d}:{t.minute:02d}:{t.second:02d}'
    s = str(t)
    # 截断微秒部分
    if '.' in s:
        s = s[:s.index('.')]
    return s


def _check_phone_uniqueness(cursor, phone, exclude_table=None, exclude_id=None):
    """???????????/???/????"""
    if exclude_table and exclude_id:
        sql = """
            SELECT id, tbl FROM (
                SELECT id, 'agents' as tbl FROM agents WHERE contact_phone=%s
                UNION ALL
                SELECT id, 'merchants' as tbl FROM merchants WHERE contact_phone=%s
                UNION ALL
                SELECT id, 'employees' as tbl FROM employees WHERE phone=%s
            ) all_phones
        """
        cursor.execute(sql, (phone, phone, phone))
    else:
        sql = "SELECT COUNT(*) FROM (SELECT contact_phone FROM agents UNION SELECT contact_phone FROM merchants UNION SELECT phone FROM employees) p WHERE p.phone=%s"
        cursor.execute(sql, (phone,))
        row = cursor.fetchone()
        return row[0] > 0
    for row in cursor.fetchall():
        if row["tbl"] != exclude_table or row["id"] != exclude_id:
            return True
    return False



bp = Blueprint('admin_v2', __name__)

_door_status_results = {}
_door_status_lock = threading.Lock()



@bp.route('/admin/dashboard', methods=['GET', 'POST'])
@require_auth
def admin_dashboard():
    """主控台统计数据"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(deposit_amount),0) as amt FROM orders WHERE status NOT IN (1, 5) AND created_at >= NOW() - INTERVAL '30 days'")
        order_stat = c.fetchone()
        c.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(refund_amount),0) as amt FROM orders WHERE refund_status='refunded' AND refund_time >= NOW() - INTERVAL '30 days'")
        refund_stat = c.fetchone()
        c.execute('SELECT COUNT(*) as cnt, COALESCE(SUM(balance),0) as bal, COALESCE(SUM(total_deposited),0) as dep, COALESCE(SUM(total_withdrawn),0) as wd FROM user_balances')
        member_stat = c.fetchone()
        c.execute('SELECT COUNT(*) FROM locations WHERE status=1')
        loc_count = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM cabinets WHERE status=1')
        dev_count = c.fetchone()[0]
        # 统计在线设备：WebSocket连接 + 最近60秒有心跳的设备
        import datetime as dt_mod
        now = dt_mod.datetime.now()
        online_ids = set(connected_devices.keys())
        c.execute("SELECT mainboard_device_id, last_heartbeat FROM cabinets WHERE last_heartbeat >= NOW() - INTERVAL '60 seconds'")
        for row in c.fetchall():
            did = row['mainboard_device_id']
            if did:
                online_ids.add(did)
        online = len(online_ids)
        c.execute('SELECT COALESCE(SUM(deposit_amount),0) FROM orders WHERE status=2')
        storage_income = c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(deposit_amount),0) FROM orders WHERE status NOT IN (1, 5) AND created_at >= NOW() - INTERVAL '30 days'")
        online_income = c.fetchone()[0]
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute("SELECT COUNT(*) FROM orders WHERE date(created_at)=%s AND status NOT IN (1, 5)", (today,))
        today_orders = c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(deposit_amount),0) FROM orders WHERE date(created_at)=%s AND status NOT IN (1, 5)", (today,))
        today_amount = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM user_balances')
        user_count = c.fetchone()[0]
        conn.close()
        return json_response(data={
            'onlineIncome': f'{online_income:.2f}',
            'storageIncome': f'{storage_income:.2f}',
            'withdrawn': f'{member_stat["wd"]:.2f}' if member_stat else '0.00',
            'totalIncome': f'{online_income:.2f}',
            'memberBalance': f'{member_stat["bal"]:.2f}' if member_stat else '0.00',
            'memberPending': '0.00',
            'memberWithdrawn': f'{member_stat["wd"]:.2f}' if member_stat else '0.00',
            'memberRecharge': f'{member_stat["dep"]:.2f}' if member_stat else '0.00',
            'orderCount': order_stat['cnt'] if order_stat else 0,
            'orderAmount': f'{order_stat["amt"]:.2f}' if order_stat else '0.00',
            'refundAmount': f'{refund_stat["amt"]:.2f}' if refund_stat else '0.00',
            'orderProfit': f'{(order_stat["amt"] - refund_stat["amt"]):.2f}' if order_stat and refund_stat else '0.00',
            'userCount': user_count,
            'locationCount': loc_count,
            'deviceCount': dev_count,
            'onlineCount': online,
            'todayOrders': today_orders,
            'todayAmount': f'{today_amount:.2f}'
        })
    except Exception as e:
        logger.error(f'[dashboard] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/daily-trend', methods=['GET', 'POST'])
@require_auth
def admin_daily_trend():
    """每日趋势数据"""
    try:
        days = int(request.args.get('days', 7))
        conn = get_db()
        c = conn.cursor()
        result = []
        for i in range(days):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            c.execute('''
                SELECT COUNT(*) as cnt, COALESCE(SUM(deposit_amount),0) as amt 
                FROM orders WHERE date(created_at)=%s AND status NOT IN (0, 1, 5)
            ''', (date,))
            row = c.fetchone()
            result.insert(0, {'date': date, 'count': row['cnt'] if row else 0, 'amount': float(row['amt'] if row else 0)})
        conn.close()
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[daily-trend] {e}')
        return json_response(data=[])


@bp.route('/admin/devices', methods=['GET', 'POST'])
@require_auth
def admin_devices():
    try:
        data = request.get_json() if request.method == 'POST' else {}
        keyword = (data or {}).get('keyword', '') or request.args.get('keyword', '')
        status = (data or {}).get('status', '') or request.args.get('status', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        # Auto-deactivate cabinets with no heartbeat for 7+ days
        try:
            _dc = conn.cursor()
            _dc.execute("UPDATE cabinets SET status=0 WHERE status=1 AND last_heartbeat < NOW() - INTERVAL '7 days'")
            conn.commit()
        except:
            pass
        where, params = "1=1", []
        if keyword:
            where += ' AND (cabinet_code LIKE %s OR name LIKE %s OR mainboard_device_id LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
        if status == 'online':
            online_ids = list(connected_devices.keys())
            if online_ids:
                ph = ','.join(['%s' for _ in online_ids])
                where += f' AND mainboard_device_id IN ({ph})'
                params.extend(online_ids)
            else:
                where += ' AND 1=0'
        elif status == 'offline':
            online_ids = list(connected_devices.keys())
            if online_ids:
                ph = ','.join(['%s' for _ in online_ids])
                where += f' AND (mainboard_device_id NOT IN ({ph}) OR mainboard_device_id IS NULL OR mainboard_device_id="")'
                params.extend(online_ids)
        c.execute(f'SELECT COUNT(*) FROM cabinets WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT c.*, l.name as location_name,
            (SELECT COUNT(*) FROM cabinet_slots cs WHERE cs.cabinet_id=c.id) as total_slots,
            (SELECT serial_port FROM mainboards WHERE cabinet_id=c.id ORDER BY board_index LIMIT 1) as mb_serial_port,
            (SELECT baud_rate FROM mainboards WHERE cabinet_id=c.id ORDER BY board_index LIMIT 1) as mb_baud_rate,
            (SELECT protocol FROM mainboards WHERE cabinet_id=c.id ORDER BY board_index LIMIT 1) as mb_protocol
            FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id
            WHERE {where} ORDER BY c.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        devices = []
        import datetime as dt_mod
        now = dt_mod.datetime.now()
        for row in c.fetchall():
            d = dict(row)
            d['is_online'] = d.get('mainboard_device_id') in connected_devices
            if not d['is_online'] and d.get('last_heartbeat'):
                try:
                    hb = d['last_heartbeat']
                    # PostgreSQL 返回 datetime 对象，SQLite 返回字符串
                    if isinstance(hb, str):
                        hb = dt_mod.datetime.strptime(hb, '%Y-%m-%d %H:%M:%S')
                    d['is_online'] = (now - hb).total_seconds() < 60
                except:
                    pass
            d['serial_port'] = d.get('mb_serial_port') or 'ttyS4'
            d['baud_rate'] = d.get('mb_baud_rate') or 9600
            if d.get('mb_protocol'): d['mainboard_source'] = d['mb_protocol']
            d['app_version'] = d.get('app_version', '')
            d['app_version_code'] = d.get('app_version_code', 0) or 0
            devices.append(d)
        conn.close()
        return json_response(data={'list': devices, 'total': total})
    except Exception as e:
        logger.error(f'[admin_devices] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/cabinet/save', methods=['POST'])
@require_auth
def admin_cabinet_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('id'):
            fields = ['name','cabinet_code','location_id','mainboard_device_id','mainboard_source',
                     'total_slots','business_status','status','charge_mode',
                     'deposit_amount','per_use_price','customer_phone',
                     'usage_rules']
            sets, params = [], []
            for f in fields:
                if f in data:
                    v = data[f]
                    if isinstance(v, bool):
                        v = 1 if v else 0
                    elif f in ('deposit_amount','per_use_price') and (v == '' or v is None):
                        v = 0
                    sets.append(f'{f}=%s')
                    params.append(v)
            params.append(data['id'])
            c.execute(f'UPDATE cabinets SET {",".join(sets)},updated_at=CURRENT_TIMESTAMP WHERE id=%s', params)
        else:
            cabinet_code = data.get('cabinet_code') or f'CAB{datetime.now().strftime("%Y%m%d%H%M%S")}'
            c.execute("""INSERT INTO cabinets (cabinet_code,name,location_id,mainboard_device_id,mainboard_source,
                total_slots,business_status,status,charge_mode,deposit_amount,
                customer_phone,per_use_price,usage_rules) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (cabinet_code, data.get("name",""), int(data.get("location_id")) if data.get("location_id") and str(data.get("location_id")).strip() else None, data.get("mainboard_device_id"),
                 data.get("mainboard_source","WT"),
                 data.get("total_slots",12), data.get("business_status","open"),
                 data.get("status",1), data.get("charge_mode","deposit"),
                 float(data.get("deposit_amount") or 20),
                 data.get("customer_phone",""),
                 float(data.get("per_use_price") or 0),
                 data.get("usage_rules") or "24h"))
            data['id'] = c.fetchone()[0]
        _sp = data.get('serial_port') or ''
        _br = data.get('baud_rate') or ''
        _pr = data.get('mainboard_source') or ''
        if data.get('id') and (_sp or _br or _pr):
            c.execute('SELECT id FROM mainboards WHERE cabinet_id=%s', (data['id'],))
            row = c.fetchone()
            if row:
                _upd, _up = [], []
                # 协议改变时自动匹配默认串口/波特率
                if _pr and not _sp:
                    _def_sp = 'ttyS3' if _pr.upper() == 'WT' else 'ttyS4'
                    _upd.append('serial_port=%s')
                    _up.append(_def_sp)
                if _pr and not _br:
                    _def_br = 115200 if _pr.upper() == 'WT' else 9600
                    _upd.append('baud_rate=%s')
                    _up.append(int(_def_br))
                if _sp:
                    _upd.append('serial_port=%s')
                    _up.append(_sp)
                if _br:
                    _upd.append('baud_rate=%s')
                    _up.append(int(_br))
                if _pr:
                    _upd.append('protocol=%s')
                    _up.append(_pr)
                if _upd:
                    _up.append(data['id'])
                    c.execute(f"UPDATE mainboards SET {','.join(_upd)} WHERE cabinet_id=%s", _up)
            else:
                _def_sp = 'ttyS3' if (_pr or '').upper() == 'WT' else 'ttyS4'
                _def_br = 115200 if (_pr or '').upper() == 'WT' else 9600
                c.execute('INSERT INTO mainboards (cabinet_id, board_index, slot_count, serial_port, baud_rate, protocol) VALUES (%s,1,16,%s,%s,%s)',
                         (data['id'], _sp or _def_sp, _br or _def_br, _pr or 'YBM'))
        elif data.get('id') and not (_sp or _br or _pr) and data.get('mainboard_source',''):
            c.execute('SELECT id FROM mainboards WHERE cabinet_id=%s', (data['id'],))
            if not c.fetchone():
                _pr2 = data.get('mainboard_source','YBM')
                c.execute('INSERT INTO mainboards (cabinet_id, board_index, slot_count, protocol) VALUES (%s,1,16,%s)',
                         (data['id'], _pr2))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[cabinet_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/cabinet/delete', methods=['POST'])
@require_auth
def admin_cabinet_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM cabinets WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[cabinet_delete] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/force-update', methods=['POST'])
@require_auth
def admin_force_update():
    """推送设备更新 - 写入pending_lock_cmds，设备轮询时拾取"""
    try:
        conn_apk = get_db()
        c_apk = conn_apk.cursor()
        c_apk.execute("SELECT version_name, version_code, download_url FROM apk_version ORDER BY version_code DESC LIMIT 1")
        apk_row = c_apk.fetchone()
        conn_apk.close()
        if not apk_row:
            return json_response(message="未找到APK版本信息，请先上传APK", code=400)
        latest_url = apk_row["download_url"]
        latest_ver = apk_row["version_name"]
        latest_code = apk_row["version_code"]

        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '')
        if device_id:
            conn3 = get_db()
            c3 = conn3.cursor()
            c3.execute('SELECT id FROM cabinets WHERE mainboard_device_id=%s', (device_id,))
            cab = c3.fetchone()
            if cab:
                import json as _json
                # 获取MD5
                latest_md5 = ''
                try:
                    c_apk2 = conn_apk.cursor()
                    c_apk2.execute('SELECT file_md5 FROM apk_version ORDER BY version_code DESC LIMIT 1')
                    md5_row = c_apk2.fetchone()
                    if md5_row: latest_md5 = md5_row.get('file_md5', '') or ''
                    c_apk2.close()
                except: pass
                cmd = _json.dumps({'type': 'force_update', 'download_url': latest_url, 'version_name': latest_ver, 'version_code': latest_code, 'force': True, 'file_md5': latest_md5})
                c3.execute('INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, status) VALUES (%s,%s,%s,%s)', (device_id, cab['id'], cmd, 'pending'))
                conn3.commit()
                logger.info(f'[force_update] OK cabinet={cab["id"]} version={latest_ver}')
            else:
                logger.warning(f'[force_update] device_id={device_id} not found')
            conn3.close()
        else:
            logger.warning('[force_update] no device_id in request')
        return json_response(message='已推送更新指令')
    except Exception as e:
        logger.error(f'[force_update] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/reset-activation', methods=['POST'])
@require_auth
def admin_reset_activation():
    """重置设备激活状态"""
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE cabinets SET activated=0 WHERE id=%s', (data.get('cabinet_id'),))
        conn.commit()
        conn.close()
        return json_response(message='已重置激活状态')
    except Exception as e:
        logger.error(f'[reset_activation] {e}')
        return json_response(message=str(e), code=500)


# ============ Mainboards ============

@bp.route('/admin/slots', methods=['GET', 'POST'])
@require_auth
def admin_slots():
    """获取柜门列表"""
    try:
        data = request.get_json() if request.method == 'POST' else {}
        cabinet_id = data.get('cabinet_id') or request.args.get('cabinet_id')
        conn = get_db()
        c = conn.cursor()
        if cabinet_id:
            c.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s ORDER BY slot_number', (cabinet_id,))
        else:
            c.execute('SELECT * FROM cabinet_slots ORDER BY cabinet_id, slot_number')
        slots = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': slots})
    except Exception as e:
        logger.error(f'[slots] {e}')
        return json_response(data={'list': []})


@bp.route('/admin/slot/save', methods=['POST'])
@require_auth
def admin_slot_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        logger.info(f'[slot_save] BEFORE: slot_id={data.get("id")}, status={data.get("status")}')
        c.execute('UPDATE cabinet_slots SET slot_size=%s,status=%s,slot_label=%s WHERE id=%s',
                  (data.get('slot_size'), data.get('status'), data.get('slot_label', ''), data['id']))
        logger.info(f'[slot_save] AFTER: affected={c.rowcount}, slot_id={data["id"]}')
        c.execute('SELECT cabinet_id FROM cabinet_slots WHERE id=%s', (data["id"],))
        _cab_row = c.fetchone()
        if _cab_row: logger.info(f'[slot_save] cabinet_id={_cab_row["cabinet_id"]}')
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[slot_save] {e}')
        return json_response(message=str(e), code=500)




@bp.route('/admin/slots/batch-label', methods=['POST'])
@require_auth
def admin_slots_batch_label():
    """批量设置柜门标签：根据字母前缀+编号自动生成slot_label"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        prefix = data.get('prefix', '').strip()
        start_num = data.get('start_num', 1)
        
        if not cabinet_id:
            return json_response(message='缺少柜体ID', code=400)
        if not prefix:
            return json_response(message='请输入字母前缀', code=400)
        
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id, slot_number FROM cabinet_slots WHERE cabinet_id=%s ORDER BY slot_number', (cabinet_id,))
        slots = c.fetchall()
        
        if not slots:
            conn.close()
            return json_response(message='该柜体没有柜门', code=400)
        
        updated = 0
        for i, slot in enumerate(slots):
            label = prefix + str(start_num + i)
            c.execute('UPDATE cabinet_slots SET slot_label=%s WHERE id=%s', (label, slot[0]))
            updated += 1
        
        conn.commit()
        conn.close()
        return json_response(message=f'已批量设置{updated}个柜门标签', data={'updated': updated})
    except Exception as e:
        logger.error(f'[batch_label] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/slots/open-all', methods=['POST'])
@require_auth
def admin_slots_open_all():
    """全开柜门"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        if not cabinet_id:
            return json_response(message='缺少柜体ID', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT cs.slot_number, cs.status, c.mainboard_device_id FROM cabinets c JOIN cabinet_slots cs ON cs.cabinet_id = c.id WHERE c.id = %s AND cs.status NOT IN (3, 4)', (cabinet_id,))
        rows = c.fetchall()
        conn.close()
        if not rows:
            return json_response(message='没有可开的正常柜门', code=400)
        from helpers import send_open_lock
        did = str(rows[0]['mainboard_device_id'])
        opened = []
        for r in rows:
            send_open_lock(did, 1, r['slot_number'], None, '')
            opened.append(r['slot_number'])
        return json_response(message=f'已发送{len(opened)}个柜门开锁指令')
        return json_response(message='已发送全开指令')
    except Exception as e:
        logger.error(f'[open_all] {e}')
        return json_response(message=str(e), code=500)


# ============ Locations ============

@bp.route('/admin/locations', methods=['GET', 'POST'])
@require_auth
def admin_locations():
    try:
        data = request.get_json() if request.method == 'POST' else {}
        keyword = (data or {}).get('keyword', '') or request.args.get('keyword', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if keyword:
            where += ' AND (l.name LIKE %s OR l.address LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%'])
        c.execute(f'SELECT COUNT(*) FROM locations l WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT l.*, m.name as merchant_name,
            (SELECT COUNT(*) FROM cabinets WHERE location_id=l.id) as cabinet_count,
            (SELECT COUNT(*) FROM cabinets WHERE location_id=l.id AND last_heartbeat>=NOW() - INTERVAL '5 minutes') as online_count,
            (SELECT COUNT(*) FROM orders WHERE slot_id IN (SELECT id FROM cabinet_slots WHERE cabinet_id IN (SELECT id FROM cabinets WHERE location_id=l.id)) AND status=2) as active_orders
            FROM locations l LEFT JOIN merchants m ON l.merchant_id=m.id
            WHERE {where} ORDER BY l.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        locations = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': locations, 'total': total})
    except Exception as e:
        logger.error(f'[admin_locations] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/location/save', methods=['POST'])
@require_auth
def admin_location_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('id'):
            fields = ['name','address','longitude','latitude','merchant_id','status',
                     'contact_name','contact_phone','open_time','close_time',
                     'allow_slot_select','slot_assign_mode','allow_mid_retrieve','retrieve_mode',
                     'allow_h5_to_mp','show_qr_follow','force_follow_mp','h5_url',
                     'show_slot_count','screen_show_title','screen_title',
                     'slot_full_alert','slot_full_text','end_alert_minutes',
                     'enable_clear_box','clear_box_time','clear_box_cycle',
                     'deposit_random','deposit_min','deposit_max',
                     'withdraw_enabled','show_refunding_status','refund_mode','withdraw_mode',
                     'auto_approve_day',
                     'auto_approve_time','auto_approve_rate','click_free_count',
                     'anti_test_minutes','anti_test_auto_refund','hide_ratio',
                     'whitelist_phones','duplicate_filter_enabled','duplicate_filter_days','duplicate_filter_limit',
                     'refund_approve_rate','refund_approve_start_min','refund_approve_end_min',
                     'balance_hide_enabled','balance_hide_days']
            sets, params = [], []
            for f in fields:
                if f in data:
                    v = data[f]
                    if isinstance(v, bool):
                        v = 1 if v else 0
                    elif f in ('deposit_amount','per_use_price') and (v == '' or v is None):
                        v = 0
                    sets.append(f'{f}=%s')
                    params.append(v)
            params.append(data['id'])
            c.execute(f'UPDATE locations SET {",".join(sets)} WHERE id=%s', params)
        else:
            c.execute('''INSERT INTO locations (name,address,longitude,latitude,merchant_id,status,
                contact_name,contact_phone) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                (data.get('name'), data.get('address'), data.get('longitude'),
                 data.get('latitude'), data.get('merchant_id'), data.get('status',1),
                 data.get('contact_name',''), data.get('contact_phone','')))
        # 如果网点切换为自动审批，自动处理该网点pending的提现记录
        if data.get('id') and data.get('withdraw_mode') == 'auto_approve':
            try:
                c2 = conn.cursor()
                # 找到该网点下所有status=0(待审核)的余额提现记录
                c2.execute('''SELECT w.id, w.user_phone, w.amount, w.openid FROM withdrawal_records w
                    JOIN orders o ON w.order_id = o.id
                    JOIN cabinets cb ON o.cabinet_id = cb.id
                    WHERE cb.location_id = %s AND w.status = 0 AND w.order_id IS NOT NULL''', (data['id'],))
                pending_records = c2.fetchall()
                # 也找余额提现(order_id IS NULL)属于该网点的
                c2.execute('''SELECT w.id, w.user_phone, w.amount, w.openid FROM withdrawal_records w
                    WHERE w.order_id IS NULL AND w.status = 0 AND w.user_phone IN (
                        SELECT DISTINCT o.user_phone FROM orders o
                        JOIN cabinets cb ON o.cabinet_id = cb.id
                        WHERE cb.location_id = %s
                    )''', (data['id'],))
                pending_records += c2.fetchall()
                for wr in pending_records:
                    try:
                        refund_success = False
                        if wr['order_id']:
                            # 有关联订单，走原路退款
                            from helpers import do_real_refund
                            refund_success, refund_id, refund_msg = do_real_refund(order_id=wr['order_id'], amount=wr['amount'], openid=wr.get('openid',''))
                        else:
                            # 余额提现，找最近订单原路退
                            c2.execute('SELECT id FROM orders WHERE user_phone=%s AND status IN (2,4,5,6) AND deposit_amount > 0 ORDER BY id DESC LIMIT 1', (wr['user_phone'],))
                            recent = c2.fetchone()
                            if recent:
                                from helpers import do_real_refund
                                refund_success, refund_id, refund_msg = do_real_refund(order_id=recent['id'], amount=wr['amount'], openid=wr.get('openid',''))
                        if refund_success or (" 订单已全额退款" in str(refund_msg)):
                            c2.execute('UPDATE withdrawal_records SET status=2 WHERE id=%s', (wr['id'],))
                            logger.info(f'[auto_approve_switch] 提现{wr["id"]}原路退款成功')
                        else:
                            c2.execute('UPDATE withdrawal_records SET status=1 WHERE id=%s', (wr['id'],))
                            logger.info(f'[auto_approve_switch] 提现{wr["id"]}退款处理中')
                    except Exception as re:
                        logger.error(f'[auto_approve_switch] 提现{wr["id"]}自动退款失败: {re}')
                conn.commit()
            except Exception as ae:
                logger.error(f'[auto_approve_switch] 处理pending提现异常: {ae}')

        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[location_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/location/delete', methods=['POST'])
@require_auth
def admin_location_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM locations WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[location_delete] {e}')
        return json_response(message=str(e), code=500)


# ============ Orders ============

@bp.route('/admin/orders', methods=['GET', 'POST'])
@require_auth
def admin_orders():
    try:
        data = request.get_json() if request.method == 'POST' else request.args.to_dict()
        order_id = data.get('order_id', '') or data.get('order_no', '')
        phone = data.get('phone', '') or data.get('user_phone', '')
        status = data.get('status', '')
        page = int(data.get('page', 1))
        page_size = int(data.get('limit', 20))
        conn = get_db()
        c = conn.cursor()
        where, params = "o.status NOT IN (5)", []
        if order_id:
            where += ' AND o.order_no LIKE %s'
            params.append(f'%{order_id}%')
        if phone:
            where += ' AND o.user_phone LIKE %s'
            params.append(f'%{phone}%')
        if status:
            where += ' AND o.status = %s'
            params.append(int(status))
        if data.get('logic_mark'):
            where += ' AND o.logic_mark = %s'
            params.append(data['logic_mark'])
        if data.get('refund_mark'):
            where += ' AND o.refund_mark = %s'
            params.append(data['refund_mark'])
        if data.get('location_id'):
            where += ' AND c.location_id = %s'
            params.append(data['location_id'])
        if data.get('merchant_id'):
            where += ' AND l.merchant_id = %s'
            params.append(data['merchant_id'])
        if data.get('device_id'):
            where += ' AND o.cabinet_id = %s'
            params.append(data['device_id'])
        if data.get('wechat_name'):
            where += ' AND (ub.wechat_name LIKE %s OR up.wechat_name LIKE %s)'
            params.extend(['%' + data['wechat_name'] + '%', '%' + data['wechat_name'] + '%'])
        # Date range filter, default 30 days
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        if start_date and end_date:
            where += ' AND date(o.created_at)>=%s AND date(o.created_at)<=%s'
            params.extend([start_date, end_date])
        elif not start_date and not end_date:
            # Default: last 30 days
            where += " AND o.created_at>=NOW() - interval '30 days'"
        c.execute(f'SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone LEFT JOIN phone_openids po ON o.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f"""SELECT o.id, o.order_no, o.user_phone, o.access_code as password, o.compartment_number, o.deposit_amount, CASE WHEN o.status=4 THEN COALESCE(o.refund_amount,0) ELSE 0 END as refund_amount, o.status,
            o.store_time, o.retrieve_time, o.created_at, o.group_id, o.cabinet_code,
            o.transaction_id, o.pay_time, o.refund_time, o.refund_mark, o.logic_mark,
            COALESCE(NULLIF(ub.wechat_name,''), NULLIF(po.wechat_name,''), up.wechat_name) as wechat_name,""" + f"""
            l.id as location_id, l.name as location_name, m.name as merchant_name, m.id as merchant_id, pc.mch_id as pay_mch_id
            FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
            LEFT JOIN cabinet_slots cs ON o.slot_id=cs.id
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone
            LEFT JOIN phone_openids po ON o.user_phone=po.phone
            LEFT JOIN user_profiles up ON po.openid=up.openid
            LEFT JOIN locations l ON c.location_id=l.id
            LEFT JOIN merchants m ON l.merchant_id=m.id
            LEFT JOIN payment_channels pc ON o.payment_channel_id = pc.id
            WHERE {where} ORDER BY o.created_at DESC LIMIT %s OFFSET %s""",
                  params + [page_size, (page-1)*page_size])
        orders = []
        for r in c.fetchall():
            d = dict(r)
            d['status_text'] = {1:'待支付',2:'使用中',3:'可退款',4:'已退款',5:'已取消',6:'退款异常'}.get(d.get('status'), '未知')
            d['created_at'] = _fmt_time(d.get('created_at'))
            d['retrieve_time'] = _fmt_time(d.get('retrieve_time'))
            # 计算订单是否被 hide_ratio 隐藏
            if d.get('logic_mark') != 'N' and d.get('merchant_id') and d.get('location_id'):
                # 查询网点 hide_ratio
                c2 = conn.cursor()
                c2.execute('SELECT hide_ratio, whitelist_phones FROM locations WHERE id = %s', (d['location_id'],))
                loc = c2.fetchone()
                if loc and loc['hide_ratio'] and loc['hide_ratio'] > 0:
                    whitelist = set((loc['whitelist_phones'] or '').split(',')) if loc['whitelist_phones'] else set()
                    if should_hide_order(d['merchant_id'], d['id'], d.get('user_phone', ''), loc['hide_ratio'], whitelist, d.get('logic_mark')):
                        d['logic_mark'] = 'Y'
            orders.append(d)
        conn.close()
        return json_response(data={'list': orders, 'total': total})
    except Exception as e:
        logger.error(f'[admin_orders] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/order/detail', methods=['GET', 'POST'])
@require_auth
def admin_order_detail():
    """订单详情(含开门记录)"""
    try:
        data = request.get_json() if request.method == 'POST' else request.args.to_dict()
        order_id = data.get('order_id')
        conn = get_db()
        c = conn.cursor()
        c.execute('''SELECT o.*, o.access_code as password, c.cabinet_code, c.name as cabinet_name,
            COALESCE(NULLIF(ub.wechat_name,''), NULLIF(po.wechat_name,''), up.wechat_name) as wechat_name,
            l.id as location_id, l.name as location_name, m.name as merchant_name, m.id as merchant_id, pc.mch_id as pay_mch_id
            FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
            LEFT JOIN cabinet_slots cs ON o.slot_id=cs.id
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone
            LEFT JOIN phone_openids po ON o.user_phone=po.phone
            LEFT JOIN user_profiles up ON po.openid=up.openid
            LEFT JOIN locations l ON c.location_id=l.id
            LEFT JOIN merchants m ON l.merchant_id=m.id
            LEFT JOIN payment_channels pc ON o.payment_channel_id = pc.id
            WHERE o.id=%s''', (order_id,))
        order = c.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        order_dict = dict(order)
        # 查询该订单关联的开门记录
        order_no = order_dict.get("order_no", "")
        open_logs = []
        # 直接按order_no查door_records
        if order_no:
            c.execute("SELECT id, device_id, board_no, lock_no, order_id, open_type, create_time FROM door_records WHERE order_id IN (%s, %s) ORDER BY create_time DESC", (str(order_no), str(order_id)))
            door_logs = [dict(r) for r in c.fetchall()]
            for log in door_logs:
                log["source"] = "door"
                ct = log.get("create_time")
                if hasattr(ct, "strftime"):
                    log["create_time"] = ct.strftime("%Y-%m-%d %H:%M:%S")
                log["created_at"] = log.get("create_time", "")
        else:
            door_logs = []
        # 查remote_open_logs(通过device_id+slot_id，限定订单时间范围)
        slot_id = order_dict.get("slot_id") or 0
        if slot_id:
            dev_row = c.execute("SELECT c.mainboard_device_id FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id=c.id WHERE cs.id=%s", (slot_id,)).fetchone()
            if dev_row and dev_row["mainboard_device_id"]:
                device_id = dev_row["mainboard_device_id"]
                # 只查询订单时间范围内的开门记录
                store_t = order_dict.get('created_at')
                retrieve_t = order_dict.get('retrieve_time')
                if store_t and retrieve_t:
                    c.execute("SELECT id, action_type, operator, result, success, ip_address, created_at, device_id, slot_id FROM remote_open_logs WHERE device_id=%s AND slot_id=%s AND created_at>=%s AND created_at<=%s ORDER BY created_at DESC", (device_id, slot_id, store_t, retrieve_t))
                elif store_t:
                    c.execute("SELECT id, action_type, operator, result, success, ip_address, created_at, device_id, slot_id FROM remote_open_logs WHERE device_id=%s AND slot_id=%s AND created_at>=%s ORDER BY created_at DESC", (device_id, slot_id, store_t))
                else:
                    c.execute("SELECT id, action_type, operator, result, success, ip_address, created_at, device_id, slot_id FROM remote_open_logs WHERE device_id=%s AND slot_id=%s ORDER BY created_at DESC", (device_id, slot_id))
                remote_logs = [dict(r) for r in c.fetchall()]
                for log in remote_logs:
                    log["source"] = "remote"
                    log["open_type"] = log.get("action_type", "")
            else:
                remote_logs = []
        else:
            remote_logs = []
        # 格式化开门记录时间
        for log in door_logs:
            log["create_time"] = _fmt_time(log.get("create_time"))
            log["created_at"] = log["create_time"]  # 统一字段用于排序
        for log in remote_logs:
            log["created_at"] = _fmt_time(log.get("created_at"))
        # 合并并按时间倒序
        open_logs = door_logs + remote_logs
        open_logs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        order_dict['open_logs'] = open_logs
        # 格式化时间字段
        order_dict['created_at'] = _fmt_time(order_dict.get('created_at'))
        order_dict['retrieve_time'] = _fmt_time(order_dict.get('retrieve_time'))
        return json_response(data=order_dict)
    except Exception as e:
        logger.error(f'[order_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/order/refund', methods=['POST'])
@require_auth
def admin_order_refund():
    """订单退款 - 支持使用中(2)和已结算(4)的订单"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        conn = get_db()
        c = conn.cursor()
        # 支持status=2(使用中)和status=3(可退款)的订单退款
        c.execute('SELECT * FROM orders WHERE id=%s AND status IN (2,3)', (order_id,))
        order = c.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在或状态不允许退款', code=400)
        order_dict = dict(order)
        amount = order_dict.get('deposit_amount', 0)
        transaction_id = order_dict.get('transaction_id', '')
        order_no = order_dict.get('order_no', '')
        payment_channel_id = order_dict.get('payment_channel_id')
        refund_no = 'RF' + datetime.now().strftime('%Y%m%d%H%M%S') + str(order_id)
        # 尝试调用微信退款API
        refund_result = None
        actual_refund = False
        if transaction_id and transaction_id != 'MOCK':
            try:
                from helpers import get_channel_wxpay, get_wxpay
                if payment_channel_id:
                    c.execute('SELECT * FROM payment_channels WHERE id=%s', (payment_channel_id,))
                    ch = c.fetchone()
                    if ch:
                        wxpay_inst, _ = get_channel_wxpay(dict(ch))
                    else:
                        return json_response(message='订单关联的商户渠道不存在，无法退款', code=400)
                else:
                    # 没有渠道ID，选一个活跃的
                    c.execute('SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1')
                    active_ch = c.fetchone()
                    if active_ch:
                        wxpay_inst, _ = get_channel_wxpay(dict(active_ch))
                    else:
                        return json_response(message='无可用活跃商户，无法退款', code=400)
                total_fee = int(amount * 100)
                refund_result = wxpay_inst.refund(
                    out_trade_no=order_no,
                    total_fee=total_fee,
                    refund_fee=total_fee,
                    out_refund_no=refund_no,
                    refund_desc=''
                )
                if refund_result and refund_result.get('return_code') == 'SUCCESS' and refund_result.get('result_code') == 'SUCCESS':
                    actual_refund = True
                    logger.info(f'[order_refund] 微信退款成功 order={order_no} refund_no={refund_no}')
                else:
                    err_msg = (refund_result.get('err_code_des') or refund_result.get('err_code') or refund_result.get('return_msg') or '未知错误') if refund_result else '无返回'
                    # 已全额退款视为成功（之前退款成功但本地DB未更新的场景）
                    if refund_result and '已全额退款' in str(refund_result.get('err_code_des') or ''):
                        actual_refund = True
                        logger.info(f'[order_refund] 微信已全额退款，同步本地状态 order={order_no}')
                    else:
                        logger.warning(f'[order_refund] 微信退款失败 order={order_no} err={err_msg}')
                        conn.close()
                        return json_response(message=f'微信退款失败: {err_msg}', code=400)
            except Exception as e:
                logger.warning(f'[order_refund] 微信退款异常 order={order_no} err={e}')
                conn.close()
                return json_response(message=f'微信退款异常: {str(e)}', code=400)
        # 微信退款成功或无transaction_id(MOCK)，才更新本地状态
        c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                  (amount, order_id))
        # 联动更新待审核的提现记录
        c.execute("UPDATE withdrawal_records SET status=2, approver='管理员', approve_time=CURRENT_TIMESTAMP WHERE order_id=%s AND status=0", (order_id,))
        # 记录payments退款流水
        c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, %s)',
                  (order_id, amount, transaction_id, refund_no, datetime.now()))
        # 如果订单是已结算(3)，保证金已从余额退过，需要扣回余额
        if order_dict.get('status') == 3 and order_dict.get('user_phone'):
            # 统一用 mp_openid 查找
            _rv_mp = None
            c.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (order_dict['user_phone'],))
            _rv_row = c.fetchone()
            if _rv_row:
                _rv_mp = _rv_row['mp_openid']
            if _rv_mp:
                c.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE mp_openid = %s',
                          (amount, amount, _rv_mp))
            else:
                c.execute('UPDATE user_balances SET balance = balance - %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s',
                          (amount, amount, order_dict['user_phone']))
            # 同步更新 balance_details 状态，防止双重退款
            c.execute("UPDATE user_balance_details SET status='clawed_back' WHERE order_id=%s AND status='available'", (order_id,))
        # 如果订单还在使用中(2)，释放柜格
        # [Agent-modified 2026-07-04] 退款时释放格口：无论订单是使用中(2)还是已结算(3)，都要释放格口为空闲(0)
        if order_dict.get('status') in (2, 3) and order_dict.get('slot_id'):
            c.execute('UPDATE cabinet_slots SET status=1 WHERE id=%s', (order_dict['slot_id'],))
        conn.commit()
        
        
        conn.close()
        return json_response(message='退款成功')
    except Exception as e:
        logger.error(f'[order_refund] {e}')
        return json_response(message=str(e), code=500)







@bp.route('/admin/order/close', methods=['POST'])
@require_auth
def admin_order_close():
    """结束订单 - 支持待支付(1)和使用中(2)的订单"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='缺少order_id', code=400)
        from config import DATABASE_URL as _DU
        conn = psycopg2.connect(_DU, connect_timeout=10)
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('SELECT * FROM orders WHERE id=%s AND status IN (1,2)', (order_id,))
        order = c.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在或状态不允许结束', code=400)
        order_dict = dict(order)
        from datetime import datetime as dt_mod2
        now = dt_mod2.now().strftime('%Y-%m-%d %H:%M:%S')
        # 更新订单状态为已结束(3)，保证金退到余额
        c.execute('UPDATE orders SET status=3, retrieve_time=%s, pickup_time=%s, updated_at=%s, refund_mark=1 WHERE id=%s',
                   (now, now, now, order_id))
        # 释放柜格
        if order_dict.get('slot_id'):
            c.execute('UPDATE cabinet_slots SET status=1 WHERE id=%s', (order_dict['slot_id'],))
        # 保证金退到用户余额
        deposit_amount = order_dict.get('deposit_amount', 0)
        if deposit_amount > 0 and order_dict.get("user_phone"):
            # 统一用 mp_openid 查找用户余额
            _cl_mp_openid = order_dict.get('mp_openid', '') or order_dict.get('openid', '')
            if not _cl_mp_openid:
                c.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (order_dict['user_phone'],))
                _cl_r = c.fetchone()
                if _cl_r and _cl_r['mp_openid']:
                    _cl_mp_openid = _cl_r['mp_openid']
            c.execute('SELECT id FROM user_balances WHERE mp_openid = %s', (_cl_mp_openid,))
            ub_row = c.fetchone()
            if not ub_row:
                c.execute('SELECT id FROM user_balances WHERE phone = %s', (order_dict['user_phone'],))
                ub_row = c.fetchone()
            if ub_row:
                c.execute("UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s, mp_openid = COALESCE(NULLIF(mp_openid, ''), %s), openid = COALESCE(NULLIF(openid, ''), %s), unionid = COALESCE(NULLIF(unionid, ''), %s) WHERE id = %s",
                          (deposit_amount, deposit_amount, _cl_mp_openid, order_dict.get('openid', ''), order_dict.get('unionid', ''), ub_row['id']))
            else:
                c.execute('INSERT INTO user_balances (phone, openid, unionid, mp_openid, balance, total_deposited, total_withdrawn, first_use_time) VALUES (%s, %s, %s, %s, %s, %s, 0, NOW())',
                          (order_dict['user_phone'], order_dict.get('openid', ''), order_dict.get('unionid', ''), _cl_mp_openid, deposit_amount, deposit_amount))
            # 写入余额明细（灰度：新提现逻辑）
            c.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING",
                      (order_dict['user_phone'], order_id, deposit_amount))
        conn.commit()
        
        # 发送寄存结束订阅消息
        if order_dict.get('openid'):
            try:
                from helpers import send_wx_subscribe_message
                subscribe_data = {
                    'amount6': {'value': '¥{:.2f}'.format(deposit_amount)},
                    'time4': {'value': now},
                    'thing7': {'value': '已退还至小程序用户钱包'},
                    'thing2': {'value': '请自行点击此通知消息跳转“我的钱包”提现'}
                }
                send_wx_subscribe_message(order_dict['openid'], '5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A', subscribe_data, phone=order_dict.get('user_phone'))
                # 退款通知在用户提现时发送，不在结束寄存时发送
            except Exception as e:
                logger.error(f"[order_close发送订阅消息失败] {e}")
        
        conn.close()
        # 通知APK刷新柜格状态
        try:
            device_id = order_dict.get('device_id') or order_dict.get('cabinet_id')
            if device_id:
                # 尝试从cabinet获取mainboard_device_id
                conn2 = get_db()
                c2 = conn2.cursor()
                if order_dict.get('cabinet_id'):
                    c2.execute('SELECT mainboard_device_id FROM cabinets WHERE id=%s', (order_dict['cabinet_id'],))
                    cab = c2.fetchone()
                    if cab:
                        device_id = cab['mainboard_device_id']
                conn2.close()
                from helpers import connected_devices
                import json as _json2
                ws = connected_devices.get(str(device_id))
                if ws:
                    ws.send(_json2.dumps({'type': 'slot_update', 'slot_id': order_dict.get('slot_id'), 'status': 1}))
                    logger.info(f'[order_close] 已通知设备{device_id}刷新柜格')
        except Exception as notify_err:
            logger.warning(f'[order_close] 通知APK失败: {notify_err}')
        return json_response(message='订单已结束')
    except Exception as e:
        logger.error(f'[order_close] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/member/refund', methods=['POST'])
@require_auth
def admin_member_refund():
    """单个会员退款"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        amount = data.get('amount', 0)
        if not phone or amount <= 0:
            return json_response(message='参数错误', code=400)
        conn = get_db()
        c = conn.cursor()
        # 统一用 mp_openid 查找
        c.execute("SELECT * FROM user_balances WHERE mp_openid = (SELECT mp_openid FROM user_balances WHERE phone=%s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1) LIMIT 1", (phone,))
        user = c.fetchone()
        if not user:
            c.execute('SELECT * FROM user_balances WHERE phone=%s', (phone,))
            user = c.fetchone()
        if not user or (user['balance'] or 0) <= 0:
            conn.close()
            return json_response(message='用户余额不足', code=400)
        refund_amount = min(amount, user['balance'] or 0)
        _m_mp = user.get('mp_openid') or ''
        if _m_mp:
            c.execute('UPDATE user_balances SET balance=balance-%s, total_withdrawn=total_withdrawn+%s WHERE mp_openid=%s', (refund_amount, refund_amount, _m_mp))
        else:
            c.execute('UPDATE user_balances SET balance=balance-%s, total_withdrawn=total_withdrawn+%s WHERE phone=%s', (refund_amount, refund_amount, phone))
        c.execute("SELECT id, order_no, transaction_id, deposit_amount, payment_channel_id FROM orders WHERE user_phone=%s AND status != 4 AND refund_status!='refunded' ORDER BY id DESC LIMIT 1", (phone,))
        order = c.fetchone()
        refund_no = 'RF_M' + datetime.now().strftime('%Y%m%d%H%M%S') + str(phone)[-4:]
        if order:
            # 先尝试微信退款，成功才更新本地状态
            wx_refund_ok = False
            wx_err_msg = ''
            try:
                from helpers import get_channel_wxpay, get_wxpay
                payment_channel_id = order['payment_channel_id'] if order else None
                if payment_channel_id:
                    c.execute('SELECT * FROM payment_channels WHERE id=%s', (payment_channel_id,))
                    ch = c.fetchone()
                    if ch:
                        wxpay_inst, _ = get_channel_wxpay(dict(ch))
                    else:
                        wxpay_inst = None
                        wx_err_msg = '订单关联的商户渠道不存在'
                else:
                    c.execute('SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1')
                    active_ch = c.fetchone()
                    if active_ch:
                        wxpay_inst, _ = get_channel_wxpay(dict(active_ch))
                    else:
                        wxpay_inst = None
                        wx_err_msg = '无可用活跃商户'
                total_fee = int(refund_amount * 100)
                if not wxpay_inst:
                    wx_err_msg = wx_err_msg or '无可用支付实例'
                    logger.error(f'[member_refund] {wx_err_msg}')
                else:
                    refund_result = wxpay_inst.refund(out_trade_no=order['order_no'], total_fee=total_fee, refund_fee=total_fee, out_refund_no=refund_no, refund_desc='')
                if refund_result and refund_result.get('return_code') == 'SUCCESS' and refund_result.get('result_code') == 'SUCCESS':
                    wx_refund_ok = True
                else:
                    wx_err_msg = (refund_result.get('err_code_des') or refund_result.get('err_code') or refund_result.get('return_msg') or '未知错误') if refund_result else '无返回'
                    # 已全额退款视为成功
                    if refund_result and '已全额退款' in str(refund_result.get('err_code_des') or ''):
                        wx_refund_ok = True
                        logger.info(f'[member_refund] 微信已全额退款，同步本地状态 order={order.get("order_no", "")}')
                    else:
                        logger.warning(f'[member_refund] 微信退款失败 err={wx_err_msg}')
            except Exception as e:
                wx_err_msg = str(e)
                logger.warning(f'[member_refund] 微信退款异常 err={e}')
            if not wx_refund_ok and order['transaction_id'] and order['transaction_id'] != 'MOCK':
                conn.close()
                return json_response(message=f'微信退款失败: {wx_err_msg}', code=400)
            c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                      (order['deposit_amount'], order['id']))
            c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, %s)',
                      (order['id'], refund_amount, order['transaction_id'], refund_no, datetime.now()))
        conn.commit()
        conn.close()
        return json_response(message=f'退款成功 ¥{refund_amount:.2f}')
    except Exception as e:
        logger.error(f'[member_refund] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/member/batch-refund', methods=['POST'])
@require_auth
def admin_member_batch_refund():
    """批量会员退款"""
    try:
        data = request.get_json()
        phones = data.get('phones', [])
        if not phones:
            return json_response(message='请选择会员', code=400)
        conn = get_db()
        c = conn.cursor()
        success_count = 0
        total_refund = 0
        for phone in phones:
            c.execute('SELECT * FROM user_balances WHERE phone=%s', (phone,))
            user = c.fetchone()
            if not user or (user['balance'] or 0) <= 0:
                continue
            refund_amount = user['balance'] or 0
            _b_mp = user.get('mp_openid') or ''
            if _b_mp:
                c.execute('UPDATE user_balances SET balance=0, total_withdrawn=total_withdrawn+balance WHERE mp_openid=%s', (_b_mp,))
            else:
                c.execute('UPDATE user_balances SET balance=0, total_withdrawn=total_withdrawn+balance WHERE phone=%s', (phone,))
            c.execute("SELECT id, order_no, transaction_id, deposit_amount, payment_channel_id FROM orders WHERE user_phone=%s AND status != 4 AND refund_status!='refunded' ORDER BY id DESC LIMIT 1", (phone,))
            order = c.fetchone()
            refund_no = 'RF_B' + datetime.now().strftime('%Y%m%d%H%M%S') + str(phone)[-4:]
            if order:
                c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                          (order['deposit_amount'], order['id']))
                c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, %s)',
                          (order['id'], refund_amount, order['transaction_id'], refund_no, datetime.now()))
            success_count += 1
            total_refund += refund_amount
        conn.commit()
        conn.close()
        return json_response(message=f'批量退款完成: {success_count}人, 共¥{total_refund:.2f}')
    except Exception as e:
        logger.error(f'[batch_refund] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/admin/order/open-lock', methods=['POST'])
@require_auth
def admin_order_open_lock():
    """远程开锁"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        conn = get_db()
        c = conn.cursor()
        c.execute('''SELECT o.*, c.mainboard_device_id, cs.slot_number, cs.board_no, cs.lock_no FROM orders o 
            JOIN cabinets c ON o.cabinet_id=c.id 
            LEFT JOIN cabinet_slots cs ON o.slot_id=cs.id
            WHERE o.id=%s''', (order_id,))
        order = c.fetchone()
        conn.close()
        if not order or not order['mainboard_device_id']:
            return json_response(message='订单或设备不存在', code=404)
        order = dict(order)
        device_id = str(order['mainboard_device_id'])
        board_no = order.get('board_no') or 1
        lock_no = order.get('lock_no') or (order.get('slot_number', 1) - (order.get('board_no', 1) - 1) * 16) or 1
        from helpers import send_open_lock
        send_open_lock(device_id, board_no, lock_no, None, order.get('order_no', str(order_id)))
        return json_response(message='开柜指令已发送')
    except Exception as e:
        logger.error(f'[open_lock] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/order/toggle-logic', methods=['POST'])
@require_auth
def admin_order_toggle_logic():
    """切换订单逻辑标记"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        logic_mark = data.get('logic_mark', 'N')
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET logic_mark=%s WHERE id=%s', (logic_mark, order_id))
        conn.commit()
        conn.close()
        return json_response(message='已切换')
    except Exception as e:
        logger.error(f'[toggle_logic] {e}')
        return json_response(message=str(e), code=500)


# ============ Members ============

@bp.route('/admin/members', methods=['GET', 'POST'])
@require_auth
def admin_members():
    try:
        data = request.get_json() if request.method == 'POST' else {}
        phone = (data or {}).get('phone', '') or request.args.get('phone', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if phone:
            where += ' AND phone LIKE %s'
            params.append(f'%{phone}%')
        c.execute(f'SELECT COUNT(*) FROM user_balances WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT ub.*, 
            (SELECT COUNT(*) FROM orders WHERE user_phone=ub.phone) as total_orders,
            (SELECT COALESCE(SUM(deposit_amount),0) FROM orders WHERE user_phone=ub.phone AND deposit_amount>0) as total_deposit
            FROM user_balances ub WHERE {where} ORDER BY ub.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        members = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': members, 'total': total})
    except Exception as e:
        logger.error(f'[admin_members] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/member/detail', methods=['GET', 'POST'])
@require_auth
def admin_member_detail():
    """会员详情"""
    try:
        data = request.get_json() if request.method == 'POST' else request.args.to_dict()
        user_id = data.get('user_id')
        phone = data.get('phone')
        conn = get_db()
        c = conn.cursor()
        if user_id:
            c.execute('SELECT * FROM user_balances WHERE id=%s', (user_id,))
        elif phone:
            c.execute('SELECT * FROM user_balances WHERE phone=%s', (phone,))
        else:
            conn.close()
            return json_response(message='缺少参数', code=400)
        member = c.fetchone()
        if member:
            c.execute('''SELECT order_no, created_at, status FROM orders 
                WHERE user_phone=%s ORDER BY created_at DESC LIMIT 10''', (member['phone'],))
            recent_orders = [dict(r) for r in c.fetchall()]
            conn.close()
            result = dict(member)
            result['recent_orders'] = recent_orders
            result['total_orders'] = len(recent_orders)
            return json_response(data=result)
        conn.close()
        return json_response(message='会员不存在', code=404)
    except Exception as e:
        logger.error(f'[member_detail] {e}')
        return json_response(message=str(e), code=500)


# ============ Withdrawals ============

@bp.route('/admin/withdrawals', methods=['GET', 'POST'])
@require_auth
def admin_withdrawals():
    """提现列表"""
    try:
        data = request.get_json() if request.method == 'POST' else {}
        status = (data or {}).get('status', '') or request.args.get('status', '')
        search = (data or {}).get('search', '') or request.args.get('search', '')
        order_no = (data or {}).get('order_no', '') or request.args.get('order_no', '')
        wechat_name = (data or {}).get('wechat_name', '') or request.args.get('wechat_name', '')
        date_start = (data or {}).get('date_start', '') or request.args.get('date_start', '')
        date_end = (data or {}).get('date_end', '') or request.args.get('date_end', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if status:
            where += ' AND wr.status=%s'
            params.append(int(status))
        if search:
            where += ' AND wr.user_phone LIKE %s'
            params.append(f'%%{search}%%')
        if order_no:
            where += ' AND o.order_no LIKE %s'
            params.append(f'%%{order_no}%%')
        if wechat_name:
            where += ' AND (ub.wechat_name LIKE %s OR up.wechat_name LIKE %s)'
            params.extend([f'%%{wechat_name}%%', f'%%{wechat_name}%%'])
        if date_start:
            where += ' AND wr.created_at >= %s'
            params.append(date_start)
        if date_end:
            where += ' AND wr.created_at <= %s'
            params.append(date_end + ' 23:59:59')
        c.execute(f'SELECT COUNT(*) FROM withdrawal_records wr LEFT JOIN orders o ON wr.order_id=o.id LEFT JOIN (SELECT phone, MAX(wechat_name) as wechat_name FROM user_balances GROUP BY phone) ub ON wr.user_phone=ub.phone LEFT JOIN phone_openids po ON wr.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f"SELECT wr.*, o.order_no, COALESCE(NULLIF(ub.wechat_name,''), NULLIF(po.wechat_name,''), up.wechat_name, '') as wechat_name FROM withdrawal_records wr LEFT JOIN orders o ON wr.order_id=o.id LEFT JOIN (SELECT phone, MAX(wechat_name) as wechat_name FROM user_balances GROUP BY phone) ub ON wr.user_phone=ub.phone LEFT JOIN phone_openids po ON wr.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where} ORDER BY wr.created_at DESC LIMIT %s OFFSET %s",
                  params + [page_size, (page-1)*page_size])
        withdrawals = []
        orders = []
        for r in c.fetchall():
            d = dict(r)
            d['created_at'] = _fmt_time(d.get('created_at'))
            d['approve_time'] = _fmt_time(d.get('approve_time'))
            withdrawals.append(d)
        conn.close()
        return json_response(data={'list': withdrawals, 'total': total})
    except Exception as e:
        logger.error(f'[withdrawals] {e}')
        return json_response(data={'list': [], 'total': 0})

@bp.route('/admin/withdrawal/approve', methods=['POST'])
@require_auth
def admin_withdrawal_approve():
    """审批通过提现申请（status=0 -> 真退款 -> status=2或1）"""
    try:
        data = request.get_json()
        withdrawal_id = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT w.*, o.payment_channel_id, o.order_no FROM withdrawal_records w LEFT JOIN orders o ON w.order_id = o.id WHERE w.id=%s', (withdrawal_id,))
        wd = c.fetchone()
        if not wd:
            conn.close()
            return json_response(message='提现记录不存在', code=400)
        if wd['status'] != 0:
            conn.close()
            return json_response(message='该记录状态不允许审批', code=400)
        amount = wd['amount']
        phone = wd['user_phone']
        order_id = wd['order_id']
        # 解析打包的订单ID列表
        import json as _json
        order_ids_str = wd.get('order_ids') or '[]'
        try:
            order_ids_list = _json.loads(order_ids_str) if order_ids_str else []
        except:
            order_ids_list = []
        # 如果有order_ids（打包提现），对每个订单退款
        if order_ids_list and len(order_ids_list) > 0:
            from helpers import do_real_refund
            all_ok = True
            for oid in order_ids_list:
                c.execute('SELECT deposit_amount, COALESCE(refund_amount,0) as refund_amount FROM orders WHERE id=%s', (oid,))
                od = c.fetchone()
                if od:
                    refund_this = float(od['deposit_amount']) - float(od['refund_amount'])
                    if refund_this > 0.001:
                        ok, rid, rmsg = do_real_refund(order_id=oid, amount=refund_this, payment_channel_id=wd.get('payment_channel_id'))
                        if ok:
                            c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount=COALESCE(refund_amount,0)+%s WHERE id=%s', (rid, refund_this, oid))
                            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                        else:
                            all_ok = False
            c.execute('UPDATE withdrawal_records SET status=%s, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (2 if all_ok else 1, session.get('admin_username', 'admin'), withdrawal_id))
            conn.commit()
            conn.close()
            return json_response(message='审批通过，退款已处理' if all_ok else '审批通过，部分退款失败')
        # 兼容旧逻辑：单个order_id
        # 余额已在用户提现时扣除，无需再次扣除
        # 统一用 mp_openid 查找
        _ap_mp = None
        c.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (phone,))
        _ap_r = c.fetchone()
        if _ap_r:
            _ap_mp = _ap_r['mp_openid']
        if _ap_mp:
            c.execute("UPDATE user_balances SET balance = GREATEST(COALESCE(balance,0) - %s, 0), total_withdrawn = COALESCE(total_withdrawn,0) + %s WHERE mp_openid = %s", (amount, amount, _ap_mp))
        else:
            c.execute("UPDATE user_balances SET balance = GREATEST(COALESCE(balance,0) - %s, 0), total_withdrawn = COALESCE(total_withdrawn,0) + %s WHERE phone = %s", (amount, amount, phone))
        # 真正退款/转账
        refund_success = False
        refund_id = ''
        if order_id:
            # 检查是否已退过款
            _check_refund = c.execute('SELECT refund_status FROM orders WHERE id=%s', (order_id,))
            _refund_row = c.fetchone()
            if _refund_row and _refund_row[0] == 'refunded':
                refund_success = True
                refund_id = 'BALANCE_' + str(order_id)
                refund_msg = '订单已退款，余额已计入'
            else:
                # 订单押金退款
                from helpers import do_real_refund
                refund_success, refund_id, refund_msg = do_real_refund(order_id=order_id, amount=amount, payment_channel_id=wd.get('payment_channel_id'))
        else:
            # 余额提现：无需微信退款，余额已在上面扣除
            refund_success = True
            refund_id = 'BALANCE_' + datetime.now().strftime('%Y%m%d%H%M%S')
            refund_msg = '余额提现成功'
        if refund_success or (" 订单已全额退款" in str(refund_msg)):
            c.execute('UPDATE withdrawal_records SET status=2, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (session.get('admin_username', 'admin'), withdrawal_id))
            if order_id:
                c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=%s WHERE id=%s', (refund_id, datetime.now(), order_id))
                c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (order_id,))
        else:
            c.execute('UPDATE withdrawal_records SET status=1, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (session.get('admin_username', 'admin'), withdrawal_id))
            if order_id:
                c.execute("UPDATE orders SET status=3, refund_status='refunded', refund_mark=1 WHERE id=%s", (order_id,))
        conn.commit()
        conn.close()
        if refund_success or (" 订单已全额退款" in str(refund_msg)):
            # 发送审批通过订阅消息
            try:
                from helpers import send_wx_subscribe_message
                # 获取用户openid
                _conn2 = get_db()
                _c2 = _conn2.cursor()
                _c2.execute("SELECT mp_openid FROM user_balances WHERE phone=%s AND mp_openid IS NOT NULL LIMIT 1", (phone,))
                _usr = _c2.fetchone()
                _conn2.close()
                if _usr and _usr['mp_openid']:
                    from datetime import datetime as dt_notify
                    wd_data = {
                        'amount8': {'value': '¥{:.2f}'.format(amount)},
                        'time6': {'value': dt_notify.now().strftime('%Y-%m-%d %H:%M:%S')},
                        'thing3': {'value': '原路退回支付账户'},
                        'thing2': {'value': '预计1-3个工作日到账，请耐心等待'}
                    }
                    send_wx_subscribe_message(_usr['mp_openid'], 'YsfB8FH4eMrISAS92oUzBhoXe178AnxP8XSA0_24YoE', wd_data, phone=phone, page='pages/mine/mine')
            except Exception as _notify_err:
                logger.error(f'[withdrawal_approve] 发送订阅消息失败: {_notify_err}')
            return json_response(message='审批通过，退款已完成')
        else:
            return json_response(message='审批通过，但退款失败，请手动确认退款')
    except Exception as e:
        logger.error('[withdrawal_approve] ' + str(e))
        return json_response(message=str(e), code=500)




@bp.route("/admin/recharge-records", methods=["GET", "POST"])
@require_auth
def admin_recharge_records():
    """会员充值记录"""
    try:
        from helpers import _fmt_time
        data = request.get_json() if request.method == "POST" else {}
        search = (data or {}).get("search", "") or request.args.get("search", "")
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 10)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if search:
            where += " AND bd.user_phone LIKE %s"
            params.append(f"%%{search}%%")
        c.execute("SELECT COUNT(*) FROM user_balance_details bd WHERE " + where, params)
        total = c.fetchone()[0]
        c.execute("SELECT bd.user_phone, bd.amount, o.transaction_id, bd.created_at FROM user_balance_details bd LEFT JOIN orders o ON bd.order_id = o.id WHERE " + where + " ORDER BY bd.id DESC LIMIT %s OFFSET %s",
                  params + [page_size, (page - 1) * page_size])
        rows = [dict(r) for r in c.fetchall()]
        for r in rows:
            r["create_time"] = _fmt_time(r.pop("created_at"))
            r["amount"] = float(r["amount"])
        conn.close()
        return json_response(data={"list": rows, "total": total})
    except Exception as e:
        logger.error("[admin_recharge_records] %s", str(e))
        return json_response(data={"list": [], "total": 0}, code=500)

@bp.route('/admin/withdrawal/reject', methods=['POST'])
@require_auth
def admin_withdrawal_reject():
    """拒绝提现"""
    try:
        data = request.get_json()
        withdrawal_id = data.get('id')
        reason = data.get('reason', '')
        conn = get_db()
        c = conn.cursor()
        # 查询提现记录
        c.execute('SELECT user_phone, amount, status, order_ids FROM withdrawal_records WHERE id=%s', (withdrawal_id,))
        wd = c.fetchone()
        if not wd:
            conn.close()
            return json_response(message='提现记录不存在', code=400)
        # 已扣余额的记录需要退还
        if wd['status'] in (0, 1) and wd['user_phone']:
            # 统一用 mp_openid 查找
            _cn_mp = None
            c.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (wd['user_phone'],))
            _cn_r = c.fetchone()
            if _cn_r:
                _cn_mp = _cn_r['mp_openid']
            if _cn_mp:
                c.execute('UPDATE user_balances SET balance=balance+%s,total_withdrawn=total_withdrawn-%s WHERE mp_openid=%s',
                          (wd['amount'], wd['amount'], _cn_mp))
            else:
                c.execute('UPDATE user_balances SET balance=balance+%s,total_withdrawn=total_withdrawn-%s WHERE phone=%s',
                          (wd['amount'], wd['amount'], wd['user_phone']))
        # 解析打包的订单ID
        import json as _json
        order_ids_str = wd.get('order_ids') or '[]'
        try:
            order_ids_list = _json.loads(order_ids_str) if order_ids_str else []
        except:
            order_ids_list = []
        # 恢复余额明细状态为available
        if order_ids_list:
            c.execute("UPDATE user_balance_details SET status='available' WHERE order_id = ANY(%s) AND status='pending'", (order_ids_list,))
        else:
            c.execute("SELECT order_id FROM withdrawal_records WHERE id=%s", (withdrawal_id,))
            wd2 = c.fetchone()
            if wd2 and wd2['order_id']:
                c.execute("UPDATE orders SET status=3 WHERE id=%s AND status!=4", (wd2['order_id'],))
                c.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (wd2['order_id'],))
        c.execute("UPDATE withdrawal_records SET status=3,approver=%s WHERE id=%s",
                  (session.get('admin_username', 'admin') + (':' + reason if reason else ''), withdrawal_id))
        conn.commit()
        conn.close()
        # 发送拒绝订阅消息（退回余额）
        try:
            from helpers import send_wx_subscribe_message
            if wd and wd.get('user_phone'):
                _conn3 = get_db()
                _c3 = _conn3.cursor()
                _c3.execute("SELECT mp_openid FROM user_balances WHERE phone=%s AND mp_openid IS NOT NULL LIMIT 1", (wd['user_phone'],))
                _usr3 = _c3.fetchone()
                _conn3.close()
                if _usr3 and _usr3['mp_openid']:
                    from datetime import datetime as dt_notify
                    wd_reject_data = {
                        'amount8': {'value': '¥{:.2f}'.format(wd['amount'])},
                        'time6': {'value': dt_notify.now().strftime('%Y-%m-%d %H:%M:%S')},
                        'thing3': {'value': '提现申请已拒绝'},
                        'thing2': {'value': '金额已退回到您的余额，可继续使用'}
                    }
                    send_wx_subscribe_message(_usr3['mp_openid'], 'YsfB8FH4eMrISAS92oUzBhoXe178AnxP8XSA0_24YoE', wd_reject_data, phone=wd['user_phone'], page='pages/mine/mine')
        except Exception as _notify_err2:
            logger.error(f'[withdrawal_reject] 发送订阅消息失败: {_notify_err2}')
        return json_response(message='已拒绝')
    except Exception as e:
        logger.error(f'[withdrawal_reject] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/admin/withdrawal/confirm-refund', methods=['POST'])
@require_auth
def admin_withdrawal_confirm_refund():
    """确认退款完成（status=1 -> status=2）"""
    try:
        data = request.get_json()
        withdrawal_id = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id, status FROM withdrawal_records WHERE id=%s', (withdrawal_id,))
        wd = c.fetchone()
        if not wd:
            conn.close()
            return json_response(message='提现记录不存在', code=400)
        if wd['status'] != 1:
            conn.close()
            return json_response(message='该记录状态不允许确认退款', code=400)
        # 确认退款完成：检查是否有关联订单需要更新
        c.execute('SELECT order_id FROM withdrawal_records WHERE id=%s', (withdrawal_id,))
        wd2 = c.fetchone()
        if wd2 and wd2['order_id']:
            c.execute('UPDATE orders SET status=4, refund_time=%s WHERE id=%s AND status!=4', (datetime.now(), wd2['order_id']))
        c.execute("UPDATE withdrawal_records SET status=2, approve_time=CURRENT_TIMESTAMP WHERE id=%s", (withdrawal_id,))
        conn.commit()
        conn.close()
        return json_response(message='已确认退款完成')
    except Exception as e:
        logger.error(f'[withdrawal_confirm_refund] {e}')
        return json_response(message=str(e), code=500)

# ============ Complaints ============

@bp.route('/admin/complaints', methods=['GET', 'POST'])
@require_auth
def admin_complaints():
    """投诉列表"""
    try:
        data = request.get_json() if request.method == 'POST' else {}
        complaint_type = (data or {}).get('type', '') or request.args.get('type', '')
        status = (data or {}).get('status', '') or request.args.get('status', '')
        phone = (data or {}).get('phone', '') or request.args.get('phone', '')
        order_no = (data or {}).get('order_no', '') or request.args.get('order_no', '')
        start_date = (data or {}).get('start_date', '') or request.args.get('start_date', '')
        end_date = (data or {}).get('end_date', '') or request.args.get('end_date', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if complaint_type:
            where += ' AND c.complaint_type=%s'
            params.append(complaint_type)
        if status:
            where += ' AND c.status=%s'
            params.append(int(status))
        if phone:
            where += ' AND c.user_phone LIKE %s'
            params.append(f'%{phone}%')
        if order_no:
            where += ' AND o.order_no LIKE %s'
            params.append(f'%{order_no}%')
        if start_date:
            where += ' AND c.created_at >= %s'
            params.append(start_date)
        if end_date:
            where += ' AND c.created_at < %s::date + INTERVAL \'1 day\''
            params.append(end_date)
        c.execute(f'SELECT COUNT(*) FROM complaints c LEFT JOIN orders o ON c.order_id=o.id WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT c.*, o.order_no, o.user_phone, pc.mch_id
            FROM complaints c LEFT JOIN orders o ON c.order_id=o.id LEFT JOIN payment_channels pc ON o.payment_channel_id=pc.id
            WHERE {where} ORDER BY c.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        complaints = [dict(r) for r in c.fetchall()]
        conn.close()
        for comp in complaints:
            for key in ['created_at', 'reply_time']:
                if key in comp and hasattr(comp[key], 'strftime'):
                    comp[key] = comp[key].strftime('%Y-%m-%d %H:%M:%S')
            if 'status' in comp:
                try:
                    comp['status'] = int(comp['status'])
                except (ValueError, TypeError):
                    pass
        return json_response(data={'list': complaints, 'total': total})
    except Exception as e:
        logger.error(f'[complaints] {e}')
        return json_response(data={'list': [], 'total': 0})


@bp.route('/admin/complaint/reply', methods=['POST'])
@require_auth
def admin_complaint_reply():
    """回复投诉"""
    try:
        data = request.get_json()
        complaint_id = data.get('id')
        reply = data.get('reply', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE complaints SET reply=%s,status=1,reply_time=CURRENT_TIMESTAMP WHERE id=%s',
                  (reply, complaint_id))
        conn.commit()
        conn.close()
        return json_response(message='回复成功')
    except Exception as e:
        logger.error(f'[complaint_reply] {e}')
        return json_response(message=str(e), code=500)


# ============ Agents ============

@bp.route('/admin/agents', methods=['GET', 'POST'])
@require_auth
def admin_agents():
    try:
        data = request.get_json() if request.method == 'POST' else {}
        keyword = (data or {}).get('keyword', '') or request.args.get('keyword', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if keyword:
            where += ' AND (name LIKE %s OR contact_name LIKE %s OR contact_phone LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
        c.execute(f'SELECT COUNT(*) FROM agents WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT a.*, a.is_locked, (SELECT COUNT(*) FROM merchants WHERE agent_id=a.id) as merchant_count
            FROM agents a WHERE {where} ORDER BY a.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        agents = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': agents, 'total': total})
    except Exception as e:
        logger.error(f'[admin_agents] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/agent/save', methods=['POST'])
@require_auth
def admin_agent_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('contact_phone') and _check_phone_uniqueness(c, data["contact_phone"], "agents" if data.get("id") else None, data.get("id")):
            conn.close()
            return json_response(message="该手机号已被其他商家/代理商/员工使用", code=400)
        if data.get('id'):
            fields = ['name','contact_name','contact_phone','status','commission_rate']
            sets, params = [], []
            for f in fields:
                if f in data:
                    sets.append(f'{f}=%s')
                    params.append(data[f])
            if data.get('password'):
                sets.append('password_hash=%s')
                params.append(generate_password_hash(data['password']))
                sets.append('plain_password=%s')
                params.append(data['password'])
            params.append(data['id'])
            c.execute(f'UPDATE agents SET {",".join(sets)} WHERE id=%s', params)
        else:
            if not data.get('name') or not data.get('contact_phone'):
                conn.close()
                return json_response(message='参数不完整', code=400)
            pwd = data.get('password') or 'Agt@' + ''.join(random.choices(string.ascii_letters + string.digits, k=2))
            c.execute('INSERT INTO agents (name, contact_name, contact_phone, password_hash, commission_rate, plain_password) VALUES (%s,%s,%s,%s,%s,%s)',
                      (data['name'], data.get('contact_name',''), data['contact_phone'], generate_password_hash(pwd), data.get('commission_rate', 0), pwd))
        conn.commit()
        conn.close()
        resp_data = None
        if not data.get('id'):
            resp_data = {'password': pwd}
        elif data.get('password'):
            resp_data = {'password': data['password']}
        return json_response(data=resp_data, message='保存成功')
        return resp
    except Exception as e:
        logger.error(f'[agent_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/agent/delete', methods=['POST'])
@require_auth
def admin_agent_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM agents WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[agent_delete] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/admin/agent/stats', methods=['GET'])
@require_auth
def admin_agent_stats():
    try:
        agent_id = request.args.get('agent_id', type=int)
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        if not agent_id:
            return json_response(message='missing agent_id', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id, name, commission_rate FROM agents WHERE id=%s', (agent_id,))
        agent = c.fetchone()
        if not agent:
            conn.close()
            return json_response(message='agent not found', code=404)
        agent_dict = dict(agent)
        rate = agent_dict.get('commission_rate', 0) or 0
        c.execute('SELECT id FROM merchants WHERE agent_id=%s', (agent_id,))
        merchant_ids = [r[0] for r in c.fetchall()]
        empty_result = json_response(data={
            'agent': agent_dict,
            'total_deposit': 0, 'total_refund': 0, 'total_unreturned': 0,
            'platform_commission': 0, 'agent_income': 0,
            'order_count': 0, 'active_order_count': 0
        })
        if not merchant_ids:
            conn.close()
            return empty_result
        date_where = ''
        date_params = []
        if start_date:
            date_where += " AND o.created_at >= %s"
            date_params.append(start_date + ' 00:00:00')
        if end_date:
            date_where += " AND o.created_at <= %s"
            date_params.append(end_date + ' 23:59:59')
        m_ph = ','.join(['%s'] * len(merchant_ids))
        c.execute('SELECT id FROM locations WHERE merchant_id IN (' + m_ph + ')', merchant_ids)
        location_ids = [r[0] for r in c.fetchall()]
        if not location_ids:
            conn.close()
            return empty_result
        l_ph = ','.join(['%s'] * len(location_ids))
        c.execute('SELECT id FROM cabinets WHERE location_id IN (' + l_ph + ')', location_ids)
        cabinet_ids = [r[0] for r in c.fetchall()]
        if not cabinet_ids:
            conn.close()
            return empty_result
        c_ph = ','.join(['%s'] * len(cabinet_ids))
        sql = 'SELECT COUNT(*) as order_count, COALESCE(SUM(deposit_amount),0) as total_deposit, COALESCE(SUM(CASE WHEN status=4 THEN refund_amount ELSE 0 END),0) as total_refund, COALESCE(SUM(deposit_amount - CASE WHEN status=4 THEN refund_amount ELSE 0 END),0) as total_unreturned FROM orders o WHERE o.cabinet_id IN (' + c_ph + ') ' + date_where
        c.execute(sql, cabinet_ids + date_params)
        row = c.fetchone()
        order_count = row[0]
        total_deposit = row[1] or 0
        total_refund = row[2] or 0
        total_unreturned = row[3] or 0
        c.execute(f'SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone LEFT JOIN phone_openids po ON o.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
        c.execute(sql2, cabinet_ids + date_params)
        active_order_count = c.fetchone()[0]
        conn.close()
        platform_commission = round(total_unreturned * rate / 100, 2)
        agent_income = round(total_unreturned - platform_commission, 2)
        return json_response(data={
            'agent': agent_dict,
            'total_deposit': round(total_deposit, 2),
            'total_refund': round(total_refund, 2),
            'total_unreturned': round(total_unreturned, 2),
            'platform_commission': platform_commission,
            'agent_income': agent_income,
            'order_count': order_count,
            'active_order_count': active_order_count
        })
    except Exception as e:
        logger.error('[agent_stats] %s', e)
        return json_response(message=str(e), code=500)


# ============ Merchants ============

@bp.route('/admin/merchants', methods=['GET', 'POST'])
@require_auth
def admin_merchants():
    try:
        data = request.get_json() if request.method == 'POST' else {}
        keyword = (data or {}).get('keyword', '') or request.args.get('keyword', '')
        agent_id = (data or {}).get('agent_id', '') or request.args.get('agent_id', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if keyword:
            where += ' AND (m.name LIKE %s OR m.contact_name LIKE %s OR m.contact_phone LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
        if agent_id:
            where += ' AND m.agent_id=%s'
            params.append(agent_id)
        c.execute(f'SELECT COUNT(*) FROM merchants m WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT m.id, m.name, m.merchant_number, m.contact_name, m.contact_phone,
            m.agent_id, m.status, m.is_locked, m.commission_per_order, m.permissions,
            m.text_labels, m.dashboard_config, m.plain_password, m.login_attempts,
            m.auth_token,
            to_char(m.created_at, 'YYYY-MM-DD HH24:MI:SS') as created_at,
            to_char(m.last_login_at, 'YYYY-MM-DD HH24:MI:SS') as last_login_at,
            a.name as agent_name,
            (SELECT COUNT(*) FROM locations WHERE merchant_id=m.id) as location_count,
            (SELECT COUNT(*) FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id=m.id)) as device_count,
            (SELECT COUNT(*) FROM orders WHERE cabinet_id IN (SELECT id FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id=m.id))) as order_count,
            COALESCE((SELECT SUM(deposit_amount) FROM orders WHERE cabinet_id IN (SELECT id FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id=m.id))),0) as total_revenue
            FROM merchants m LEFT JOIN agents a ON m.agent_id=a.id
            WHERE {where} ORDER BY m.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        merchants = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': merchants, 'total': total})
    except Exception as e:
        logger.error(f'[admin_merchants] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/merchant/save', methods=['POST'])
@require_auth
def admin_merchant_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('contact_phone') and _check_phone_uniqueness(c, data["contact_phone"], "merchants" if data.get("id") else None, data.get("id")):
            conn.close()
            return json_response(message="该手机号已被其他商家/代理商/员工使用", code=400)
        if data.get('id'):
            fields = ['name','merchant_number','contact_name','contact_phone','agent_id','status','permissions','commission_per_order','text_labels','dashboard_config']
            _int_fields = {'agent_id'}
            sets, params = [], []
            for f in fields:
                if f in data:
                    sets.append(f'{f}=%s')
                    val = data[f]
                    if f in _int_fields and val == '':
                        val = None
                    params.append(val)
            if data.get('password'):
                sets.append('password_hash=%s')
                params.append(generate_password_hash(data['password']))
                sets.append('plain_password=%s')
                params.append(data['password'])
            params.append(data['id'])
            c.execute(f'UPDATE merchants SET {",".join(sets)} WHERE id=%s', params)
        else:
            if not data.get('name') or not data.get('contact_phone'):
                conn.close()
                return json_response(message='参数不完整', code=400)
            pwd = data.get('password') or 'Mch@' + ''.join(__import__('random').choices(__import__('string').ascii_letters + __import__('string').digits, k=2))
            c.execute('INSERT INTO merchants (name, merchant_number, contact_name, contact_phone, password_hash, agent_id, permissions, plain_password, commission_per_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (data['name'], data.get('merchant_number',''), data.get('contact_name',''), data['contact_phone'], generate_password_hash(pwd), (data.get('agent_id') or None), data.get('permissions','[]'), pwd, data.get('commission_per_order', 0)))
        conn.commit()
        conn.close()
        resp_data = None
        if not data.get('id'):
            resp_data = {'password': pwd}
        elif data.get('password'):
            resp_data = {'password': data['password']}
        return json_response(data=resp_data, message='保存成功')
    except Exception as e:
        logger.error(f'[merchant_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/merchant/delete', methods=['POST'])
@require_auth
def admin_merchant_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM merchants WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[merchant_delete] {e}')
        return json_response(message=str(e), code=500)


# ============ Employees ============

@bp.route('/admin/employees', methods=['GET', 'POST'])
@require_auth
def admin_employees():
    try:
        data = request.get_json() if request.method == 'POST' else {}
        keyword = (data or {}).get('keyword', '') or request.args.get('keyword', '')
        merchant_id = (data or {}).get('merchant_id', '') or request.args.get('merchant_id', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if keyword:
            where += ' AND (e.name LIKE %s OR e.phone LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%'])
        if merchant_id:
            where += ' AND e.merchant_id=%s'
            params.append(merchant_id)
        c.execute(f'SELECT COUNT(*) FROM employees e WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT e.*, e.is_locked, m.name as merchant_name
            FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id
            WHERE {where} ORDER BY e.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        employees = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': employees, 'total': total})
    except Exception as e:
        logger.error(f'[admin_employees] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/employee/save', methods=['POST'])
@require_auth
def admin_employee_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('phone') and _check_phone_uniqueness(c, data["phone"], "employees" if data.get("id") else None, data.get("id")):
            conn.close()
            return json_response(message="该手机号已被其他商家/代理商/员工使用", code=400)
        if data.get('id'):
            fields = ['name','phone','role','merchant_id','status','permissions']
            sets, params = [], []
            for f in fields:
                if f in data:
                    sets.append(f'{f}=%s')
                    val = data[f]; params.append(None if val == "" and f in ("merchant_id",) else val)
            if data.get('password'):
                sets.append('password_hash=%s')
                params.append(generate_password_hash(data['password']))
                sets.append('plain_password=%s')
                params.append(data['password'])
            params.append(data['id'])
            c.execute(f'UPDATE employees SET {",".join(sets)} WHERE id=%s', params)
        else:
            if not data.get('name') or not data.get('phone'):
                conn.close()
                return json_response(message='参数不完整', code=400)
            pwd = data.get('password') or 'Emp@' + ''.join(__import__('random').choices(__import__('string').ascii_letters + __import__('string').digits, k=2))
            c.execute('INSERT INTO employees (merchant_id, name, phone, password_hash, role, permissions, plain_password) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                      (data.get("merchant_id") or None, data["name"], data['phone'], generate_password_hash(pwd), data.get('role','staff'), data.get('permissions','[]'), pwd))
        conn.commit()
        conn.close()
        resp_data = None
        if not data.get('id'):
            resp_data = {'password': pwd}
        elif data.get('password'):
            resp_data = {'password': data['password']}
        return json_response(data=resp_data, message='保存成功')
    except Exception as e:
        logger.error(f'[employee_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/employee/delete', methods=['POST'])
@require_auth
def admin_employee_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM employees WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[employee_delete] {e}')
        return json_response(message=str(e), code=500)


# ============ Users ============

@bp.route('/admin/users', methods=['GET', 'POST'])
@require_auth
def admin_users():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id, username, role, created_at FROM admin_users ORDER BY created_at DESC')
        users = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data=users)
    except Exception as e:
        logger.error(f'[admin_users] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/user/save', methods=['POST'])
@require_auth
def admin_user_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('id'):
            fields = ['username','role']
            sets, params = [], []
            for f in fields:
                if f in data:
                    sets.append(f'{f}=%s')
                    params.append(data[f])
            if data.get('password'):
                sets.append('password_hash=%s')
                params.append(generate_password_hash(data['password']))
            params.append(data['id'])
            c.execute(f'UPDATE admin_users SET {",".join(sets)} WHERE id=%s', params)
        else:
            if not data.get('username') or not data.get('password'):
                conn.close()
                return json_response(message='参数不完整', code=400)
            c.execute('SELECT id FROM admin_users WHERE username=%s', (data['username'],))
            if c.fetchone():
                conn.close()
                return json_response(message='用户名已存在', code=400)
            c.execute('INSERT INTO admin_users (username, password_hash, role) VALUES (%s,%s,%s)',
                      (data['username'], generate_password_hash(data['password']), data.get('role','admin')))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[user_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/user/delete', methods=['POST'])
@require_auth
def admin_user_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM admin_users WHERE id=%s AND id!=%s', (data.get('id'), session.get('admin_id', 0)))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[user_delete] {e}')
        return json_response(message=str(e), code=500)


# ============ APK ============

@bp.route('/admin/apk-version', methods=['GET', 'POST'])
@require_auth
def admin_apk_version():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM apk_version ORDER BY version_code DESC LIMIT 1')
        row = c.fetchone()
        conn.close()
        if row:
            return json_response(data=dict(row))
        return json_response(data={})
    except Exception as e:
        logger.error(f'[apk_version] {e}')
        return json_response(data={})


@bp.route('/admin/apk-version/save', methods=['POST'])
@require_auth
def admin_apk_version_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO apk_version (version_name,version_code,download_url,update_desc) VALUES (%s,%s,%s,%s)''',
                  (data.get('version_name'), data.get('version_code'), data.get('download_url'), data.get('update_desc','')))
        conn.commit()
        conn.close()
        return json_response(message='发布成功')
    except Exception as e:
        logger.error(f'[apk_version_save] {e}')
        return json_response(message=str(e), code=500)



@bp.route("/admin/apk/push-update", methods=["POST"])
@require_auth
def admin_apk_push_update():
    """推送APK更新到所有在线设备"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM apk_version ORDER BY version_code DESC LIMIT 1")
        apk = c.fetchone()
        conn.close()
        if not apk:
            return json_response(message="没有找到APK版本信息", code=400)
        cmd = {"type":"force_update","download_url":apk["download_url"],"version_name":apk["version_name"],"version_code":apk["version_code"],"update_desc":apk.get("update_desc","") or "","force":True,"file_md5":apk.get("file_md5","") or ""}
        pushed = 0
        for did, ws in list(connected_devices.items()):
            try:
                ws.send(json.dumps(cmd))
                pushed += 1
                logger.info(f"[APK推送] 已推送更新到设备 {did}")
            except Exception as e:
                logger.error(f"[APK推送] 推送失败 {did}: {e}")
        if pushed == 0:
            conn2 = get_db()
            c2 = conn2.cursor()
            c2.execute("SELECT mainboard_device_id FROM cabinets WHERE mainboard_device_id IS NOT NULL AND mainboard_device_id != ''""")
            for row in c2.fetchall():
                did = row["mainboard_device_id"]
                c2.execute("INSERT INTO pending_lock_cmds (device_id, command, status) VALUES (%s, %s, \"pending\")", (did, json.dumps(cmd)))
            conn2.commit()
            conn2.close()
        return json_response(data={"pushed": pushed}, message=f"已向{pushed}台在线设备推送更新")
    except Exception as e:
        logger.error(f"[APK推送] 错误: {e}")
        return json_response(message=str(e), code=500)

# ============ After-sales ============

@bp.route('/admin/after-sales', methods=['GET', 'POST'])
@require_auth
def admin_after_sales():
    """售后工单列表"""
    try:
        data = request.get_json() if request.method == 'POST' else {}
        status = (data or {}).get('status', '') or request.args.get('status', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if status:
            where += ' AND status=%s'
            params.append(status)
        c.execute(f'SELECT COUNT(*) FROM after_sales WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT a.*, c.cabinet_code, l.name as location_name
            FROM after_sales a LEFT JOIN cabinets c ON a.cabinet_id=c.id
            LEFT JOIN locations l ON c.location_id=l.id
            WHERE {where} ORDER BY a.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        records = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': records, 'total': total})
    except Exception as e:
        logger.error(f'[after_sales] {e}')
        return json_response(data={'list': [], 'total': 0})


@bp.route('/admin/after-sales/save', methods=['POST'])
@require_auth
def admin_after_sales_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if _had_id:
            c.execute('UPDATE after_sales SET cabinet_id=%s,type=%s,description=%s WHERE id=%s',
                      (data.get('cabinet_id'), data.get('type'), data.get('description'), data['id']))
        else:
            ticket_no = f'AS{datetime.now().strftime("%Y%m%d%H%M%S")}'
            c.execute('''INSERT INTO after_sales (ticket_no,cabinet_id,type,description,status) VALUES (%s,%s,%s,%s,%s)''',
                      (ticket_no, data.get('cabinet_id'), data.get('type'), data.get('description',''), 'pending'))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[after_sales_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/after-sales/handle', methods=['POST'])
@require_auth
def admin_after_sales_handle():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE after_sales SET status=%s,handler_note=%s,handler=%s WHERE id=%s',
                  (data.get('status','processing'), data.get('handler_note',''), session.get('admin_username','admin'), data['id']))
        conn.commit()
        conn.close()
        return json_response(message='处理成功')
    except Exception as e:
        logger.error(f'[after_sales_handle] {e}')
        return json_response(message=str(e), code=500)


# ============ Stats ============

@bp.route('/admin/stats', methods=['GET', 'POST'])
@require_auth
def admin_stats():
    """统计数据"""
    try:
        data = request.get_json() if request.method == 'POST' else request.args.to_dict()
        location_id = data.get('location_id', '')
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        conn = get_db()
        c = conn.cursor()
        # Summary - status=1 means in use
        where_parts = []
        params = []
        if location_id:
            where_parts.append('cabinet_id IN (SELECT id FROM cabinets WHERE location_id=%s)')
            params.append(location_id)
        if start_date and end_date:
            where_parts.append('date(created_at)>=%s AND date(created_at)<=%s')
            params.extend([start_date, end_date])
        elif not start_date and not end_date:
            where += " AND o.created_at>=NOW() - interval '30 days'"
        where_clause = (' WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
        c.execute(f'''SELECT COUNT(*) as total, 
            SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) as active_count,
            COALESCE(SUM(o.deposit_amount),0) as deposit_total,
            COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END),0) as refund_total
            FROM orders{where_clause}''', params)
        summary = dict(c.fetchone())
        summary['net_income'] = float(summary.get('deposit_total',0)) - float(summary.get('refund_total',0))
        # Location stats - join through cabinets since orders have no location_id
        c.execute('''SELECT l.name as location_name, m.name as merchant_name,
            COUNT(o.id) as order_count,
            SUM(CASE WHEN o.status=2 THEN 1 ELSE 0 END) as active_count,
            COALESCE(SUM(o.deposit_amount),0) as deposit_total,
            COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END),0) as refund_total,
            COALESCE(l.legacy_visible_orders, 0) as legacy_visible_orders
            FROM locations l LEFT JOIN merchants m ON l.merchant_id=m.id
            LEFT JOIN cabinets cab ON cab.location_id=l.id
            LEFT JOIN orders o ON o.cabinet_id=cab.id
            LEFT JOIN payment_channels pc ON o.payment_channel_id = pc.id
            GROUP BY l.id''')
        locations = []
        orders = []
        for r in c.fetchall():
            d = dict(r)
            d['net_income'] = float(d.get('deposit_total',0) or 0) - float(d.get('refund_total',0) or 0)
            locations.append(d)
        # Trend
        trend = []
        for i in range(30):
            date = (datetime.now() - timedelta(days=29-i)).strftime('%Y-%m-%d')
            c.execute('''SELECT COUNT(*) as order_count,
                COALESCE(SUM(o.deposit_amount),0) as deposit_total,
                COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END),0) as refund_total
                FROM orders WHERE date(created_at)=%s''', (date,))
            row = c.fetchone()
            trend.append({
                'date': date,
                'order_count': row['order_count'] if row else 0,
                'deposit_total': float(row['deposit_total'] if row else 0),
                'refund_total': float(row['refund_total'] if row else 0)
            })
        conn.close()
        return json_response(data={'summary': summary, 'locations': locations, 'trend': trend})
    except Exception as e:
        logger.error(f'[stats] {e}')
        return json_response(data={'summary': {}, 'locations': [], 'trend': []})



@bp.route('/admin/biz-stats', methods=['GET', 'POST'])
@require_auth
def admin_biz_stats():
    """业务统计数据"""
    try:
        data = request.get_json() if request.method == 'POST' else request.args.to_dict()
        province = data.get('province', '')
        agent_id = data.get('agent_id', '')
        location_id = data.get('location_id', '')
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        
        conn = get_db()
        c = conn.cursor()
        
        # 构建基础查询条件
        where_parts = ["o.status NOT IN (5)"]
        params = []
        
        if location_id:
            where_parts.append('cab.location_id=%s')
            params.append(location_id)
        if agent_id:
            where_parts.append('l.agent_id=%s')
            params.append(agent_id)
        if province:
            where_parts.append('l.province=%s')
            params.append(province)
        if start_date and end_date:
            where_parts.append('date(o.created_at)>= %s AND date(o.created_at)<= %s')
            params.extend([start_date, end_date])
        elif not start_date and not end_date:
            where_parts.append("o.created_at>=NOW() - INTERVAL '30 days'")
        
        where_clause = (' WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
        
        # 订单汇总统计（含隐藏订单统计）
        c.execute(f'''SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN o.logic_mark IS NULL OR o.logic_mark != 'Y' THEN 1 END) as visible_count,
            SUM(CASE WHEN o.status=2 THEN 1 ELSE 0 END) as active_count,
            COALESCE(SUM(o.deposit_amount),0) as deposit_total,
            COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END),0) as refund_total
            FROM orders o
            LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
            LEFT JOIN locations l ON cab.location_id=l.id
            {where_clause}''', params)
        row = c.fetchone()
        # Add legacy visible orders to total
        c.execute("SELECT COALESCE(SUM(legacy_visible_orders), 0) FROM locations")
        _legacy_total = c.fetchone()[0] or 0
        orderStats = {
            'total': row[0] if row else 0,
            'visible_count': (row[1] if row else 0) + _legacy_total,
            'active_count': row[2] if row else 0,
            'deposit_total': float(row[3] if row and row[3] else 0),
            'refund_total': float(row[4] if row and row[4] else 0),
            'net_income': float(row[3] if row and row[3] else 0) - float(row[4] if row and row[4] else 0)
        }
        
        # 根据 hide_ratio 调整 visible_count（SQL 只看 logic_mark，hash 隐藏的需要额外计算）
        c.execute("SELECT o.id, o.user_phone, o.logic_mark, cab.location_id, l.merchant_id, l.hide_ratio, l.whitelist_phones FROM orders o LEFT JOIN cabinets cab ON o.cabinet_id=cab.id LEFT JOIN locations l ON cab.location_id=l.id " + (where_clause or ''), params)
        hidden_count = 0
        for r in c.fetchall():
            if r['logic_mark'] == 'N':
                continue
            if r['logic_mark'] == 'Y':
                continue
            mid = r['merchant_id']
            hide_ratio = r['hide_ratio'] or 0
            if hide_ratio <= 0 or not mid:
                continue
            whitelist = set((r['whitelist_phones'] or '').split(',')) if r['whitelist_phones'] else set()
            if should_hide_order(mid, r['id'], r['user_phone'] or '', hide_ratio, whitelist):
                hidden_count += 1
        orderStats['visible_count'] = max(0, orderStats['visible_count'] - hidden_count)
        
        # 按日期明细统计：改为先查询所有订单，然后在Python中分组统计（支持hide_ratio）
        has_location_filter = bool(location_id)
        if has_location_filter:
            c.execute(f'''SELECT 
                l.id as location_id,
                l.name as location_name,
                date(o.created_at) as stat_date,
                o.id as order_id,
                o.user_phone,
                o.logic_mark,
                o.deposit_amount,
                CASE WHEN date(o.refund_time)=date(o.created_at) THEN o.refund_amount ELSE 0 END as refund_amount,
                l.merchant_id,
                l.hide_ratio,
                l.whitelist_phones
                FROM locations l
                LEFT JOIN cabinets cab ON cab.location_id=l.id
                LEFT JOIN orders o ON o.cabinet_id=cab.id
                {where_clause}
                ORDER BY stat_date DESC, l.name''', params)
        else:
            c.execute(f'''SELECT 
                date(o.created_at) as stat_date,
                o.id as order_id,
                o.user_phone,
                o.logic_mark,
                o.deposit_amount,
                CASE WHEN date(o.refund_time)=date(o.created_at) THEN o.refund_amount ELSE 0 END as refund_amount,
                l.merchant_id,
                l.hide_ratio,
                l.whitelist_phones
                FROM orders o
                LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
                LEFT JOIN locations l ON cab.location_id=l.id
                {where_clause}
                ORDER BY stat_date DESC''', params)
        # 在Python中按日期+网点分组统计，支持hide_ratio
        from collections import defaultdict
        location_details = []
        orders = []
        
        # 按(stat_date, location_id)分组
        grouped = defaultdict(lambda: {'order_count': 0, 'visible_count': 0, 'deposit_total': 0, 'refund_total': 0, 'location_name': ''})
        
        for row in c.fetchall():
            if has_location_filter:
                stat_date = str(row['stat_date']) if row['stat_date'] else ''
                loc_id = row['location_id']
                loc_name = row['location_name']
                order_id = row['order_id']
                user_phone = row['user_phone'] or ''
                logic_mark = row['logic_mark']
                deposit = float(row['deposit_amount'] or 0)
                refund = float(row['refund_amount'] or 0)
                merchant_id = row['merchant_id']
                hide_ratio = row['hide_ratio'] or 0
                whitelist_phones = row['whitelist_phones']
            else:
                stat_date = str(row['stat_date']) if row['stat_date'] else ''
                loc_id = 'all'
                loc_name = ''
                order_id = row['order_id']
                user_phone = row['user_phone'] or ''
                logic_mark = row['logic_mark']
                deposit = float(row['deposit_amount'] or 0)
                refund = float(row['refund_amount'] or 0)
                merchant_id = row['merchant_id']
                hide_ratio = row['hide_ratio'] or 0
                whitelist_phones = row['whitelist_phones']
            
            key = (stat_date, loc_id)
            grouped[key]['order_count'] += 1
            grouped[key]['deposit_total'] += deposit
            grouped[key]['refund_total'] += refund
            if loc_name:
                grouped[key]['location_name'] = loc_name
            
            # 计算visible_count
            is_hidden = False
            if logic_mark == 'Y':
                is_hidden = True
            elif logic_mark != 'N' and merchant_id and hide_ratio > 0:
                whitelist = set((whitelist_phones or '').split(',')) if whitelist_phones else set()
                if should_hide_order(merchant_id, order_id, user_phone, hide_ratio, whitelist):
                    is_hidden = True
            
            if not is_hidden:
                grouped[key]['visible_count'] += 1
        
        # 转换为列表
        for (stat_date, loc_id), data in grouped.items():
            if has_location_filter:
                location_details.append({
                    'location_id': loc_id,
                    'location_name': data['location_name'],
                    'stat_date': stat_date,
                    'order_count': data['order_count'],
                    'visible_count': data['visible_count'],
                    'deposit_total': data['deposit_total'],
                    'refund_total': data['refund_total'],
                    'balance': data['deposit_total'] - data['refund_total']
                })
            else:
                location_details.append({
                    'stat_date': stat_date,
                    'order_count': data['order_count'],
                    'visible_count': data['visible_count'],
                    'deposit_total': data['deposit_total'],
                    'refund_total': data['refund_total'],
                    'balance': data['deposit_total'] - data['refund_total']
                })
        
        # 按天聚合趋势
        daily = []
        days = 30
        for i in range(days):
            date = (datetime.now() - timedelta(days=days-1-i)).strftime('%Y-%m-%d')
            c.execute(f'''SELECT COUNT(*) as cnt, COALESCE(SUM(o.deposit_amount),0) as dep, COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END),0) as ref
                FROM orders o
                LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
                LEFT JOIN locations l ON cab.location_id=l.id
                WHERE date(o.created_at)=%s
                {(' AND ' + ' AND '.join(where_parts)) if where_parts else ''}''', [date] + params)
            row = c.fetchone()
            daily.append({
                'date': date,
                'order_count': row[0] if row else 0,
                'deposit_total': float(row[1] if row and row[1] else 0),
                'refund_total': float(row[2] if row and row[2] else 0)
            })
        
        conn.close()
        return json_response(data={
            'orderStats': orderStats,
            'locationDetails': location_details,
            'daily': daily
        })
    except Exception as e:
        logger.error(f'[biz_stats] {e}')
        return json_response(message=str(e), code=500)


# ============ Channels ============

@bp.route('/admin/channels', methods=['GET', 'POST'])
@require_auth
def admin_channels():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM payment_channels ORDER BY created_at DESC')
        channels = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data=channels)
    except Exception as e:
        logger.error(f'[channels] {e}')
        return json_response(data=[])


@bp.route('/admin/channel/save', methods=['POST'])
@require_auth
def admin_channel_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('id'):
            fields = ['name','channel_type','app_id','mch_id','api_key','app_secret','cert_name','is_active']
            sets, params = [], []
            for f in fields:
                if f in data:
                    sets.append(f'{f}=%s')
                    params.append(data[f])
            params.append(data['id'])
            c.execute(f'UPDATE payment_channels SET {",".join(sets)} WHERE id=%s', params)
        else:
            # 防重复：检查 mch_id 是否已存在
            mch_id = data.get('mch_id', '').strip()
            if mch_id:
                c.execute('SELECT id FROM payment_channels WHERE mch_id=%s', (mch_id,))
                if c.fetchone():
                    conn.close()
                    return json_response(message=f'商户号 {mch_id} 已存在，请勿重复添加', code=400)
            c.execute('''INSERT INTO payment_channels (name,channel_type,app_id,mch_id,api_key,app_secret,cert_name,is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                      (data.get('name'), data.get('channel_type'), data.get('app_id') or WX_APP_ID,
                       data.get('mch_id'), data.get('api_key') or WX_API_KEY, data.get('app_secret') or WX_APP_SECRET, data.get('cert_name'), data.get('status',1)))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[channel_save] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/channel/defaults', methods=['GET'])
@require_auth
def admin_channel_defaults():
    return json_response({'app_id': WX_APP_ID, 'api_key': WX_API_KEY, 'app_secret': WX_APP_SECRET})

@bp.route('/admin/channel/delete', methods=['POST'])
@require_auth
def admin_channel_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM payment_channels WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[channel_delete] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/channel/upload-cert', methods=['POST'])
@require_auth
def admin_channel_upload_cert():
    try:
        mch_id = request.form.get('mch_id', '').strip()
        if not mch_id:
            return json_response(message='缺少商户号', code=400)
        cert_dir = '/home/ubuntu/smart-locker/cert'
        uploaded = []
        file_map = {
            'cert_pem': f'{mch_id}_cert.pem',
            'key_pem': f'{mch_id}_key.pem',
            'cert_p12': f'{mch_id}_cert.p12',
        }
        for field, filename in file_map.items():
            f = request.files.get(field)
            if f and f.filename:
                filepath = os.path.join(cert_dir, filename)
                f.save(filepath)
                uploaded.append(filename)
        # Extract cert serial number if cert_pem was uploaded
        cert_pem_path = os.path.join(cert_dir, mch_id + "_cert.pem")
        if os.path.exists(cert_pem_path):
            try:
                result = subprocess.run(
                    ["openssl", "x509", "-in", cert_pem_path, "-noout", "-serial"],
                    capture_output=True, text=True, timeout=5
                )
                serial = result.stdout.strip().replace("serial=", "")
                if serial:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE payment_channels SET cert_serial_no=%s WHERE mch_id=%s", (serial, mch_id))
                    conn.commit()
                    conn.close()
                    uploaded.append("serial_no: " + serial[:20] + "...")
            except Exception as cert_e:
                logger.warning("[upload_cert] 提取证书序列号失败: " + str(cert_e))
        return json_response(data={'uploaded': uploaded}, message='上传成功并已更新证书序列号')
    except Exception as e:
        logger.error(f'[channel_upload_cert] {e}')
        return json_response(message=str(e), code=500)


# ============ Password ============

@bp.route('/admin/change-password', methods=['POST'])
@require_auth
def admin_change_password():
    try:
        data = request.get_json()
        old_pwd = data.get('old_password', '')
        new_pwd = data.get('new_password', '')
        if not all([old_pwd, new_pwd]):
            return json_response(message='旧密码和新密码不能为空', code=400)
        if len(new_pwd) < 6:
            return json_response(message='新密码长度不能少于6位', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT password_hash FROM admin_users WHERE id=%s', (session['admin_id'],))
        admin = c.fetchone()
        if not admin or not check_password_hash(admin['password_hash'], old_pwd):
            conn.close()
            return json_response(message='旧密码错误', code=400)
        c.execute('UPDATE admin_users SET password_hash=%s WHERE id=%s',
                  (generate_password_hash(new_pwd), session['admin_id']))
        conn.commit()
        conn.close()
        return json_response(message='密码修改成功')
    except Exception as e:
        logger.error(f'[change_pwd] {e}')
        return json_response(message=str(e), code=500)
# -*- coding: utf-8 -*-
"""
11个功能页面的API端点
追加到 routes/admin_v2.py 末尾
"""
import os
import sqlite3
import random
import string
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'locker.db')
# Fix: actual DB is in app root, not parent of routes/

# ============ 建表 ============

def _ensure_tables():
    try:
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS companies(
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            credit_code TEXT,
            contact_person TEXT,
            contact_phone TEXT,
            address TEXT,
            status INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS blacklist(
            id SERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            reason TEXT,
            cabinet_id INTEGER,
            operator TEXT,
            status INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS after_sales(
            id SERIAL PRIMARY KEY,
            ticket_no TEXT UNIQUE,
            cabinet_id INTEGER,
            location_id INTEGER,
            device_id TEXT,
            fault_type TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            handler TEXT,
            handler_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS alarms(
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            cabinet_id INTEGER,
            device_id TEXT,
            content TEXT,
            level INTEGER DEFAULT 1,
            status INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            resolved_at TIMESTAMP,
            resolver TEXT
        )""")
        conn.commit()
        conn.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'_ensure_tables error (tables likely exist): {e}')

_ensure_tables()

# ============ 1. 结算管理 ============

@bp.route('/settlement/list', methods=['GET', 'POST'])
@require_auth
def settlement_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        _d2=request.get_json(silent=True) or {}; location_id = _d2.get('location_id') or request.args.get('location_id', '')
        date_start = request.args.get('date_start', '')
        date_end = request.args.get('date_end', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT o.id, o.order_no, o.user_phone, o.cabinet_id, o.deposit_amount,
                o.refund_amount, o.refund_mark, o.refund_status, o.status, o.created_at, o.retrieve_time, o.cabinet_name,
                c.location_id, l.name as location_name, m.name as merchant_name
                FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
                LEFT JOIN locations l ON c.location_id=l.id
                LEFT JOIN merchants m ON l.merchant_id=m.id
            LEFT JOIN payment_channels pc ON o.payment_channel_id = pc.id
                WHERE 1=1"""
        params = []
        if location_id:
            sql += " AND c.location_id=%s"
            params.append(location_id)
        if date_start:
            sql += " AND o.created_at>=%s"
            params.append(date_start)
        if date_end:
            sql += " AND o.created_at<=%s"
            params.append(date_end)
        sql += " ORDER BY o.id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id WHERE 1=1"
        count_params = []
        if location_id:
            count_sql += " AND c.location_id=%s"
            count_params.append(location_id)
        if date_start:
            count_sql += " AND o.created_at>=%s"
            count_params.append(date_start)
        if date_end:
            count_sql += " AND o.created_at<=%s"
            count_params.append(date_end)
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page, 'size': size})
    except Exception as e:
        logger.error(f'[settlement_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/settlement/stats', methods=['GET', 'POST'])
@require_auth
def settlement_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as total_orders, COALESCE(SUM(deposit_amount),0) as total_deposit FROM orders")
        row = c.fetchone()
        c.execute("SELECT COUNT(*) as active_orders FROM orders WHERE status=2")
        active = c.fetchone()[0]
        c.execute("SELECT COUNT(*) as completed FROM orders WHERE status=2")
        completed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) as refunded FROM orders WHERE status=3")
        refunded = c.fetchone()[0]
        conn.close()
        return json_response(data={
            'total_orders': row['total_orders'],
            'total_deposit': row['total_deposit'],
            'active_orders': active,
            'completed': completed,
            'refunded': refunded
        })
    except Exception as e:
        logger.error(f'[settlement_stats] {e}')
        return json_response(message=str(e), code=500)

# ============ 2. 提现管理 ============

@bp.route('/withdrawals/list', methods=['GET', 'POST'])
@require_auth
def withdrawals_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        status = request.args.get('status', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT wr.*, o.order_no, o.deposit_amount as order_amount, c.cabinet_code
                FROM withdrawal_records wr 
                LEFT JOIN orders o ON wr.order_id=o.id
                LEFT JOIN cabinets c ON o.cabinet_id=c.id
                WHERE 1=1"""
        params = []
        if status != '':
            sql += " AND status=%s"
            params.append(int(status))
        sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM withdrawal_records WHERE 1=1"
        count_params = []
        if status != '':
            count_sql += " AND status=%s"
            count_params.append(int(status))
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page, 'size': size})
    except Exception as e:
        logger.error(f'[withdrawals_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/withdrawals/approve', methods=['POST'])
@require_auth
def withdrawals_approve():
    try:
        data = request.get_json()
        wid = data.get('id')
        action = data.get('action')  # approve/reject
        if not wid or not action:
            return json_response(message='参数缺失', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM withdrawal_records WHERE id=%s", (wid,))
        record = c.fetchone()
        if not record:
            conn.close()
            return json_response(message='记录不存在', code=404)
        if record['status'] != 0:
            conn.close()
            return json_response(message='该记录已处理', code=400)
        new_status = 1 if action == 'approve' else 2
        c.execute("UPDATE withdrawal_records SET status=%s, approve_time=NOW(), approver=%s WHERE id=%s",
                  (new_status, session.get('admin_user', 'admin'), wid))
        conn.commit()
        conn.close()
        return json_response(message='处理成功')
    except Exception as e:
        logger.error(f'[withdrawals_approve] {e}')
        return json_response(message=str(e), code=500)

# ============ 3. 平台流水 ============

@bp.route('/platform-flow/list', methods=['GET', 'POST'])
@require_auth
def platform_flow_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        flow_type = request.args.get('type', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT p.id, p.order_id, p.type, p.amount, p.transaction_id, p.status, p.created_at,
                p.refund_transaction_id, o.order_no, o.user_phone
                FROM payments p LEFT JOIN orders o ON p.order_id=o.id WHERE 1=1"""
        params = []
        if flow_type:
            sql += " AND p.type=%s"
            params.append(int(flow_type))
        sql += " ORDER BY p.id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM payments p WHERE 1=1"
        count_params = []
        if flow_type:
            count_sql += " AND p.type=%s"
            count_params.append(int(flow_type))
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(CASE WHEN type=1 THEN amount END),0) as total_deposit, COALESCE(SUM(CASE WHEN type=2 THEN amount END),0) as total_refund FROM payments WHERE status=1")
        summary = c.fetchone()
        conn.close()
        return json_response(data={
            'list': rows, 'total': total, 'page': page, 'size': size,
            'total_deposit': summary['total_deposit'],
            'total_refund': summary['total_refund']
        })
    except Exception as e:
        logger.error(f'[platform_flow_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 4. 资金流水 ============

@bp.route('/fund-flow/list', methods=['GET', 'POST'])
@require_auth
def fund_flow_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT ub.phone, ub.balance, ub.total_deposited, ub.total_withdrawn, ub.first_use_time,
                (SELECT COUNT(*) FROM orders WHERE user_phone=ub.phone) as order_count
                FROM user_balances ub ORDER BY ub.id DESC""")
        rows = [dict(r) for r in c.fetchall()]
        c.execute("SELECT COUNT(*) as total_users, COALESCE(SUM(balance),0) as total_balance FROM user_balances")
        summary = c.fetchone()
        conn.close()
        return json_response(data={'list': rows, 'total_users': summary['total_users'], 'total_balance': summary['total_balance']})
    except Exception as e:
        logger.error(f'[fund_flow_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 5. 综合查询 ============

@bp.route('/query-all/list', methods=['GET', 'POST'])
@require_auth
def query_all_list():
    try:
        keyword = request.args.get('keyword', '')
        query_type = request.args.get('type', 'order')
        conn = get_db()
        c = conn.cursor()
        results = []
        if not keyword:
            return json_response(data={'list': [], 'total': 0})
        if query_type == 'order':
            c.execute("""SELECT o.*, o.access_code as password, c.cabinet_code, c.name as cabinet_name, l.name as location_name
                    FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
                    LEFT JOIN locations l ON c.location_id=l.id
                    WHERE o.order_no LIKE %s OR o.user_phone LIKE %s ORDER BY o.id DESC LIMIT 50""",
                    (f'%{keyword}%', f'%{keyword}%'))
            results = [dict(r) for r in c.fetchall()]
        elif query_type == 'phone':
            c.execute("""SELECT ub.*, (SELECT COUNT(*) FROM orders WHERE user_phone=ub.phone) as order_count
                    FROM user_balances ub WHERE ub.phone LIKE %s LIMIT 50""", (f'%{keyword}%',))
            results = [dict(r) for r in c.fetchall()]
        elif query_type == 'cabinet':
            c.execute("""SELECT c.*, l.name as location_name
                    FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id
                    WHERE c.cabinet_code LIKE %s OR c.name LIKE %s OR c.mainboard_device_id LIKE %s LIMIT 50""",
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'))
            results = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': results, 'total': len(results)})
    except Exception as e:
        logger.error(f'[query_all_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 6. 公司管理 ============

@bp.route('/companies/list', methods=['GET', 'POST'])
@require_auth
def companies_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM companies ORDER BY id DESC")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[companies_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/companies/save', methods=['POST'])
@require_auth
def companies_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        cid = data.get('id')
        if cid:
            c.execute("UPDATE companies SET name=%s, credit_code=%s, contact_person=%s, contact_phone=%s, address=%s, status=%s WHERE id=%s",
                      (data.get('name',''), data.get('credit_code',''), data.get('contact_person',''),
                       data.get('contact_phone',''), data.get('address',''), data.get('status',1), cid))
        else:
            c.execute("INSERT INTO companies(name, credit_code, contact_person, contact_phone, address, status) VALUES(%s,%s,%s,%s,%s,%s)",
                      (data.get('name',''), data.get('credit_code',''), data.get('contact_person',''),
                       data.get('contact_phone',''), data.get('address',''), data.get('status',1)))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[companies_save] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/companies/delete', methods=['POST'])
@require_auth
def companies_delete():
    try:
        data = request.get_json()
        cid = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM companies WHERE id=%s", (cid,))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[companies_delete] {e}')
        return json_response(message=str(e), code=500)

# ============ 7. 黑名单管理 ============

@bp.route('/blacklist/list', methods=['GET', 'POST'])
@require_auth
def blacklist_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT b.*, c.cabinet_code, c.name as cabinet_name
                FROM blacklist b LEFT JOIN cabinets c ON b.cabinet_id=c.id
                ORDER BY b.id DESC""")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[blacklist_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/blacklist/save', methods=['POST'])
@require_auth
def blacklist_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        bid = data.get('id')
        if bid:
            c.execute("UPDATE blacklist SET phone=%s, reason=%s, cabinet_id=%s, status=%s WHERE id=%s",
                      (data.get('phone',''), data.get('reason',''), data.get('cabinet_id'), data.get('status',1), bid))
        else:
            c.execute("INSERT INTO blacklist(phone, reason, cabinet_id, operator, status) VALUES(%s,%s,%s,%s,%s)",
                      (data.get('phone',''), data.get('reason',''), data.get('cabinet_id'),
                       session.get('admin_user','admin'), data.get('status',1)))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[blacklist_save] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/blacklist/delete', methods=['POST'])
@require_auth
def blacklist_delete():
    try:
        data = request.get_json()
        bid = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM blacklist WHERE id=%s", (bid,))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[blacklist_delete] {e}')
        return json_response(message=str(e), code=500)

# ============ 8. 报警记录 ============

@bp.route('/alarms/list', methods=['GET', 'POST'])
@require_auth
def alarms_resolve():
    try:
        data = request.get_json()
        aid = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE alarms SET status=1, resolved_at=NOW(), resolver=%s WHERE id=%s",
                  (session.get('admin_user','admin'), aid))
        conn.commit()
        conn.close()
        return json_response(message='处理成功')
    except Exception as e:
        logger.error(f'[alarms_resolve] {e}')
        return json_response(message=str(e), code=500)

# ============ 9. 位置报警 ============

@bp.route('/location-alarms/list', methods=['GET', 'POST'])
@require_auth
def location_alarms_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT c.id, c.cabinet_code, c.name, c.location_id, l.name as location_name,
                c.last_heartbeat, c.status, c.mainboard_device_id,
                (SELECT COUNT(*) FROM alarms WHERE cabinet_id=c.id AND status=0) as alarm_count,
                (SELECT COUNT(*) FROM orders WHERE cabinet_id=c.id AND status=1) as active_orders
                FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id
                WHERE c.location_id IS NOT NULL ORDER BY c.id""")
        rows = [dict(r) for r in c.fetchall()]
        import datetime as dt_mod
        for row in rows:
            if row.get('last_heartbeat'):
                try:
                    hb = row['last_heartbeat']
                    if isinstance(hb, str):
                        hb = dt_mod.datetime.strptime(hb, '%Y-%m-%d %H:%M:%S')
                    diff = (dt_mod.datetime.utcnow() - hb).total_seconds()
                    row['offline'] = diff > 300
                    row['heartbeat_age_min'] = int(diff / 60)
                except:
                    row['offline'] = True
                    row['heartbeat_age_min'] = 999
            else:
                row['offline'] = True
                row['heartbeat_age_min'] = 0
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[location_alarms_list] {e}')
        return json_response(message=str(e), code=500)

# ============ 10. 角色管理 ============

@bp.route('/roles/list', methods=['GET', 'POST'])
@require_auth
def roles_list():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, username, role, created_at FROM admin_users ORDER BY id")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[roles_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/roles/save', methods=['POST'])
@require_auth
def roles_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        uid = data.get('id')
        username = data.get('username','')
        role = data.get('role','viewer')
        password = data.get('password','')
        if uid:
            if password:
                c.execute("UPDATE admin_users SET username=%s, role=%s, password_hash=%s WHERE id=%s",
                          (username, role, generate_password_hash(password), uid))
            else:
                c.execute("UPDATE admin_users SET username=%s, role=%s WHERE id=%s", (username, role, uid))
        else:
            if not password:
                return json_response(message='新用户必须设置密码', code=400)
            c.execute("INSERT INTO admin_users(username, password_hash, role) VALUES(%s,%s,%s)",
                      (username, generate_password_hash(password), role))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[roles_save] {e}')
        return json_response(message=str(e), code=500)

# ============ 11. 数据重置 ============

@bp.route('/data-reset/stats', methods=['GET', 'POST'])
@require_auth
def data_reset_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        stats = {}
        for table in ['orders','payments','withdrawal_records','complaints','device_logs','storage_records','door_records','pending_lock_cmds']:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = c.fetchone()[0]
        conn.close()
        return json_response(data=stats)
    except Exception as e:
        logger.error(f'[data_reset_stats] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/data-reset/exec', methods=['POST'])
@require_auth
def data_reset_exec():
    try:
        data = request.get_json()
        tables = data.get('tables', [])
        if not tables:
            return json_response(message='请选择要清理的表', code=400)
        conn = get_db()
        c = conn.cursor()
        allowed = ['orders','payments','withdrawal_records','complaints','device_logs','storage_records','door_records','pending_lock_cmds']
        for t in tables:
            if t in allowed:
                c.execute(f"DELETE FROM {t}")
        conn.commit()
        conn.close()
        return json_response(message='清理完成')
    except Exception as e:
        logger.error(f'[data_reset_exec] {e}')
        return json_response(message=str(e), code=500)

# ==================== P0-3: 系统设置管理 ====================

@bp.route('/settings', methods=['GET', 'POST'])
def get_settings():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        rows = c.execute("SELECT setting_key as key, setting_value as value, description FROM system_settings").fetchall()
        settings = {row['key']: {'value': row['value'], 'desc': row['description']} for row in rows}
        conn.close()
        return jsonify({'code': 200, 'data': settings})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass



# DISABLED: misplaced delete endpoint (was inserted at wrong location)
# @bp.route('/admin/alerts/delete', methods=['POST'])
# @require_auth
def alerts_delete():
    """??????"""
    try:
        data = request.get_json() or {}
        alert_id = data.get('id', '')
        device_id = data.get('device_id', '')
        days = int(data.get('days', 0))
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if alert_id:
            c.execute("DELETE FROM device_alerts WHERE id=?", (alert_id,))
            msg = f'????? #{alert_id}'
        elif device_id:
            c.execute("DELETE FROM device_alerts WHERE device_id=?", (device_id,))
            msg = f'????? {device_id} ???'
        elif days > 0:
            c.execute("DELETE FROM device_alerts WHERE created_at < datetime('now', ?)", ('-' + str(days) + ' days',))
            msg = f'??? {days} ?????'
        else:
            c.execute("DELETE FROM device_alerts")
            msg = '???????'
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': msg + f'???? {deleted} ??'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/settings/save', methods=['POST'])
def save_settings():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for key, value in data.items():
            existing = c.execute("SELECT id FROM system_settings WHERE setting_key=%s", (key,)).fetchone()
            if existing:
                c.execute("UPDATE system_settings SET setting_value=%s WHERE setting_key=%s", (str(value), key))
            else:
                c.execute("INSERT INTO system_settings (setting_key, setting_value, description) VALUES (%s, %s, '')", (key, str(value)))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/settings/order-visibility', methods=['GET', 'POST'])
def get_order_visibility():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        hide_rate = c.execute("SELECT setting_value FROM system_settings WHERE setting_key='order_hide_rate'").fetchone()
        whitelist = c.execute("SELECT setting_value FROM system_settings WHERE setting_key='order_hide_whitelist'").fetchone()
        conn.close()
        return jsonify({'code': 200, 'data': {
            'order_hide_rate': int(hide_rate['value']) if hide_rate else 0,
            'order_hide_whitelist': whitelist['value'] if whitelist else ''
        }})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/settings/order-visibility/save', methods=['POST'])
def save_order_visibility():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for key in ['order_hide_rate', 'order_hide_whitelist']:
            val = data.get(key, '')
            existing = c.execute("SELECT id FROM system_settings WHERE setting_key=%s", (key,)).fetchone()
            if existing:
                c.execute("UPDATE system_settings SET setting_value=%s WHERE setting_key=%s", (str(val), key))
            else:
                c.execute("INSERT INTO system_settings (setting_key, setting_value, description) VALUES (%s, %s, '')", (key, str(val)))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/settings/duplicate-filter', methods=['GET', 'POST'])
def get_duplicate_filter():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        enabled = c.execute("SELECT setting_value FROM system_settings WHERE setting_key='duplicate_filter_enabled'").fetchone()
        days = c.execute("SELECT setting_value FROM system_settings WHERE setting_key='duplicate_days'").fetchone()
        limit = c.execute("SELECT setting_value FROM system_settings WHERE setting_key='duplicate_limit'").fetchone()
        conn.close()
        return jsonify({'code': 200, 'data': {
            'duplicate_filter_enabled': int(enabled['value']) if enabled and enabled['value'] not in ('false','0') else 0,
            'duplicate_days': int(days['value']) if days else 7,
            'duplicate_limit': int(limit['value']) if limit else 5
        }})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/settings/duplicate-filter/save', methods=['POST'])
def save_duplicate_filter():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for key in ['duplicate_filter_enabled', 'duplicate_days', 'duplicate_limit']:
            val = data.get(key, '')
            existing = c.execute("SELECT id FROM system_settings WHERE setting_key=%s", (key,)).fetchone()
            if existing:
                c.execute("UPDATE system_settings SET setting_value=%s WHERE setting_key=%s", (str(val), key))
            else:
                c.execute("INSERT INTO system_settings (setting_key, setting_value, description) VALUES (%s, %s, '')", (key, str(val)))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== P0-4: 柜组管理 ====================

@bp.route('/admin/cabinet-groups', methods=['GET', 'POST'])
def cabinet_groups_list():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        keyword = request.args.get('keyword', '')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        where = "WHERE 1=1"
        params = []
        if keyword:
            where += " AND (group_code LIKE %s OR name LIKE %s)"
            params += [f'%{keyword}%', f'%{keyword}%']
        total = c.execute(f"SELECT COUNT(*) FROM cabinet_groups {where}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM cabinet_groups {where} ORDER BY id DESC LIMIT %s OFFSET %s", params + [limit, (page-1)*limit]).fetchall()
        groups = []
        for r in rows:
            g = dict(r)
            cabinet_count = c.execute("SELECT COUNT(*) FROM cabinets WHERE group_id=%s", (g['id'],)).fetchone()[0]
            g['cabinet_count'] = cabinet_count
            groups.append(g)
        conn.close()
        return jsonify({'code': 200, 'data': {'list': groups, 'total': total}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/admin/cabinet-groups/save', methods=['POST'])
def cabinet_groups_save():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if _had_id:
            c.execute("UPDATE cabinet_groups SET group_code=%s, name=%s, location_id=%s WHERE id=%s",
                      (data.get('group_code',''), data.get('name',''), data.get('location_id'), data['id']))
        else:
            c.execute("INSERT INTO cabinet_groups (location_id, group_code, name, status, created_at) VALUES (%s, %s, %s, 1, NOW())",
                      (data.get('location_id'), data.get('group_code',''), data.get('name','')))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/admin/cabinet-groups/delete', methods=['POST'])
def cabinet_groups_delete():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM cabinet_groups WHERE id=%s", (data['id'],))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/admin/cabinet-groups/cabinets', methods=['GET', 'POST'])
def cabinet_groups_cabinets():
    try:
        group_id = request.args.get('group_id')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        rows = c.execute("SELECT * FROM cabinets WHERE group_id=%s", (group_id,)).fetchall()
        conn.close()
        return jsonify({'code': 200, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@bp.route('/admin/cabinet-groups/by-code', methods=['GET', 'POST'])
def cabinet_groups_by_code():
    try:
        code = request.args.get('code')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM cabinet_groups WHERE group_code=%s", (code,)).fetchone()
        if not row:
            return jsonify({'code': 404, 'message': '柜组不存在'})
        g = dict(row)
        cabinets = c.execute("SELECT * FROM cabinets WHERE group_id=%s", (g['id'],)).fetchall()
        g['cabinets'] = [dict(c2) for c2 in cabinets]
        conn.close()
        return jsonify({'code': 200, 'data': g})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ==================== P1: 代理商/员工登录 ====================

@bp.route('/admin/agent/login', methods=['POST'])
def agent_login():
    try:
        data = request.get_json() or {}
        phone = data.get('phone', '')
        password = data.get('password', '')
        if not phone or not password:
            return jsonify({'code': 400, 'message': '请输入手机号和密码'})
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM agents WHERE contact_phone=%s AND status=1", (phone,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'code': 401, 'message': '账号不存在或已停用'})
        if row.get('is_locked'):
            conn.close()
            return jsonify({'code': 403, 'message': '账号已锁定，请联系管理员解锁'})
        if not check_password_hash(row['password_hash'], password):
            attempts = (row.get('login_attempts') or 0) + 1
            locked = 1 if attempts >= 5 else 0
            c.execute("UPDATE agents SET login_attempts=%s, is_locked=%s WHERE id=%s", (attempts, locked, row['id']))
            conn.commit()
            conn.close()
            if locked:
                return jsonify({'code': 403, 'message': '密码错误次数过多，账号已锁定'})
            return jsonify({'code': 401, 'message': f'密码错误，还可尝试{5-attempts}次'})
        import secrets
        token = secrets.token_hex(16)
        c.execute("UPDATE agents SET auth_token=%s, login_attempts=0, is_locked=0 WHERE id=%s", (token, row['id']))
        conn.commit()
        result = dict(row)
        result.pop('password_hash', None)
        conn.close()
        session['agent_id'] = row['id']
        session['agent_name'] = row['name']
        session['is_agent'] = True
        all_perms = ["dashboard","locations","devices","orders","statistics","withdrawal","alerts","merchant_manage","full_data"]
        return jsonify({'code': 200, 'data': {'token': token, 'role': 'agent', 'agent_id': row['id'], 'name': row['name'], 'commission_rate': result.get('commission_rate', 0)}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.route('/admin/merchant/login', methods=['POST'])
def merchant_login():
    try:
        data = request.get_json() or {}
        phone = data.get('phone', '')
        password = data.get('password', '')
        if not phone or not password:
            return jsonify({'code': 400, 'message': '请输入手机号和密码'})
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM merchants WHERE contact_phone=%s AND status=1", (phone,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'code': 401, 'message': '账号不存在或已停用'})
        if row.get('is_locked'):
            conn.close()
            return jsonify({'code': 403, 'message': '账号已锁定，请联系管理员解锁'})
        if not check_password_hash(row['password_hash'], password):
            attempts = (row.get('login_attempts') or 0) + 1
            locked = 1 if attempts >= 5 else 0
            c.execute("UPDATE merchants SET login_attempts=%s, is_locked=%s WHERE id=%s", (attempts, locked, row['id']))
            conn.commit()
            conn.close()
            if locked:
                return jsonify({'code': 403, 'message': '密码错误次数过多，账号已锁定'})
            return jsonify({'code': 401, 'message': f'密码错误，还可尝试{5-attempts}次'})
        import secrets
        token = secrets.token_hex(16)
        c.execute("UPDATE merchants SET auth_token=%s, login_attempts=0, is_locked=0, last_login_at=datetime('now') WHERE id=%s", (token, row['id']))
        manage_user_tokens(c, 'merchant', row['id'], token, 3)
        conn.commit()
        result = dict(row)
        result.pop('password_hash', None)
        conn.close()
        return jsonify({'code': 200, 'data': {'token': token, 'role': 'merchant', 'merchant_id': row['id'], 'name': row['name'], 'agent_id': result.get('agent_id'), 'permissions': result.get("permissions", "[]")}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.route('/admin/employee/login', methods=['POST'])
def employee_login():
    try:
        data = request.get_json() or {}
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM employees WHERE phone=%s AND status=1", (data.get('phone',''),)).fetchone()
        if not row:
            return jsonify({'code': 401, 'message': '账号不存在或已停用'})
        if row['is_locked']:
            conn.close()
            return jsonify({'code': 403, 'message': '账号已锁定，请联系管理员解锁'})
        if not check_password_hash(row['password_hash'], data.get('password','')):
            attempts = (row['login_attempts'] or 0) + 1
            locked = 1 if attempts >= 5 else 0
            c.execute("UPDATE employees SET login_attempts=%s, is_locked=%s WHERE id=%s", (attempts, locked, row['id']))
            conn.commit()
            conn.close()
            if locked:
                return jsonify({'code': 403, 'message': '密码错误次数过多，账号已锁定'})
            return jsonify({'code': 401, 'message': f'密码错误，还可尝试{5-attempts}次'})
        import secrets
        token = secrets.token_hex(16)
        c.execute("UPDATE employees SET auth_token=%s, login_attempts=0, is_locked=0 WHERE id=%s", (token, row['id']))
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'data': {'token': token, 'employee_id': row['id'], 'name': row['name'], 'permissions': row['permissions'] if 'permissions' in row.keys() else '[]'}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass



# ==================== 详情接口 ====================

@bp.route('/admin/agent/detail', methods=['GET'])
@require_auth
def admin_agent_detail():
    try:
        agent_id = request.args.get('id', type=int)
        if not agent_id:
            return json_response(message='missing id', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM agents WHERE id=%s', (agent_id,))
        agent = dict(c.fetchone() or {})
        if not agent:
            conn.close()
            return json_response(message='代理商不存在', code=404)
        c.execute('SELECT COUNT(*) FROM merchants WHERE agent_id=%s', (agent_id,))
        agent['merchant_count'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM locations WHERE merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)', (agent_id,))
        agent['location_count'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s))', (agent_id,))
        agent['device_count'] = c.fetchone()[0]
        conn.close()
        return json_response(data=agent)
    except Exception as e:
        logger.error(f'[agent_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/merchant/detail', methods=['GET'])
@require_auth
def admin_merchant_detail():
    try:
        merchant_id = request.args.get('id', type=int)
        if not merchant_id:
            return json_response(message='missing id', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT m.*, a.name as agent_name FROM merchants m LEFT JOIN agents a ON m.agent_id=a.id WHERE m.id=%s', (merchant_id,))
        merchant = dict(c.fetchone() or {})
        if not merchant:
            conn.close()
            return json_response(message='商家不存在', code=404)
        c.execute('SELECT COUNT(*) FROM locations WHERE merchant_id=%s', (merchant_id,))
        merchant['location_count'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id=%s)', (merchant_id,))
        merchant['device_count'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM orders WHERE cabinet_id IN (SELECT id FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id=%s))', (merchant_id,))
        merchant['order_count'] = c.fetchone()[0]
        c.execute('SELECT COALESCE(SUM(deposit_amount),0) FROM orders WHERE cabinet_id IN (SELECT id FROM cabinets WHERE location_id IN (SELECT id FROM locations WHERE merchant_id=%s))', (merchant_id,))
        merchant['total_revenue'] = float(c.fetchone()[0] or 0)
        conn.close()
        return json_response(data=merchant)
    except Exception as e:
        logger.error(f'[merchant_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/employee/detail', methods=['GET'])
@require_auth
def admin_employee_detail():
    try:
        emp_id = request.args.get('id', type=int)
        if not emp_id:
            return json_response(message='missing id', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT e.*, m.name as merchant_name FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id WHERE e.id=%s', (emp_id,))
        employee = dict(c.fetchone() or {})
        if not employee:
            conn.close()
            return json_response(message='员工不存在', code=404)
        conn.close()
        return json_response(data=employee)
    except Exception as e:
        logger.error(f'[employee_detail] {e}')
        return json_response(message=str(e), code=500)


# ==================== 解锁 / 重置密码 ====================

@bp.route('/admin/agent/unlock', methods=['POST'])
@require_auth
def admin_agent_unlock():
    try:
        data = request.get_json()
        agent_id = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE agents SET is_locked=0, login_attempts=0 WHERE id=%s", (agent_id,))
        conn.commit()
        conn.close()
        return json_response(message='解锁成功')
    except Exception as e:
        logger.error(f'[agent_unlock] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/agent/reset-password', methods=['POST'])
@require_auth
def admin_agent_reset_password():
    try:
        data = request.get_json()
        agent_id = data.get('id')
        new_pwd = 'Agt@' + ''.join(random.choices(string.ascii_letters + string.digits, k=2))
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE agents SET password_hash=%s, plain_password=%s, is_locked=0, login_attempts=0 WHERE id=%s",
                  (generate_password_hash(new_pwd), new_pwd, agent_id))
        conn.commit()
        conn.close()
        return json_response(message='密码已重置', data={'password': new_pwd})
    except Exception as e:
        logger.error(f'[agent_reset_pwd] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/merchant/unlock', methods=['POST'])
@require_auth
def admin_merchant_unlock():
    try:
        data = request.get_json()
        merchant_id = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE merchants SET is_locked=0, login_attempts=0 WHERE id=%s", (merchant_id,))
        conn.commit()
        conn.close()
        return json_response(message='解锁成功')
    except Exception as e:
        logger.error(f'[merchant_unlock] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/merchant/reset-password', methods=['POST'])
@require_auth
def admin_merchant_reset_password():
    try:
        data = request.get_json()
        merchant_id = data.get('id')
        new_pwd = 'Mch@' + ''.join(random.choices(string.ascii_letters + string.digits, k=2))
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE merchants SET password_hash=%s, plain_password=%s, is_locked=0, login_attempts=0 WHERE id=%s",
                  (generate_password_hash(new_pwd), new_pwd, merchant_id))
        conn.commit()
        conn.close()
        return json_response(message='密码已重置', data={'password': new_pwd})
    except Exception as e:
        logger.error(f'[merchant_reset_pwd] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/employee/unlock', methods=['POST'])
@require_auth
def admin_employee_unlock():
    try:
        data = request.get_json()
        emp_id = data.get('id')
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE employees SET is_locked=0, login_attempts=0 WHERE id=%s", (emp_id,))
        conn.commit()
        conn.close()
        return json_response(message='解锁成功')
    except Exception as e:
        logger.error(f'[employee_unlock] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/employee/reset-password', methods=['POST'])
@require_auth
def admin_employee_reset_password():
    try:
        data = request.get_json()
        emp_id = data.get('id')
        new_pwd = 'Emp@' + ''.join(random.choices(string.ascii_letters + string.digits, k=2))
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE employees SET password_hash=%s, plain_password=%s, is_locked=0, login_attempts=0 WHERE id=%s",
                  (generate_password_hash(new_pwd), new_pwd, emp_id))
        conn.commit()
        conn.close()
        return json_response(message='密码已重置', data={'password': new_pwd})
    except Exception as e:
        logger.error(f'[employee_reset_pwd] {e}')
        return json_response(message=str(e), code=500)

# ==================== P1: 批量自动提现 ====================

@bp.route('/admin/withdrawal/batch-auto', methods=['POST'])
def withdrawal_batch_auto():
    """?????????????(???+??)?????(????)??????"""
    try:
        from database import get_db
        import random as _rnd
        conn = get_db()
        c = conn.cursor()
        approved = 0
        rejected = 0
        
        # 1. ?????auto_approve_time ?????
        rows = c.execute("""
            SELECT w.id, w.user_phone, w.amount, w.order_id, w.auto_approve_time,
                   l.refund_approve_rate
            FROM withdrawal_records w
            JOIN orders o ON w.order_id = o.id
            JOIN cabinets cb ON o.cabinet_id = cb.id
            JOIN locations l ON cb.location_id = l.id
            WHERE w.status = 0 AND l.withdraw_mode = 'queue_approve'
            AND w.auto_approve_time IS NOT NULL
            AND datetime(w.auto_approve_time) <= datetime('now')
        """).fetchall()
        for r in rows:
            rate = (r['refund_approve_rate'] or 80) / 100.0
            if _rnd.random() < rate:
                # ????????????
                from helpers import do_real_refund
                order_id = r['order_id']
                amt = r['amount']
                # ??????
                c2 = conn.cursor()
                c2.execute('SELECT o.order_no, o.payment_channel_id FROM orders o WHERE id=%s', (order_id,))
                ord = c2.fetchone()
                if ord:
                    success, refund_id, msg = do_real_refund(order_id=order_id, order_no=ord['order_no'], amount=amt, payment_channel_id=ord['payment_channel_id'])
                    c2.execute("UPDATE withdrawal_records SET status=2, approve_time=datetime('now'), approver='自动' WHERE id=%s", (r['id'],))
                    if success:
                        c2.execute("UPDATE orders SET status=4, refund_id=%s, refund_time=datetime('now') WHERE id=%s", (refund_id, order_id))
                    approved += 1
            else:
                # ?????????????
                # 统一用 mp_openid 查找
                _brj_mp = None
                c.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (r['user_phone'],))
                _brj_r = c.fetchone()
                if _brj_r:
                    _brj_mp = _brj_r['mp_openid']
                if _brj_mp:
                    c.execute("UPDATE user_balances SET balance = balance + %s, total_withdrawn = total_withdrawn - %s WHERE mp_openid = %s",
                              (r['amount'], r['amount'], _brj_mp))
                else:
                    c.execute("UPDATE user_balances SET balance = balance + %s, total_withdrawn = total_withdrawn - %s WHERE phone = %s",
                              (r['amount'], r['amount'], r['user_phone']))
                c.execute("UPDATE withdrawal_records SET status=3, approve_time=datetime('now'), approver='队列' WHERE id=%s", (r['id'],))
                rejected += 1
        conn.commit()
        
        # 2. ?????????? >= 80% ?????????????
        rows2 = c.execute("""
            SELECT w.id FROM withdrawal_records w
            JOIN orders o ON w.order_id = o.id
            JOIN cabinets cb ON o.cabinet_id = cb.id
            JOIN locations l ON cb.location_id = l.id
            WHERE w.status = 0 AND l.withdraw_mode = 'manual_approve' AND l.auto_approve_rate >= 80
        """).fetchall()
        for r in rows2:
            c.execute("UPDATE withdrawal_records SET status=1, approve_time=datetime('now'), approver='人工' WHERE id=%s", (r['id'],))
            approved += 1
        
        conn.commit()
        conn.close()
        return jsonify({'code': 200, 'message': f'????{approved}????{rejected}?', 'data': {'approved': approved, 'rejected': rejected}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})



# ==================== P1: 远程开门日志 ====================

@bp.route('/admin/merchant-share-stats', methods=['GET'])
@require_auth
def admin_merchant_share_stats():
    """商家分成统计"""
    try:
        agent_id = request.args.get('agent_id', '')
        merchant_id = request.args.get('merchant_id', '')
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        
        conn = get_db()
        c = conn.cursor()
        
        where_parts = ["o.logic_mark IS NULL OR o.logic_mark != 'Y'"]
        params = []
        
        if agent_id:
            where_parts.append('l.agent_id=%s')
            params.append(agent_id)
        if merchant_id:
            where_parts.append('l.merchant_id=%s')
            params.append(merchant_id)
        if start_date and end_date:
            where_parts.append("date(o.created_at) >= %s AND date(o.created_at) <= %s")
            params.extend([start_date, end_date])
        else:
            where_parts.append("o.created_at >= NOW() - INTERVAL '30 days'")
        
        where_clause = ' WHERE ' + ' AND '.join(where_parts)
        
        c.execute(f'''SELECT 
            loc.id as location_id,
            loc.name as location_name,
            loc.merchant_id,
            m.name as merchant_name,
            COALESCE(m.commission_per_order, 0) as commission_per_order,
            a.name as agent_name,
            COUNT(o.id) as order_count,
            COALESCE(SUM(o.deposit_amount), 0) as deposit_total,
            COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END), 0) as refund_total,
            ROUND(COUNT(o.id) * COALESCE(m.commission_per_order, 0), 2) as share_total
            FROM orders o
            LEFT JOIN cabinets cab ON o.cabinet_id = cab.id
            LEFT JOIN locations loc ON cab.location_id = loc.id
            LEFT JOIN merchants m ON loc.merchant_id = m.id
            LEFT JOIN agents a ON loc.agent_id = a.id
            {where_clause}
            GROUP BY loc.id, loc.name, loc.merchant_id, m.name, m.commission_per_order, a.name
            ORDER BY loc.name''', params)
        
        details = []
        orders = []
        for r in c.fetchall():
            details.append({
                'location_id': r[0],
                'location_name': r[1],
                'merchant_id': r[2],
                'merchant_name': r[3],
                'commission_per_order': float(r[4] or 0),
                'agent_name': r[5],
                'order_count': r[6],
                'deposit_total': float(r[7] or 0),
                'refund_total': float(r[8] or 0),
                'share_total': float(r[9] or 0)
            })
        
        # Summary
        total_orders = sum(d['order_count'] for d in details)
        total_share = sum(d['share_total'] for d in details)
        
        conn.close()
        return json_response(data={
            'details': details,
            'summary': {'total_orders': total_orders, 'total_share': total_share}
        })
    except Exception as e:
        logger.error(f'[merchant_share_stats] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/remote-open-logs', methods=['GET', 'POST'])
def remote_open_logs_list():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        location_id = request.args.get('location_id', '').strip()
        cabinet_id = request.args.get('cabinet_id', '').strip()
        date_start = request.args.get('date_start', '').strip()
        date_end = request.args.get('date_end', '').strip()

        # default: last 3 days
        if not date_start and not date_end:
            from datetime import datetime, timedelta
            date_start = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
            date_end = datetime.now().strftime('%Y-%m-%d')

        where_clauses = []
        params = []

        if date_start:
            where_clauses.append("rol.created_at >= ?")
            params.append(date_start + " 00:00:00")
        if date_end:
            where_clauses.append("rol.created_at <= ?")
            params.append(date_end + " 23:59:59")
        if location_id:
            where_clauses.append("c.location_id = ?")
            params.append(int(location_id))
        if cabinet_id:
            where_clauses.append("rol.device_id = ?")
            params.append(cabinet_id)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        count_sql = "SELECT COUNT(*) FROM remote_open_logs rol LEFT JOIN cabinets c ON rol.device_id = c.mainboard_device_id WHERE " + where_sql
        total = c.execute(count_sql, params).fetchone()[0]

        query_sql = ("SELECT rol.id, rol.device_id, rol.slot_id, "
                     "COALESCE(cs.slot_number::text, rol.slot_number) AS slot_number, "
                     "cs.slot_size, "
                     "rol.action_type, rol.operator, rol.result, rol.success, "
                     "rol.ip_address, "
                     "rol.created_at, "
                     "c.cabinet_code, c.name AS cabinet_name, "
                     "c.location_id, l.name AS location_name "
                     "FROM remote_open_logs rol "
                     "LEFT JOIN cabinets c ON rol.device_id = c.mainboard_device_id "
                     "LEFT JOIN locations l ON c.location_id = l.id "
                     "LEFT JOIN cabinet_slots cs ON rol.slot_id = cs.id "
                     "WHERE " + where_sql + " ORDER BY rol.id DESC LIMIT ? OFFSET ?")
        raw_rows = c.execute(query_sql, params + [limit, (page-1)*limit]).fetchall()
        result_list = []
        for r in raw_rows:
            d = dict(r)
            ca = d.get('created_at')
            if ca and hasattr(ca, 'strftime'):
                d['created_at'] = ca.strftime('%Y-%m-%d %H:%M:%S')
            elif ca and isinstance(ca, str) and len(ca) > 19:
                d['created_at'] = ca[:19]
            result_list.append(d)
        rows = result_list
        conn.close()
        return jsonify({'code': 200, 'data': {'list': rows, 'total': total}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ==================== P1: 设备日志查看 ====================

@bp.route('/admin/device-logs', methods=['GET', 'POST'])
def device_logs_list():
    try:
        cabinet_id = request.args.get('cabinet_id', '')
        device_id = request.args.get('device_id', '')
        log_type = request.args.get('log_type', '')
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 10))
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        where = " WHERE d.create_time >= NOW() - INTERVAL '3 days'"
        params = []
        
        if cabinet_id:
            where += " AND d.cabinet_id=%s"
            params.append(cabinet_id)
        if device_id:
            where += " AND d.device_id LIKE %s"
            params.append('%' + device_id + '%')
        if log_type:
            where += " AND d.log_type=%s"
            params.append(log_type)
        
        sql = "SELECT d.id, d.device_id, d.log_type, d.content, d.create_time, cab.id as cabinet_id, cab.cabinet_code, COALESCE(loc.name, '') as location_name FROM device_logs d LEFT JOIN cabinets cab ON d.device_id = cab.mainboard_device_id OR d.device_id = CAST(cab.id AS TEXT) LEFT JOIN locations loc ON cab.location_id = loc.id" + where + " ORDER BY d.id DESC LIMIT %s OFFSET %s"
        
        total_sql = "SELECT COUNT(*) FROM device_logs d LEFT JOIN cabinets cab ON d.device_id = cab.mainboard_device_id OR d.device_id = CAST(cab.id AS TEXT) " + where
        total = c.execute(total_sql, params).fetchone()[0]
        rows = c.execute(sql, params + [limit, (page-1)*limit]).fetchall()
        
        logs = []
        for r in rows:
            logs.append({'id': r[0], 'device_id': r[1], 'log_type': r[2], 'content': r[3], 'created_at': r[4], 'cabinet_id': r[5], 'cabinet_code': r[6] or '', 'location_name': r[7] or ''})
        
        conn.close()
        return jsonify({'code': 200, 'data': {'list': logs, 'total': total}})
    except Exception as e:
        logger.error('[device_logs] ' + str(e))
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== P1: 开门记录 ====================

# ==================== P1: 开门记录 ====================

@bp.route('/admin/door-records', methods=['GET', 'POST'])
def door_records_list():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        cabinet_id = request.args.get('cabinet_id', '')
        order_id = request.args.get('order_id', '')
        conn = get_db()
        c = conn.cursor()
        where = "WHERE 1=1"
        params = []
        if cabinet_id:
            where += " AND cabinet_id=%s"
            params.append(cabinet_id)
        if order_id:
            # order_id可能是数字id，先查order_no
            is_int = False
            try:
                oid = int(order_id)
                # 确保是合理范围内的整数（PostgreSQL integer）
                if oid < 2000000000:
                    is_int = True
            except ValueError:
                pass
            if is_int:
                row = c.execute('SELECT order_no FROM orders WHERE id=%s', (oid,)).fetchone()
                if row:
                    # 同时查 order_no(时间戳ID) 和 order_id(数据库ID)
                    where += " AND (order_id=%s OR order_id=%s)"
                    params.extend([str(row['order_no']), str(order_id)])
                else:
                    where += " AND order_id=%s"
                    params.append(str(order_id))
            else:
                # 非数字: 可能是order_no(时间戳),同时查对应的order_id
                o_str = str(order_id)
                id_row = c.execute('SELECT id FROM orders WHERE order_no=%s', (o_str,)).fetchone()
                if id_row:
                    where += " AND (order_id=%s OR order_id=%s)"
                    params.extend([o_str, str(id_row['id'])])
                else:
                    where += " AND order_id=%s"
                    params.append(o_str)
        total = c.execute(f"SELECT COUNT(*) FROM door_records {where}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM door_records {where} ORDER BY id DESC LIMIT %s OFFSET %s", params + [limit, (page-1)*limit]).fetchall()
        conn.close()
        return json_response(data={'list': [dict(r) for r in rows], 'total': total})
    except Exception as e:
        logger.error(f'[door_records] {e}')
        return json_response(message=str(e), code=500)



# ==================== 告警管理 ====================

# @bp.route('/admin/alerts', methods=['GET', 'POST'])
def alarms_list():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 10))
        alert_type = request.args.get('alert_type', '')
        device_id = request.args.get('device_id', '')
        days = int(request.args.get('days', 3))
        logger.info(f'[alarms_list] DB_PATH={DB_PATH}, page={page}, limit={limit}, device={device_id}, type={alert_type}, days={days}')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS device_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            alert_type TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # Device summaries: latest event per device
        summary_rows = c.execute(
            """SELECT a.device_id, a.alert_type, a.detail, a.created_at as last_time
               FROM device_alerts a
               INNER JOIN (SELECT device_id, MAX(created_at) as max_time FROM device_alerts GROUP BY device_id) b
               ON a.device_id = b.device_id AND a.created_at = b.max_time
               ORDER BY a.created_at DESC"""
        ).fetchall()
        device_summaries = []
        for r in summary_rows:
            d = dict(r)
            ts = d.get('last_time', '')
            if ts and ' ' in str(ts):
                d['last_time'] = str(ts)[:19]
            device_summaries.append(d)
        # 从PostgreSQL devices表补充实时在线状态
        try:
            pg = get_db()
            pgc = pg.cursor()
            pgc.execute("SELECT device_id, status, update_time FROM devices WHERE status IS NOT NULL ORDER BY update_time DESC")
            for r in pgc.fetchall():
                did = r[0]
                st = r[1]
                ut = str(r[2])[:19] if r[2] else ''
                found = False
                for s in device_summaries:
                    if s['device_id'] == did:
                        s['pg_status'] = st
                        s['pg_time'] = ut
                        found = True
                        break
                if not found:
                    device_summaries.append({'device_id': did, 'alert_type': st, 'last_time': ut, 'pg_status': st, 'pg_time': ut})
            pg.close()
        except Exception as _pe:
            logger.error(f'[devices_status] {_pe}')
        # Filtered list (default: last N days)
        where = "WHERE 1=1"
        params = []
        if device_id:
            where += " AND device_id=?"
            params.append(device_id)
        if alert_type:
            where += " AND alert_type=?"
            params.append(alert_type)
        if days > 0:
            where += " AND created_at >= datetime('now', '-" + str(days) + " days')"
        total = c.execute(f"SELECT COUNT(*) FROM device_alerts {where}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM device_alerts {where} ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, (page-1)*limit]).fetchall()
        result_list = []
        # Cache cabinet info from PostgreSQL
        cabinet_cache = {}
        try:
            cache_conn = get_db()
            cache_cur = cache_conn.cursor()
            cache_cur.execute("SELECT mainboard_device_id, cabinet_code, name, location_id FROM cabinets WHERE mainboard_device_id IS NOT NULL")
            for cr in cache_cur.fetchall():
                cabinet_cache[cr[0]] = {'cabinet_code': cr[1] or '', 'cabinet_name': cr[2] or '', 'location_id': cr[3]}
            cache_conn.close()
            # Get location names
            cache_cur2 = get_db()
            for did, cinfo in cabinet_cache.items():
                if cinfo.get('location_id'):
                    cache_cur2.execute("SELECT name FROM locations WHERE id=%s", (cinfo['location_id'],))
                    lr = cache_cur2.fetchone()
                    cinfo['location_name'] = lr[0] if lr else ''
            cache_cur2.close()
        except:
            pass
        for r in rows:
            d = dict(r)
            ts = d.get('created_at', '')
            if ts and ' ' in str(ts):
                d['created_at'] = str(ts)[:19]
            did = d.get('device_id', '')
            if did in cabinet_cache:
                d['cabinet_code'] = cabinet_cache[did].get('cabinet_code', '')
                d['cabinet_name'] = cabinet_cache[did].get('cabinet_name', '')
                d['location_name'] = cabinet_cache[did].get('location_name', '')
            result_list.append(d)
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ==================== P1: 待执行命令监控 ====================

@bp.route('/admin/pending-cmds', methods=['GET', 'POST'])
def pending_cmds_list():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        rows = c.execute("SELECT * FROM pending_lock_cmds ORDER BY id DESC LIMIT 100").fetchall()
        conn.close()
        return jsonify({'code': 200, 'data': {'list': [dict(r) for r in rows], 'total': len(rows)}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.route('/admin/cancel-cmd', methods=['POST'])
def admin_cancel_cmd():
    data = request.get_json() or {}
    cmd_id = data.get('id')
    if not cmd_id:
        return jsonify({'code': 1, 'message': '缺少命令ID'})
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute('UPDATE pending_lock_cmds SET delivered=2 WHERE id=%s AND delivered=0', (cmd_id,))
        db.commit()
        return jsonify({'code': 0, 'message': '命令已取消'})
    except Exception as e:
        return jsonify({'code': 1, 'message': str(e)})
    finally:
        db.close()

# ==================== 柜组管理 ====================

@bp.route('/admin/cabinet-groups', methods=['GET', 'POST'])
@require_auth
def admin_cabinet_groups():
    try:
        location_id = request.args.get('location_id', '')
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('limit', 20))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if location_id:
            where += ' AND cg.location_id=%s'
            params.append(location_id)
        c.execute(f'SELECT COUNT(*) FROM cabinet_groups cg WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT cg.*, l.name as location_name,
            (SELECT COUNT(*) FROM cabinets WHERE group_id=cg.id) as cabinet_count,
            COALESCE((SELECT SUM(CASE WHEN cs.status=1 THEN 1 ELSE 0 END) FROM cabinet_slots cs JOIN cabinets cab ON cs.cabinet_id=cab.id WHERE cab.group_id=cg.id),0) as available_slots,
            COALESCE((SELECT SUM(CASE WHEN cs.status=2 THEN 1 ELSE 0 END) FROM cabinet_slots cs JOIN cabinets cab ON cs.cabinet_id=cab.id WHERE cab.group_id=cg.id),0) as occupied_slots
            FROM cabinet_groups cg LEFT JOIN locations l ON cg.location_id=l.id
            WHERE {where} ORDER BY cg.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        groups = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': groups, 'total': total})
    except Exception as e:
        logger.error(f'[cabinet_groups] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/cabinet-group/save', methods=['POST'])
@require_auth
def admin_cabinet_group_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if _had_id:
            sets, params = [], []
            for f in ['name', 'screen_url', 'status', 'location_id']:
                if f in data:
                    sets.append(f'{f}=%s')
                    params.append(data[f])
            params.append(data['id'])
            c.execute(f'UPDATE cabinet_groups SET {",".join(sets)} WHERE id=%s', params)
        else:
            group_code = data.get('group_code', '')
            if not group_code:
                import time
                group_code = 'G' + str(int(time.time() * 100))[-6:]
            c.execute('SELECT id FROM cabinet_groups WHERE group_code=%s', (group_code,))
            if c.fetchone():
                conn.close()
                return json_response(message='柜组编号已存在', code=400)
            c.execute('INSERT INTO cabinet_groups (location_id, group_code, name, screen_url) VALUES (%s,%s,%s,%s)',
                      (data.get('location_id'), group_code, data.get('name', group_code), data.get('screen_url', '')))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[cabinet_group_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/cabinet-group/delete', methods=['POST'])
@require_auth
def admin_cabinet_group_delete():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM cabinets WHERE group_id=%s', (data.get('id'),))
        if c.fetchone()[0] > 0:
            conn.close()
            return json_response(message='该柜组下存在柜体，请先删除柜体', code=400)
        c.execute('DELETE FROM cabinet_groups WHERE id=%s', (data.get('id'),))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[cabinet_group_delete] {e}')
        return json_response(message=str(e), code=500)


# ==================== 设备详情 ====================

@bp.route('/admin/device/detail', methods=['GET', 'POST'])
@require_auth
def admin_device_detail():
    try:
        _d=request.get_json(silent=True) or {}; device_id = _d.get('id') or request.args.get('id', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM cabinets WHERE id=%s', (device_id,))
        cabinet = c.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='设备不存在', code=404)
        result = dict(cabinet)
        c.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s ORDER BY slot_number', (device_id,))
        result['slots'] = [dict(r) for r in c.fetchall()]
        c.execute('''SELECT o.*, cs.slot_number as compartment_number, ub.wechat_name FROM orders o
            LEFT JOIN cabinet_slots cs ON o.slot_id=cs.id
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone
            LEFT JOIN phone_openids po ON o.user_phone=po.phone
            LEFT JOIN user_profiles up ON po.openid=up.openid
            WHERE o.cabinet_id=%s AND o.status=2 ORDER BY o.created_at DESC''', (device_id,))
        result['active_orders'] = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[device_detail] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/device/slot-open', methods=['POST'])
@require_auth
def admin_device_slot_open():
    try:
        data = request.get_json()
        slot_id = data.get('slot_id')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT cs.*, c.mainboard_device_id, c.id as cabinet_id, c.last_heartbeat, c.mainboard_source FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id=c.id WHERE cs.id=%s', (slot_id,))
        slot = c.fetchone()
        if not slot:
            conn.close()
            return json_response(message='柜格不存在', code=404)
        device_id = slot['mainboard_device_id']
        # 检查设备是否在线
        from helpers import connected_devices
        is_online = device_id in connected_devices
        if not is_online and slot['last_heartbeat']:
            from datetime import datetime
            try:
                hb = slot['last_heartbeat']
                if isinstance(hb, str):
                    hb = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
                is_online = (datetime.now() - hb).total_seconds() < 120
            except:
                pass
        if not is_online:
            conn.close()
            return json_response(message='设备离线，请稍后再试', code=503)
        board_no = slot['board_no'] if 'board_no' in slot.keys() else 1
        lock_no = slot['lock_no'] if 'lock_no' in slot.keys() and slot['lock_no'] else (slot['slot_number'] if 'slot_number' in slot.keys() else 0)
        protocol = slot.get('mainboard_source') or None
        c.execute("INSERT INTO remote_open_logs (device_id, slot_id, action_type, operator, success, created_at) VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)",
                  (device_id, slot_id, 'admin_open', 'admin', 1))
        conn.commit()
        conn.close()
        from helpers import send_open_lock
        send_open_lock(device_id, board_no, lock_no, protocol)
        logger.info(f'[slot_open] sent via send_open_lock: device={device_id}, board={board_no}, lock={lock_no}')
        return json_response(message='开门指令已发送')
    except Exception as e:
        logger.error(f'[slot_open] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/device/batch-open', methods=['POST'])
@require_auth
def admin_device_batch_open():
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        slot_ids = data.get('slot_ids', [])
        if not cabinet_id:
            return json_response(message='缺少设备ID', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM cabinets WHERE id=%s', (cabinet_id,))
        cabinet = c.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='设备不存在', code=404)
        # 检查设备是否在线
        device_id = cabinet['mainboard_device_id']
        from helpers import connected_devices
        is_online = device_id in connected_devices
        if not is_online and cabinet['last_heartbeat']:
            from datetime import datetime
            try:
                hb = cabinet['last_heartbeat']
                if isinstance(hb, str):
                    hb = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
                is_online = (datetime.now() - hb).total_seconds() < 120
            except:
                pass
        if not is_online:
            conn.close()
            return json_response(message='设备离线，请稍后再试', code=503)
        import json as _json
        opened = 0
        if slot_ids:
            for sid in slot_ids:
                c.execute('SELECT * FROM cabinet_slots WHERE id=%s AND cabinet_id=%s', (sid, cabinet_id))
                slot = c.fetchone()
                if slot:
                    cmd = _json.dumps({'type': 'open_lock', 'device_id': cabinet['mainboard_device_id'], 'slot_number': slot['slot_number']})
                    c.execute('INSERT INTO pending_lock_cmds (cabinet_id, slot_id, command, status) VALUES (%s,%s,%s,%s)',
                              (cabinet_id, sid, cmd, 'pending'))
                    c.execute("INSERT INTO remote_open_logs (device_id, slot_id, action_type, operator, success, created_at) VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)",
                              (cabinet['mainboard_device_id'], sid, 'admin_batch_open', 'admin', 1))
                    opened += 1
        else:
            c.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
            for slot in c.fetchall():
                cmd = _json.dumps({'type': 'open_lock', 'device_id': cabinet['mainboard_device_id'], 'slot_number': slot['slot_number']})
                c.execute('INSERT INTO pending_lock_cmds (cabinet_id, slot_id, command, status) VALUES (%s,%s,%s,%s)',
                          (cabinet_id, slot['id'], cmd, 'pending'))
                opened += 1
        conn.commit()
        conn.close()
        return json_response(message=f'已发送{opened}个开门指令')
    except Exception as e:
        logger.error(f'[batch_open] {e}')
        return json_response(message=str(e), code=500)


# ==================== 网点二维码 ====================


@bp.route('/admin/device/qrcode', methods=['GET','POST'])
@require_auth
def admin_device_qrcode():
    try:
        _jd=request.get_json(silent=True) or {}; device_id = _jd.get('id') or request.args.get('id', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM cabinets WHERE id=%s', (device_id,))
        cabinet = c.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='设备不存在', code=404)
        result = dict(cabinet)
        # Build QR URL for this device
        group_id = cabinet['group_id'] if 'group_id' in cabinet.keys() else None
        group_code = ''
        if group_id:
            c.execute('SELECT group_code FROM cabinet_groups WHERE id=%s', (group_id,))
            g = c.fetchone()
            if g:
                group_code = g['group_code']
        qr_url = 'https://locker.cqdyxl.com/store?group_code=' + group_code + '&cabinet_id=' + str(device_id) if group_code else 'https://locker.cqdyxl.com/store?cabinet_id=' + str(device_id)
        result['qr_url'] = qr_url
        conn.close()
        # Generate QR code image as base64 (with mainboard_device_id label at bottom)
        try:
            import qrcode
            from io import BytesIO
            import base64
            from PIL import Image, ImageDraw, ImageFont
            qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
            qr_w, qr_h = qr_img.size
            # Create canvas with extra space for label at bottom
            label_h = 60
            canvas = Image.new('RGB', (qr_w, qr_h + label_h), 'white')
            canvas.paste(qr_img, (0, 0))
            # Draw mainboard_device_id label
            draw = ImageDraw.Draw(canvas)
            device_id_text = str(cabinet['mainboard_device_id'] or '')
            label_text = f"ID: {device_id_text}" if device_id_text else ""
            if label_text:
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
                except:
                    font = ImageFont.load_default()
                bbox = draw.textbbox((0, 0), label_text, font=font)
                text_w = bbox[2] - bbox[0]
                text_x = (qr_w - text_w) // 2
                text_y = qr_h + 18
                draw.text((text_x, text_y), label_text, fill='black', font=font)
            buf = BytesIO()
            canvas.save(buf, format='PNG')
            result['qrcode_img'] = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('utf-8')
        except Exception as qe:
            logger.error(f'[device_qrcode] generate image failed: {qe}')
            result['qrcode_img'] = None
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[device_qrcode] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/location/qrcode', methods=['GET','POST'])
@require_auth
def admin_location_qrcode():
    try:
        _jd2=request.get_json(silent=True) or {}; location_id = _jd2.get('location_id') or request.args.get('location_id', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM locations WHERE id=%s', (location_id,))
        location = c.fetchone()
        if not location:
            conn.close()
            return json_response(message='网点不存在', code=404)
        c.execute('SELECT cg.*, (SELECT COUNT(*) FROM cabinets WHERE group_id=cg.id) as cabinet_count FROM cabinet_groups cg WHERE cg.location_id=%s ORDER BY cg.created_at', (location_id,))
        groups = [dict(r) for r in c.fetchall()]
        base_url = 'https://locker.cqdyxl.com/store'
        for g in groups:
            g['qr_url'] = base_url + g['group_code']
        conn.close()
        return json_response(data={'location': dict(location), 'groups': groups})
    except Exception as e:
        logger.error(f'[location_qrcode] {e}')
        return json_response(message=str(e), code=500)




@bp.route('/admin/slot/add', methods=['POST'])
@require_auth
def admin_slot_add():
    """批量添加柜门：根据主板号+柜门数自动生成连续柜门号"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        slot_size = data.get('slot_size', 'medium')
        board_no = data.get('board_no', 1)
        slot_count = int(data.get('slot_count', 1))
        if not cabinet_id:
            return json_response(message='缺少设备ID', code=400)
        if not slot_count or slot_count < 1:
            return json_response(message='柜门数至少为1', code=400)
        conn = get_db()
        cur = conn.cursor()
        # 查当前该cabinet已有最大slot_number
        cur.execute('SELECT MAX(slot_number) as max_num FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
        row = cur.fetchone()
        max_num = row['max_num'] if row and row['max_num'] else 0
        # 从max_num+1开始连续生成slot_count个柜门
        added = 0
        for i in range(slot_count):
            slot_number = max_num + 1 + i
            lock_no = i + 1  # lock_no从1开始，对应主板上的物理锁号
            cur.execute('SELECT id FROM cabinet_slots WHERE cabinet_id=%s AND slot_number=%s', (cabinet_id, slot_number))
            if cur.fetchone():
                continue  # 跳过已存在的
            cur.execute('INSERT INTO cabinet_slots(cabinet_id,slot_number,slot_label,slot_size,board_no,lock_no,status) VALUES(%s,%s,%s,%s,%s,%s,1)',
                        (cabinet_id, slot_number, str(slot_number), slot_size, board_no, lock_no))
            added += 1
        # 同步更新 cabinets.total_slots
        cur.execute('SELECT COUNT(*) as cnt FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
        cnt = cur.fetchone()['cnt']
        cur.execute('UPDATE cabinets SET total_slots=%s WHERE id=%s', (cnt, cabinet_id))
        conn.commit()
        conn.close()
        return json_response(data={'added': added}, message=f'成功添加{added}个柜门(编号{max_num+1}-{max_num+slot_count})')
    except Exception as e:
        return json_response(message=str(e), code=500)

@bp.route('/admin/slot/delete', methods=['POST'])
@require_auth
def admin_slot_delete():
    try:
        data=request.get_json(); slot_id=data.get('id')
        if not slot_id: return json_response(message='缺少柜门ID',code=400)
        conn=get_db(); cur=conn.cursor()
        # 先获取cabinet_id
        cur.execute('SELECT cabinet_id FROM cabinet_slots WHERE id=%s', (slot_id,))
        row = cur.fetchone()
        cabinet_id = row['cabinet_id'] if row else None
        cur.execute('DELETE FROM cabinet_slots WHERE id=%s',(slot_id,))
        # 同步更新 cabinets.total_slots
        if cabinet_id:
            cur.execute('SELECT COUNT(*) as cnt FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
            cnt = cur.fetchone()['cnt']
            cur.execute('UPDATE cabinets SET total_slots=%s WHERE id=%s', (cnt, cabinet_id))
        conn.commit(); conn.close()
        return json_response(message='删除成功')
    except Exception as e: return json_response(message=str(e),code=500)



@bp.route('/admin/order/open-door', methods=['POST'])
@require_auth
def admin_order_open_door():
    """已结束订单开门：根据订单找到对应slot，调用开门逻辑"""
    try:
        data = request.get_json()
        order_id = data.get('order_id')
        if not order_id:
            return json_response(message='缺少订单ID', code=400)
        conn = get_db()
        c = conn.cursor()
        # 查订单获取slot_id
        c.execute('SELECT slot_id, cabinet_id, order_no FROM orders WHERE id=%s', (order_id,))
        order = c.fetchone()
        if not order:
            conn.close()
            return json_response(message='订单不存在', code=404)
        slot_id = order['slot_id']
        cabinet_id = order['cabinet_id']
        if not slot_id:
            conn.close()
            return json_response(message='订单无关联柜门', code=400)
        # 查slot信息
        c.execute('SELECT * FROM cabinet_slots WHERE id=%s', (slot_id,))
        slot = c.fetchone()
        if not slot:
            conn.close()
            return json_response(message='柜门不存在', code=404)
        # 查设备信息
        c.execute('SELECT mainboard_device_id FROM cabinets WHERE id=%s', (cabinet_id,))
        cabinet = c.fetchone()
        if not cabinet or not cabinet['mainboard_device_id']:
            conn.close()
            return json_response(message='设备未配置', code=400)
        device_id = cabinet['mainboard_device_id']
        board_no = slot['board_no']
        lock_no = slot['lock_no']
        conn.close()
        # 调用开门逻辑
        from helpers import send_open_lock
        result = send_open_lock(device_id, board_no, lock_no, order_id=order.get('order_no', str(order_id)))
        if result:
            return json_response(message='开门指令已发送', data={'success': True})
        else:
            return json_response(message='开门指令发送失败，设备可能离线', code=500)
    except Exception as e:
        return json_response(message=str(e), code=500)
@bp.route('/admin/slots/batch-delete', methods=['POST'])
@require_auth
def admin_slots_batch_delete():
    try:
        data = request.get_json()
        ids = data.get('ids', data.get('slot_ids', []))
        if not ids or not isinstance(ids, list):
            return json_response(message='请选择要删除的柜门', code=400)
        conn = get_db()
        c = conn.cursor()
        placeholders = ','.join(['%s'] * len(ids))
        # 先把关联订单的slot_id置空，避免外键约束
        c.execute(f'UPDATE orders SET slot_id=NULL WHERE slot_id IN ({placeholders})', ids)
        # 先获取涉及的cabinet_id列表
        c.execute(f'SELECT DISTINCT cabinet_id FROM cabinet_slots WHERE id IN ({placeholders})', ids)
        cabinet_ids = [r['cabinet_id'] for r in c.fetchall() if r['cabinet_id']]
        c.execute(f'DELETE FROM cabinet_slots WHERE id IN ({placeholders})', ids)
        deleted = c.rowcount
        # 同步更新涉及设备的total_slots
        for cid in cabinet_ids:
            c.execute('SELECT COUNT(*) as cnt FROM cabinet_slots WHERE cabinet_id=%s', (cid,))
            cnt = c.fetchone()['cnt']
            c.execute('UPDATE cabinets SET total_slots=%s WHERE id=%s', (cnt, cid))
        conn.commit()
        conn.close()
        return json_response(data={'deleted': deleted}, message=f'成功删除{deleted}个柜门')
    except Exception as e:
        return json_response(message=str(e), code=500)

# ==================== 补充V1缺失功能 ====================

@bp.route('/admin/device/clear-all', methods=['POST'])
@require_auth
def admin_device_clear_all():
    """清柜: 结束所有活跃订单+退押金+通知用户+开所有门 [Agent-modified 2026-07-04]"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        if not cabinet_id:
            return json_response(message='缺少设备ID', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM cabinets WHERE id=%s', (cabinet_id,))
        cabinet = c.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message='设备不存在', code=404)
        import json as _json
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # [Agent-modified 2026-07-04] 查询所有活跃订单(使用中2+已结算3)的完整信息
        c.execute("""SELECT id, order_no, slot_id, user_phone, deposit_amount, openid, unionid, status, compartment_number
                     FROM orders WHERE cabinet_id=%s AND status IN (2,3)""", (cabinet_id,))
        active = c.fetchall()
        ended = 0
        notified = 0
        for o in active:
            o_dict = dict(o)
            # 结束订单
            c.execute('UPDATE orders SET status=3, retrieve_time=%s, pickup_time=%s, updated_at=%s WHERE id=%s',
                      (now, now, now, o_dict['id']))
            # 释放格口为空闲
            if o_dict.get('slot_id'):
                c.execute('UPDATE cabinet_slots SET status=1 WHERE id=%s', (o_dict['slot_id'],))
            # 退押金到余额（使用中的订单押金未退，已结算的已退过余额不再重复退）
            deposit_amount = o_dict.get('deposit_amount', 0)
            if deposit_amount > 0 and o_dict.get('user_phone') and o_dict.get('status') == 2:
                # 统一用 mp_openid 查找用户余额
                _bc_mp_openid = o_dict.get('mp_openid', '') or o_dict.get('openid', '')
                if not _bc_mp_openid:
                    c.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' LIMIT 1", (o_dict['user_phone'],))
                    _bc_r = c.fetchone()
                    if _bc_r and _bc_r['mp_openid']:
                        _bc_mp_openid = _bc_r['mp_openid']
                c.execute('SELECT id FROM user_balances WHERE mp_openid = %s', (_bc_mp_openid,))
                ub_row = c.fetchone()
                if not ub_row:
                    c.execute('SELECT id FROM user_balances WHERE phone = %s', (o_dict['user_phone'],))
                    ub_row = c.fetchone()
                if ub_row:
                    c.execute("""UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s,
                                 mp_openid = COALESCE(NULLIF(mp_openid, ''), %s),
                                 openid = COALESCE(NULLIF(openid, ''), %s), unionid = COALESCE(NULLIF(unionid, ''), %s)
                                 WHERE id = %s""",
                              (deposit_amount, deposit_amount, _bc_mp_openid, o_dict.get('openid', ''), o_dict.get('unionid', ''), ub_row['id']))
                else:
                    c.execute("""INSERT INTO user_balances (phone, openid, unionid, mp_openid, balance, total_deposited, total_withdrawn, first_use_time)
                                 VALUES (%s, %s, %s, %s, %s, %s, 0, NOW())""",
                              (o_dict['user_phone'], o_dict.get('openid', ''), o_dict.get('unionid', ''), _bc_mp_openid, deposit_amount, deposit_amount))
                c.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING",
                          (o_dict['user_phone'], o_dict['id'], deposit_amount))
            # 发送微信订阅消息通知用户
            if o_dict.get('openid'):
                try:
                    from helpers import send_wx_subscribe_message
                    subscribe_data = {
                        'amount6': {'value': '¥{:.2f}'.format(deposit_amount)},
                        'time4': {'value': now},
                        'thing7': {'value': '已退还至小程序用户钱包'},
                        'thing2': {'value': '请自行点击此通知消息跳转“我的钱包”提现'}
                    }
                    send_wx_subscribe_message(o_dict['openid'], '5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A', subscribe_data, phone=o_dict.get('user_phone'))
                    # 退款通知在用户提现时发送，不在清柜时发送
                    notified += 1
                except Exception as e:
                    logger.error(f'[clear_all] 发送订阅消息失败 order={o_dict["id"]}: {e}')
            ended += 1
        # Open all slots
        c.execute('SELECT * FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
        slots = c.fetchall()
        opened = 0
        for s in slots:
            cmd = _json.dumps({'type': 'open_lock', 'device_id': cabinet['mainboard_device_id'], 'slot_number': s['slot_number'], 'slot_label': s['slot_label'] if 'slot_label' in s.keys() else ''})
            c.execute('INSERT INTO pending_lock_cmds (cabinet_id, slot_id, command, status) VALUES (%s,%s,%s,%s)',
                      (cabinet_id, s['id'], cmd, 'pending'))
            opened += 1
        conn.commit()
        conn.close()
        return json_response(message=f'已结束{ended}个订单（通知{notified}人），发送{opened}个开门指令')
    except Exception as e:
        logger.error(f'[clear_all] {e}')
        return json_response(message=str(e), code=500)



@bp.route("/admin/device/unbind", methods=["POST"])
@require_auth
def admin_device_unbind():
    from datetime import datetime
    """远程解绑设备"""
    try:
        data = request.get_json()
        cabinet_id = data.get("cabinet_id")
        if not cabinet_id:
            return json_response(message="缺少设备ID", code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM cabinets WHERE id=%s", (cabinet_id,))
        cabinet = c.fetchone()
        if not cabinet:
            conn.close()
            return json_response(message="设备不存在", code=404)
        device_id = cabinet["mainboard_device_id"]
        # 先通过WebSocket发送(实时),同时写入pending_lock_cmds(HTTP轮询兜底)
        if device_id:
            unbind_cmd = {"type":"unbind","device_id":device_id,"message":"该设备已被远程解绑，请在屏上重新设置","timestamp":str(datetime.now())}
            cmd_json = json.dumps(unbind_cmd)
            if device_id in connected_devices:
                ws = connected_devices[device_id]
                try:
                    ws.send(cmd_json)
                    logger.info(f"[解绑] 已发送解绑指令(WS): device={device_id}")
                except Exception as e:
                    logger.error(f"[解绑] 发送解绑指令失败(WS): {e}")
            # 写入pending_lock_cmds,设备通过HTTP轮询也能收到
            import json as _json
            c.execute("INSERT INTO pending_lock_cmds (device_id, board_no, lock_no, protocol, order_id, command, delivered) VALUES (%s,%s,%s,%s,%s,%s,0)",
                     (device_id, "", "", "", "", cmd_json))
            logger.info(f"[解绑] 已写入pending_lock_cmds: device={device_id}")
        c.execute("UPDATE cabinets SET business_status='inactive' WHERE id=%s", (cabinet_id,))
        conn.commit()
        conn.close()
        return json_response(message="远程解绑指令已发送")
    except Exception as e:
        logger.error(f"[解绑] 错误: {e}")
        return json_response(message=str(e), code=500)


@bp.route('/admin/slot/update-status', methods=['POST'])
@require_auth
def admin_slot_update_status():
    """手动更新格口状态"""
    try:
        data = request.get_json()
        slot_id = data.get('slot_id')
        status = data.get('status')  # 1=空闲 2=占用 3=故障
        if not slot_id or status not in [1, 2, 3]:
            return json_response(message='参数错误', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE cabinet_slots SET status=%s WHERE id=%s', (status, slot_id))
        if c.rowcount == 0:
            conn.close()
            return json_response(message='格口不存在', code=404)
        conn.commit()
        conn.close()
        return json_response(message='状态已更新')
    except Exception as e:
        logger.error(f'[slot_update_status] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/logout', methods=['POST'])
@require_auth
def admin_logout():
    """管理员登出"""
    return json_response(message='已登出')


@bp.route("/admin/transactions", methods=["GET"])
@require_auth
def admin_transactions():
    """结算流水列表"""
    try:
        db = get_db()
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 20))
        offset = (page - 1) * per_page
        
        where = "1=1"
        params = []
        cabinet_id = request.args.get("cabinet_id")
        if cabinet_id:
            where += " AND o.cabinet_id = %s"
            params.append(cabinet_id)
        status = request.args.get("status")
        if status:
            where += " AND o.status = %s"
            params.append(status)
        start_date = request.args.get("start_date")
        if start_date:
            where += " AND o.created_at >= %s"
            params.append(start_date)
        end_date = request.args.get("end_date")
        if end_date:
            where += " AND o.created_at <= %s"
            params.append(end_date + " 23:59:59")
        
        c.execute(f'SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone LEFT JOIN phone_openids po ON o.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
        rows = db.execute(
            f"SELECT o.id, o.order_no, o.cabinet_id, o.slot_id, o.compartment_number, "
            f"o.deposit_amount, o.status, o.access_code, o.created_at, o.retrieve_time, "
            f"c.cabinet_code "
            f"FROM orders o LEFT JOIN cabinets c ON o.cabinet_id = c.id "
            f"WHERE {where} ORDER BY o.created_at DESC LIMIT %s OFFSET %s",
            params + [per_page, offset]
        ).fetchall()
        
        items = []
        for r in rows:
            items.append({
                "id": r["id"], "order_no": r["order_no"],
                "cabinet_id": r["cabinet_id"], "cabinet_code": r["cabinet_code"],
                "slot_id": r["slot_id"], "compartment_number": r["compartment_number"],
                "deposit_amount": r["deposit_amount"], "status": r["status"],
                "retrieve_code": r["access_code"], "created_at": r["created_at"],
                "retrieve_time": r["retrieve_time"]
            })
        
        return json_response(data={
            "items": items, "total": total,
            "page": page, "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        })
    except Exception as e:
        logger.error(f"[transactions] {e}")
        return json_response(message=str(e), code=500)


@bp.route("/admin/device/status", methods=["GET"])
@require_auth
def admin_device_status():
    """设备在线状态"""
    try:
        db = get_db()
        devices = db.execute(
            "SELECT c.mainboard_device_id as device_id, c.cabinet_code as device_name, c.id as cabinet_id, c.last_heartbeat, "
            "c.cabinet_code "
            "FROM cabinets c WHERE c.mainboard_device_id IS NOT NULL "
            "ORDER BY c.last_heartbeat DESC"
        ).fetchall()
        
        items = []
        for d in devices:
            is_online = False
            if d["last_heartbeat"]:
                from datetime import datetime, timedelta
                try:
                    last = datetime.strptime(str(d["last_heartbeat"]), "%Y-%m-%d %H:%M:%S")
                    is_online = (datetime.now() - last) < timedelta(minutes=3)
                except:
                    pass
            items.append({
                "device_id": d["device_id"], "device_name": d["device_name"],
                "cabinet_id": d["cabinet_id"], "cabinet_code": d["cabinet_code"],
                "is_online": is_online
            })
        
        online_count = sum(1 for i in items if i["is_online"])
        return json_response(data={
            "devices": items,
            "total": len(items),
            "online": online_count,
            "offline": len(items) - online_count
        })
    except Exception as e:
        logger.error(f"[device_status] {e}")
        return json_response(message=str(e), code=500)

# ========== 主板管理 API ==========

@bp.route('/admin/mainboards', methods=['GET', 'POST'])
@require_auth
def admin_mainboards_list():
    """获取指定柜体的主板列表"""
    try:
        cabinet_id = request.args.get('cabinet_id')
        if not cabinet_id:
            return json_response(message='缺少cabinet_id', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM mainboards WHERE cabinet_id=%s ORDER BY board_index', (cabinet_id,))
        rows = c.fetchall()
        result = []
        for r in rows:
            result.append({
                'id': r['id'],
                'cabinet_id': r['cabinet_id'],
                'board_index': r['board_index'],
                'slot_count': r['slot_count'],
                'name': r['name'],
                'serial_port': r['serial_port'],
                'baud_rate': r['baud_rate'],
                'protocol': r['protocol'] or 'YBM'
            })
        conn.close()
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[mainboards_list] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/mainboards/save', methods=['POST'])
@require_auth
def admin_mainboards_save():
    """新增或编辑主板"""
    try:
        data = request.get_json()
        mid = data.get('id')
        cabinet_id = data.get('cabinet_id')
        board_index = data.get('board_index')
        slot_count = data.get('slot_count', 24)
        name = data.get('name', '')
        serial_port = data.get('serial_port', 'ttyS4')
        baud_rate = data.get('baud_rate', 9600)
        protocol = data.get('protocol', 'YBM')
        
        if protocol not in ('YBM', 'WT', 'QM'):
            return json_response(message='协议仅支持YBM、WT和QM', code=400)
        
        # 协议-串口匹配校验（弱提示，不阻塞保存）
        _PROTOCOL_SERIAL_MAP = {
            'YBM': ('ttyS4', 9600),
            'WT':  ('ttyS3', 115200),
            'QM':  ('ttyS2', 9600),
        }
        _expected_port, _expected_baud = _PROTOCOL_SERIAL_MAP.get(protocol, (None, None))
        _config_warning = ''
        if _expected_port and (serial_port != _expected_port or baud_rate != _expected_baud):
            _config_warning = '当前串口(%s/%s)与%s推荐配置(%s/%s)不一致，请确认硬件实际配置' % (serial_port, baud_rate, protocol, _expected_port, _expected_baud)
        if not cabinet_id or board_index is None:
            return json_response(message='缺少必要参数', code=400)
        
        conn = get_db()
        c = conn.cursor()
        
        # serial_type映射：根据串口名判断
        serial_type = 'real'
        
        if mid:
            # 更新
            c.execute('UPDATE mainboards SET board_index=%s, slot_count=%s, name=%s, serial_port=%s, baud_rate=%s, protocol=%s WHERE id=%s',
                      (board_index, slot_count, name, serial_port, baud_rate, protocol, mid))
            # 同步更新该主板下slot的board_no
            c.execute('UPDATE cabinet_slots SET board_no=%s WHERE mainboard_id=%s', (board_index, mid))
            conn.commit()
        else:
            # 新增
            c.execute('INSERT INTO mainboards (cabinet_id, board_index, slot_count, name, serial_port, baud_rate, protocol) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                      (cabinet_id, board_index, slot_count, name, serial_port, baud_rate, protocol))
            new_id = c.lastrowid
            conn.commit()
        
        # 自动推送配置到在线设备（不用注销重启）
        push_result = None
        try:
            cab = c.execute('SELECT mainboard_device_id FROM cabinets WHERE id=%s', (cabinet_id,)).fetchone()
            if cab and cab[0]:
                device_id = cab[0]
                from helpers import connected_devices
                ws = connected_devices.get(device_id)
                if ws:
                    import json as _json
                    config_msg = {
                        'type': 'update_config',
                        'serial_port': serial_port,
                        'baud_rate': baud_rate,
                        'serial_type': serial_type,
                        'protocol_type': protocol
                    }
                    ws.send(_json.dumps(config_msg))
                    push_result = f'已推送配置到设备{device_id}'
                    logger.info(f'[mainboards_save] {push_result}: {config_msg}')
                else:
                    push_result = f'设备{device_id}离线，配置已保存，下次上线自动生效'
                    logger.info(f'[mainboards_save] {push_result}')
        except Exception as pe:
            logger.warning(f'[mainboards_save] 推送配置失败(不影响保存): {pe}')
        
        conn.close()
        
        if mid:
            return json_response(message='更新成功', data={'push': push_result})
        else:
            return json_response(data={'id': new_id, 'push': push_result}, message='新增成功')
    except Exception as e:
        logger.error(f'[mainboards_save] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/mainboards/delete', methods=['POST'])
@require_auth
def admin_mainboards_delete():
    """删除主板（需先移走或删除关联slot）"""
    try:
        data = request.get_json()
        mid = data.get('id')
        if not mid:
            return json_response(message='缺少id', code=400)
        conn = get_db()
        c = conn.cursor()
        # 检查是否有slot关联
        c.execute('SELECT COUNT(*) as cnt FROM cabinet_slots WHERE mainboard_id=%s', (mid,))
        cnt = c.fetchone()['cnt']
        if cnt > 0:
            conn.close()
            return json_response(message=f'该主板下还有{cnt}个柜格，请先移除', code=400)
        c.execute('DELETE FROM mainboards WHERE id=%s', (mid,))
        conn.commit()
        conn.close()
        return json_response(message='删除成功')
    except Exception as e:
        logger.error(f'[mainboards_delete] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/mainboards/generate-slots', methods=['POST'])
@require_auth
def admin_mainboards_generate_slots():
    """根据主板配置自动生成slot（先清空该柜体旧slot再重建）"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        if not cabinet_id:
            return json_response(message='缺少cabinet_id', code=400)
        
        conn = get_db()
        c = conn.cursor()
        
        # 获取该柜体所有主板
        c.execute('SELECT * FROM mainboards WHERE cabinet_id=%s ORDER BY board_index', (cabinet_id,))
        boards = c.fetchall()
        if not boards:
            conn.close()
            return json_response(message='该柜体没有主板配置', code=400)
        
        # [Agent-modified 2026-07-04] 检查是否有在用订单(包含使用中2和已结算3)
        c.execute('SELECT COUNT(*) as cnt FROM orders WHERE cabinet_id=%s AND status IN (1,2,3)', (cabinet_id,))
        active_orders = c.fetchone()['cnt']
        if active_orders > 0:
            conn.close()
            return json_response(message=f'该柜体有{active_orders}个在用订单，请先处理', code=400)
        
        # [Agent-modified 2026-07-04] 根据cabinet的total_slots清理多余的主板记录
        c.execute('SELECT total_slots FROM cabinets WHERE id=%s', (cabinet_id,))
        cab = c.fetchone()
        total_slots = cab['total_slots'] if cab else sum(b['slot_count'] for b in boards)
        needed_boards = 0
        cumulative = 0
        for b in boards:
            cumulative += b['slot_count']
            needed_boards += 1
            if cumulative >= total_slots:
                break
        if needed_boards < len(boards):
            extra_ids = [b['id'] for b in boards[needed_boards:]]
            c.execute('DELETE FROM mainboards WHERE id = ANY(%s)', (extra_ids,))
            boards = boards[:needed_boards]
            logger.info(f'[generate_slots] 清理多余主板: cabinet_id={cabinet_id}, 删除{len(extra_ids)}条(id={extra_ids})')
        
        # 清空旧slot
        c.execute('DELETE FROM cabinet_slots WHERE cabinet_id=%s', (cabinet_id,))
        
        # 按主板生成slot
        slot_number = 1
        for board in boards:
            for lock_no in range(1, board['slot_count'] + 1):
                c.execute(
                    'INSERT INTO cabinet_slots (cabinet_id, slot_number, board_no, lock_no, mainboard_id, status) VALUES (%s,%s,%s,%s,%s,1)',
                    (cabinet_id, slot_number, board['board_index'], lock_no, board['id'])
                )
                slot_number += 1
        
        conn.commit()
        conn.close()
        return json_response(message=f'已生成{slot_number - 1}个柜格')
    except Exception as e:
        logger.error(f'[mainboards_generate_slots] {e}')
        return json_response(message=str(e), code=500)

# ============ 微信投诉通知API (骨架) ============
# 注意：此API需要用户在微信支付后台配置投诉通知URL才能实际接收投诉
# 微信支付投诉通知URL格式: https://your-domain.com/api/admin_v2/wechat-complaint/notify
# 需要配置微信支付API证书和密钥才能解密投诉通知

@bp.route('/wechat-complaint/notify', methods=['POST'])
def wechat_complaint_notify():
    """微信支付投诉通知接收 - 自动解密+回复"""
    import hashlib, base64
    try:
        data = request.get_json() or {}
        logger.info('[wechat_complaint_notify] 收到通知: %s', json.dumps(data, ensure_ascii=False)[:500])
        
        # 解密通知内容 (AEAD_AES_256_GCM)
        resource = data.get('resource', {})
        ciphertext_b64 = resource.get('ciphertext', '')
        nonce = resource.get('nonce', '')
        associated_data = resource.get('associated_data', '')
        api_v3_key = WX_API_V3_KEY.encode('utf-8')
        
        if not ciphertext_b64:
            logger.warning('[wechat_complaint_notify] 无ciphertext, 跳过解密')
            return jsonify({'code': 'SUCCESS', 'message': 'ok'})
        
        # AES-256-GCM解密
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        ciphertext = base64.b64decode(ciphertext_b64)
        aesgcm = AESGCM(api_v3_key)
        plaintext = aesgcm.decrypt(nonce.encode('utf-8'), ciphertext, associated_data.encode('utf-8'))
        complaint_data = json.loads(plaintext.decode('utf-8'))
        logger.info('[wechat_complaint_notify] 解密内容: %s', json.dumps(complaint_data, ensure_ascii=False)[:1000])
        
        # 提取投诉信息
        complaint_id = complaint_data.get('complaint_id', '')
        complaint_time = complaint_data.get('complaint_time', '')
        complaint_detail = complaint_data.get('complaint_detail', '')
        complaint_order_info = complaint_data.get('complaint_order_info', [])
        payer_phone = complaint_data.get('payer_phone', '')
        complaint_state = complaint_data.get('complaint_state', '')
        complained_mchid = complaint_data.get('complainted_mchid', '')
        
        # 提取订单号（优先从顶层取，兼容 complaint_order_info）
        order_no = complaint_data.get('out_trade_no', '') or complaint_data.get('transaction_id', '')
        if not order_no and complaint_order_info:
            order_no = complaint_order_info[0].get('out_trade_no', '') or complaint_order_info[0].get('transaction_id', '')
        
        # 存入complaints表
        conn = get_db()
        c = conn.cursor()
        # 检查是否已存在
        existing = c.execute('SELECT id FROM complaints WHERE wx_complaint_id=%s', (complaint_id,)).fetchone()
        if not existing:
            c.execute(
                'INSERT INTO complaints (user_phone, type, content, order_no, wx_complaint_id, complaint_type, status, mch_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                (payer_phone, 'wechat', complaint_detail or complaint_time or '微信投诉', order_no, complaint_id, 'wechat', 0, complained_mchid)
            )
            new_id = c.fetchone()[0]
            # 通过order_no或transaction_id关联本地订单
            order_row = None
            if order_no:
                c.execute('SELECT id FROM orders WHERE order_no=%s LIMIT 1', (order_no,))
                order_row = c.fetchone()
            if not order_row and transaction_id:
                c.execute('SELECT id FROM orders WHERE transaction_id=%s LIMIT 1', (transaction_id,))
                order_row = c.fetchone()
            if order_row:
                c.execute('UPDATE complaints SET order_id=%s WHERE id=%s', (order_row[0], new_id))
            conn.commit()
            if payer_phone:
                from helpers import mark_user_withdraw as _muw
                try: _muw(phone=payer_phone)
                except: pass
            # 投诉自动加入提现白名单
            if payer_phone:
                from helpers import add_whitelist_by_phone
                add_whitelist_by_phone(payer_phone, 'complaint', -1)
            logger.info('[wechat_complaint_notify] 已保存投诉: complaint_id=%s', complaint_id)
        else:
            logger.info('[wechat_complaint_notify] 投诉已存在: complaint_id=%s', complaint_id)
        conn.close()
        
        # 从 complaint_order_info 提取真正的支付 transaction_id
        transaction_id = ''
        if complaint_order_info:
            transaction_id = complaint_order_info[0].get('transaction_id', '')
        
        # 如果回调没带订单号，主动查询微信API
        if not order_no and complaint_id:
            _qd = _query_wechat_complaint(complaint_id, mch_id=complained_mchid)
            if _qd:
                order_no = _qd.get("out_trade_no", "") or _qd.get("transaction_id", "")
                complained_mchid = _qd.get("complainted_mchid", "") or complained_mchid
                if order_no:
                    try:
                        _cc2 = get_db().cursor()
                        _cc2.execute("UPDATE complaints SET order_no=%s, mch_id=%s, user_phone=COALESCE(user_phone,%s) WHERE wx_complaint_id=%s",
                                     (order_no, complained_mchid, _qd.get("payer_phone", ""), complaint_id))
                        _cc2.connection.commit()
                        _cc2.close()
                    except:
                        pass
        # 自动处理投诉：退款 + 回复 + 结案
        if order_no:
            _auto_refund_complaint_order(order_no, transaction_id, complaint_id)
            # 查找正确的商户凭证
            _mch_id = complained_mchid
            if not _mch_id:
                # 从订单关联的商户获取
                try:
                    _cc = get_db().cursor()
                    _cc.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1")
                    _cr = _cc.fetchone()
                    if _cr:
                        _mch_id = _cr['mch_id']
                    _cc.connection.close()
                except:
                    pass
            if not _mch_id:
                logger.error('[投诉处理] 无可用商户号')
                return
            _cert_serial = WX_CERT_SERIAL_NO
            _key_path = WX_KEY_PATH
            if complained_mchid:
                try:
                    conn5 = get_db()
                    c5 = conn5.cursor()
                    c5.execute("SELECT cert_serial_no, cert_name FROM payment_channels WHERE mch_id=%s", (complainted_mchid,))
                    pc5 = c5.fetchone()
                    if pc5:
                        _cert_serial = pc5[0]
                        _key_path = f'/home/ubuntu/smart-locker/cert/{pc5[1]}_key.pem'
                    c5.close()
                    conn5.close()
                except:
                    pass
            _auto_reply_complaint(complaint_id, order_no, transaction_id, mch_id=_mch_id, cert_serial=_cert_serial, private_key_path=_key_path)
            _auto_complete_complaint(complaint_id, _mch_id, _cert_serial, _key_path)
        
        return jsonify({'code': 'SUCCESS', 'message': 'ok'})
    except Exception as e:
        logger.error('[wechat_complaint_notify] 错误: %s', e, exc_info=True)
        return jsonify({'code': 'FAIL', 'message': str(e)})



def _auto_refund_complaint_order(order_no, transaction_id="", complaint_id=""):
    """投诉自动原路退款：找到对应订单，调用微信退款API退回押金"""
    try:
        from helpers import do_real_refund
        conn = get_db()
        c = conn.cursor()
        order = None
        if order_no:
            c.execute('SELECT id, order_no, transaction_id, deposit_amount, refund_amount, refund_mark, refund_status, status, slot_id, payment_channel_id, user_phone FROM orders WHERE order_no=%s LIMIT 1', (order_no,))
            order = c.fetchone()
        if not order and transaction_id:
            c.execute('SELECT id, order_no, transaction_id, deposit_amount, refund_amount, refund_mark, refund_status, status, slot_id, payment_channel_id, user_phone FROM orders WHERE transaction_id=%s LIMIT 1', (transaction_id,))
            order = c.fetchone()
        if not order:
            conn.close()
            logger.warning('[auto_refund_complaint] 未找到对应订单 order_no=%s transaction_id=%s', order_no, transaction_id)
            return False, '订单不存在'
        
        order = dict(order)
        order_id = order['id']
        deposit = float(order.get('deposit_amount') or 0)
        already_refunded = float(order.get('refund_amount') or 0)
        refund_mark = order.get('refund_mark') or 0
        status = order.get('status')
        
        # 已通过微信原路退款的不重复处理（refund_status为success/refunded表示已微信退款）
        refund_status = order.get('refund_status') or ''
        if refund_status in ('success', 'refunded') and deposit > 0:
            conn.close()
            logger.info('[auto_refund_complaint] 订单已微信退款 order_id=%s refund_status=%s', order_id, refund_status)
            return True, '已退款'
        
        if deposit <= 0:
            conn.close()
            logger.info('[auto_refund_complaint] 订单无押金 order_id=%s', order_id)
            return True, '无押金'
        
        # 投诉退款：退全额押金（之前退到余额的不算微信退款）
        refund_amount = deposit
        if refund_amount <= 0:
            conn.close()
            return True, '无可退金额'
        
        # 调用微信退款
        success, refund_id, msg = do_real_refund(
            order_id=order_id,
            order_no=order.get('order_no', ''),
            amount=refund_amount,
            payment_channel_id=order.get('payment_channel_id')
        )
        
        if success:
            from datetime import datetime as dt_mod
            now = dt_mod.now().strftime('%Y-%m-%d %H:%M:%S')
            # 更新订单状态为已退款
            c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                      (deposit, order_id))
            # 释放柜格（如果还在使用中）
            if status == 2 and order.get('slot_id'):
                c.execute('UPDATE cabinet_slots SET status=1 WHERE id=%s', (order['slot_id'],))
            # 记录退款流水
            c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, CURRENT_TIMESTAMP)',
                      (order_id, refund_amount, order.get('transaction_id', ''), refund_id or ''))
            # 更新投诉记录关联
            if complaint_id:
                c.execute('UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE wx_complaint_id=%s',
                          ('已自动原路退款', complaint_id))
            conn.commit()
            conn.close()
            logger.info('[auto_refund_complaint] 退款成功 order_id=%s amount=%.2f refund_id=%s', order_id, refund_amount, refund_id)
            return True, refund_id
        else:
            conn.close()
            logger.error('[auto_refund_complaint] 退款失败 order_id=%s msg=%s', order_id, msg)
            # 永久性错误：订单在微信不存在，不再重试
            if '记录不存在' in msg or 'ORDERNOTEXIST' in msg:
                try:
                    c2 = get_db()
                    cur2 = c2.cursor()
                    cur2.execute('UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE wx_complaint_id=%s',
                              ('订单在微信不存在，自动退款失败', complaint_id))
                    c2.commit()
                    c2.close()
                except Exception as _e3:
                    logger.warning('[auto_refund_complaint] 更新投诉状态失败: %s', _e3)
                return True, '订单不存在(已标记)'
            return False, msg
    except Exception as e:
        logger.error('[auto_refund_complaint] 异常: %s', e, exc_info=True)
        return False, str(e)


def _auto_reply_complaint(complaint_id, order_no="", transaction_id="", mch_id="", cert_serial="", private_key_path=""):
    """自动回复微信投诉"""
    import time, requests, base64
    try:
        # 根据订单支付渠道选择对应商户证书
        if not mch_id:
            try:
                _ac = get_db().cursor()
                _ac.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1")
                _ar = _ac.fetchone()
                if _ar:
                    mch_id = _ar['mch_id']
                _ac.connection.close()
            except:
                pass
        cert_serial = cert_serial or WX_CERT_SERIAL_NO
        private_key_path = private_key_path or WX_KEY_PATH
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            order = None
            if order_no:
                c.execute('SELECT id, payment_channel_id FROM orders WHERE order_no=%s LIMIT 1', (order_no,))
                order = c.fetchone()
            if not order and transaction_id:
                c.execute('SELECT id, payment_channel_id FROM orders WHERE transaction_id=%s LIMIT 1', (transaction_id,))
                order = c.fetchone()
            if order and order.get('payment_channel_id'):
                c.execute('SELECT mch_id, cert_serial_no FROM payment_channels WHERE id=%s', (order['payment_channel_id'],))
                channel = c.fetchone()
                if channel and channel.get('mch_id'):
                    mch_id = channel['mch_id']
                    cert_serial = channel.get('cert_serial_no') or cert_serial
                    private_key_path = os.path.join(os.path.dirname(WX_KEY_PATH), mch_id + '_key.pem')
                    logger.info('[auto_reply] 使用商户 %s 的证书回复投诉 %s', mch_id, complaint_id)
            conn.close()
        except Exception as lookup_e:
            logger.warning('[auto_reply] 查找支付渠道失败，使用默认商户: %s', lookup_e)

        with open(private_key_path, 'r') as f:
            private_key = f.read()

        # 检查投诉是否已自动退款
        _refunded = False
        try:
            _conn = get_db()
            _c = _conn.cursor()
            _c.execute('SELECT status FROM complaints WHERE wx_complaint_id=%s', (complaint_id,))
            _row = _c.fetchone()
            if _row and _row.get('status') == 2:
                _refunded = True
            _conn.close()
        except:
            pass
        if _refunded:
            reply_content = '您好，您的订单已为您办理全额退款，款项将原路返回至您的微信零钱，请注意查收。如有疑问请联系客服，感谢您的理解与支持！'
        else:
            reply_content = '您好，我们已收到您的反馈，正在尽快为您处理。如有疑问请联系客服，感谢您的理解与支持！'

        # 构造签名
        timestamp = str(int(time.time()))
        nonce_str = os.urandom(16).hex()

        # POST body - 按微信V3投诉回复API规范
        # 不传 response_images（空数组会导致PARAM_ERROR）
        url_path = '/v3/merchant-service/complaints-v2/' + complaint_id + '/response'
        body = json.dumps({
            'complainted_mchid': mch_id,
            'response_content': reply_content
        }, ensure_ascii=False, separators=(',', ':'))

        sign_str = 'POST\n' + url_path + '\n' + timestamp + '\n' + nonce_str + '\n' + body + '\n'

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        private_key_obj = serialization.load_pem_private_key(private_key.encode(), password=None)
        signature = private_key_obj.sign(
            sign_str.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        sign_b64 = base64.b64encode(signature).decode('utf-8')

        authorization = 'WECHATPAY2-SHA256-RSA2048 mchid="' + mch_id + '",nonce_str="' + nonce_str + '",timestamp="' + timestamp + '",serial_no="' + cert_serial + '",signature="' + sign_b64 + '"'

        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': authorization
        }

        # V3 API使用Header签名认证，不需要客户端证书
        resp = requests.post(
            'https://api.mch.weixin.qq.com' + url_path,
            data=body.encode('utf-8'),
            headers=headers,
            timeout=10
        )
        logger.info('[auto_reply] complaint_id=%s status=%s resp=%s', complaint_id, resp.status_code, resp.text[:500] if resp.text else '(empty)')

        if resp.status_code in (200, 204):
            conn = get_db()
            c = conn.cursor()
            c.execute('UPDATE complaints SET reply=%s, status=1, reply_time=CURRENT_TIMESTAMP WHERE wx_complaint_id=%s',
                      (reply_content, complaint_id))
            conn.commit()
            conn.close()
            logger.info('[auto_reply] 投诉回复成功并已更新数据库 complaint_id=%s', complaint_id)
            
            # 投诉完成后调用complete API标记已处理
            try:
                url_path2 = '/v3/merchant-service/complaints-v2/' + complaint_id + '/complete'
                body2 = json.dumps({'complainted_mchid': mch_id}, ensure_ascii=False, separators=(',', ':'))
                timestamp2 = str(int(time.time()))
                nonce_str2 = os.urandom(16).hex()
                sign_str2 = 'POST\n' + url_path2 + '\n' + timestamp2 + '\n' + nonce_str2 + '\n' + body2 + '\n'
                signature2 = private_key_obj.sign(sign_str2.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
                sign_b642 = base64.b64encode(signature2).decode('utf-8')
                authorization2 = 'WECHATPAY2-SHA256-RSA2048 mchid="' + mch_id + '",nonce_str="' + nonce_str2 + '",timestamp="' + timestamp2 + '",serial_no="' + cert_serial + '",signature="' + sign_b642 + '"'
                headers2 = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': authorization2}
                resp2 = requests.post('https://api.mch.weixin.qq.com' + url_path2, data=body2.encode('utf-8'), headers=headers2, timeout=10)
                if resp2.status_code in (200, 204):
                    logger.info('[auto_reply] 投诉已完成处理 complaint_id=%s', complaint_id)
                else:
                    logger.warning('[auto_reply] 投诉完成处理失败 complaint_id=%s status=%s resp=%s', complaint_id, resp2.status_code, resp2.text[:300])
            except Exception as complete_e:
                logger.warning('[auto_reply] 投诉完成处理异常 complaint_id=%s err=%s', complaint_id, complete_e)
        else:
            logger.error('[auto_reply] 投诉回复失败 complaint_id=%s http_status=%s resp=%s',
                        complaint_id, resp.status_code, resp.text[:500] if resp.text else '(empty)')
            # 权限不足等永久性错误，不再重试
            if 'NO_AUTH' in (resp.text or '') or resp.status_code == 403:
                try:
                    c_noauth = get_db()
                    cur_noauth = c_noauth.cursor()
                    cur_noauth.execute('UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE wx_complaint_id=%s',
                                     ('无回复权限(NO_AUTH)，自动回复失败', complaint_id))
                    c_noauth.commit()
                    c_noauth.close()
                    logger.warning('[auto_reply] 已标记无权限投诉 complaint_id=%s', complaint_id)
                except Exception as _e4:
                    logger.warning('[auto_reply] 更新无权限投诉状态失败: %s', _e4)

    except Exception as e:
        logger.error('[auto_reply] 投诉自动回复失败: %s', e, exc_info=True)



def _auto_complete_complaint(complaint_id, mch_id, cert_serial, private_key_path):
    """尝试调用微信V3 complete API完结投诉"""
    import os, json, time, base64, requests
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        with open(private_key_path, 'r') as f:
            private_key = f.read()
        key_obj = serialization.load_pem_private_key(private_key.encode(), password=None)
        url_path = '/v3/merchant-service/complaints-v2/' + complaint_id + '/complete'
        body = json.dumps({'complainted_mchid': mch_id}, ensure_ascii=False, separators=(',', ':'))
        timestamp = str(int(time.time()))
        nonce_str = os.urandom(16).hex()
        sign_str = 'POST\n' + url_path + '\n' + timestamp + '\n' + nonce_str + '\n' + body + '\n'
        signature = key_obj.sign(sign_str.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
        sign_b64 = base64.b64encode(signature).decode('utf-8')
        auth = 'WECHATPAY2-SHA256-RSA2048 mchid="' + mch_id + '",nonce_str="' + nonce_str + '",timestamp="' + timestamp + '",serial_no="' + cert_serial + '",signature="' + sign_b64 + '"'
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': auth}
        resp = requests.post('https://api.mch.weixin.qq.com' + url_path, data=body.encode('utf-8'), headers=headers, timeout=10)
        if resp.status_code in (200, 204):
            logger.info('[auto_complete] 投诉已完成 complaint_id=%s', complaint_id)
            return True
        else:
            logger.warning('[auto_complete] 完成失败 complaint_id=%s status=%s resp=%s', complaint_id, resp.status_code, resp.text[:300])
            return False
    except Exception as e:
        logger.error('[auto_complete] 异常 complaint_id=%s err=%s', complaint_id, e)
        return False



def admin_query_door_status():
    import json as _j
    from helpers import connected_devices as _cd
    data = request.get_json()
    device_id = data.get('device_id', '')
    board_no = data.get('board_no', 1)
    lock_no = data.get('lock_no', 1)
    protocol = data.get('protocol', 'YBM')
    if not device_id:
        return json_response(message='device_id required', code=400)
    request_id = str(uuid.uuid4())
    with _door_status_lock:
        _door_status_results[request_id] = {'result': None, 'event': threading.Event()}
    if device_id in _cd:
        try:
            cmd = {'type': 'query_door_status', 'request_id': request_id, 'board_no': board_no, 'lock_no': lock_no, 'protocol': protocol}
            _cd[device_id].send(_j.dumps(cmd))
            logger.info('[door_status] sent: device=%s req=%s', device_id, request_id)
        except Exception as e:
            with _door_status_lock: _door_status_results.pop(request_id, None)
            return json_response(message='send failed: ' + str(e), code=500)
    else:
        with _door_status_lock: _door_status_results.pop(request_id, None)
        return json_response(message='device offline', code=400)
    with _door_status_lock:
        evt = _door_status_results.get(request_id, {}).get('event')
    if evt:
        ok = evt.wait(timeout=10)
        with _door_status_lock:
            result = _door_status_results.pop(request_id, {}).get('result')
        if result:
            return json_response(data=result)
        return json_response(message='query timeout', code=504)
    return json_response(message='query failed', code=500)
@bp.route('/admin/device/slot-status', methods=['POST'])
def admin_slot_status():
    data = request.get_json(silent=True) or {}
    slot_id = data.get('slot_id')
    if not slot_id:
        return json_response(code=400, message='缺少参数')
    db = get_db()
    try:
        slot = db.execute('SELECT cs.*, c.mainboard_device_id FROM cabinet_slots cs LEFT JOIN cabinets c ON cs.cabinet_id = c.id WHERE cs.id=?', (slot_id,)).fetchone()
        if not slot:
            return json_response(code=404, message='柜门不存在')
        status_map = {1: '空闲', 2: '占用', 3: '故障', 4: '锁定'}
        status_text = status_map.get(slot['status'], '未知')
        from helpers import connected_devices
        dev_id = slot['mainboard_device_id']
        online = dev_id and dev_id in connected_devices
        data = {
            'status': status_text,
            'slot_label': slot.get('slot_label') or slot.get('slot_number', ''),
            'device_online': online,
            'detail': status_text + ('(设备在线)' if online else '(设备离线)')
        }
        return json_response(data=data)
    except Exception as e:
        logger.error(f'查询柜门状态失败: {e}')
        return json_response(code=500, message='查询失败')
@bp.route('/admin/device/push-config', methods=['POST'])
def admin_device_push_config():
    """远程推送串口配置到设备APK"""
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')
    if not device_id:
        return json_response(code=400, message='缺少device_id')
    
    from helpers import connected_devices
    ws = connected_devices.get(device_id)
    if not ws:
        return json_response(code=400, message=f'设备{device_id}不在线')
    
    import json as _json
    config_msg = {"type": "update_config"}
    if 'serial_port' in data:
        config_msg["serial_port"] = data["serial_port"]
    if 'baud_rate' in data:
        config_msg["baud_rate"] = data["baud_rate"]
    if 'protocol_type' in data:
        config_msg["protocol_type"] = data["protocol_type"]
    
    try:
        ws.send(_json.dumps(config_msg))
        logger.info(f"[PUSH_CONFIG] sent to {device_id}: {config_msg}")
        return json_response(data={"sent": config_msg})
    except Exception as e:
        logger.error(f"[PUSH_CONFIG] failed: {e}")
        return json_response(code=500, message=str(e))

@bp.route('/admin/device/push-update', methods=['POST'])
def admin_device_push_update():
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')
    if not device_id:
        return json_response(code=400, message='缺少device_id')
    from config import LATEST_VERSION_CODE, LATEST_VERSION_NAME
    import json as _json
    # 获取APK的MD5
    file_md5 = ''
    try:
        conn_md5 = get_db()
        c_md5 = conn_md5.cursor()
        c_md5.execute('SELECT file_md5 FROM apk_version ORDER BY version_code DESC LIMIT 1')
        row_md5 = c_md5.fetchone()
        if row_md5:
            file_md5 = row_md5.get('file_md5', '') or ''
        conn_md5.close()
    except: pass

    msg = {
        'type': 'force_update',
        'download_url': 'https://locker.cqdyxl.com/static/locker.apk',
        'version_name': LATEST_VERSION_NAME,
        'version_code': LATEST_VERSION_CODE,
        'force': True,
        'file_md5': file_md5
    }
    cmd_json = _json.dumps(msg)

    # 写入pending_lock_cmds，APK通过HTTP轮询获取
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute('INSERT INTO pending_lock_cmds (device_id, command, delivered) VALUES (%s, %s, 0)',
                    (device_id, cmd_json))
        db.commit()
        db.close()
        logger.info(f'[PUSH_UPDATE] cmd inserted for {device_id}: {msg}')
    except Exception as e:
        logger.error(f'[PUSH_UPDATE] DB insert failed: {e}')
        return json_response(code=500, message=f'写入命令失败: {e}')

    # 同时尝试WS直发（设备在线时立即送达）
    ws_sent = False
    try:
        from helpers import connected_devices
        ws = connected_devices.get(device_id)
        if ws:
            ws.send(cmd_json)
            ws_sent = True
            logger.info(f'[PUSH_UPDATE] WS sent to {device_id}')
    except Exception as e:
        logger.warning(f'[PUSH_UPDATE] WS send failed: {e}')

    return json_response(data={'sent': msg, 'ws_sent': ws_sent})


@bp.route('/admin/device/update-result', methods=['POST'])
def admin_device_update_result():
    """接收APK安装结果回报"""
    try:
        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '')
        success = data.get('success', False)
        version_name = data.get('version_name', '')
        version_code = data.get('version_code', 0)
        error_msg = data.get('error_msg', '')
        
        status = 'success' if success else 'failed'
        logger.info(f'[UPDATE_RESULT] device={device_id} status={status} version={version_name}({version_code}) error={error_msg}')
        
        # 记录到数据库
        if device_id:
            conn = get_db()
            c = conn.cursor()
            c.execute("""INSERT INTO device_update_logs (device_id, success, version_name, version_code, error_msg, created_at)
                         VALUES (%s, %s, %s, %s, %s, NOW())""",
                      (device_id, 1 if success else 0, version_name, version_code, error_msg))
            conn.commit()
            conn.close()
        
        return json_response(data={'received': True})
    except Exception as e:
        logger.error(f'[UPDATE_RESULT] error: {e}')
        return json_response(code=500, message=str(e))

# ============ 投诉自动处理调度器（替代Timer） ============
def _query_wechat_complaint(complaint_id, mch_id=None, cert_serial=None, key_path=None):
    """从微信API查询投诉详情，获取订单号等信息"""
    try:
        import requests as _req, base64 as _b64, uuid as _uid, json as _js
        from cryptography.hazmat.primitives import hashes as _hs
        from cryptography.hazmat.primitives.asymmetric import padding as _pd
        from cryptography.hazmat.primitives.serialization import load_pem_private_key as _load_key
        if not mch_id:
            _cc = get_db().cursor()
            _cc.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1")
            _cr = _cc.fetchone()
            mch_id = _cr["mch_id"] if _cr else WX_MCH_ID
            _cc.close()
        cert_serial = cert_serial or WX_CERT_SERIAL_NO
        key_path = key_path or WX_KEY_PATH
        with open(key_path, "rb") as _f:
            _key_obj = _load_key(_f.read(), password=None)
        _ts = str(int(__import__("time").time()))
        _nonce = _uid.uuid4().hex
        _url = "/v3/merchant-service/complaints-v2/" + complaint_id
        _sstr = "GET\n" + _url + "\n" + _ts + "\n" + _nonce + "\n\n"
        _sig = _b64.b64encode(_key_obj.sign(_sstr.encode("utf-8"), _pd.PKCS1v15(), _hs.SHA256())).decode("utf-8")
        _auth = "WECHATPAY2-SHA256-RSA2048 mchid=\"" + mch_id + "\",nonce_str=\"" + _nonce + "\",timestamp=\"" + _ts + "\",serial_no=\"" + cert_serial + "\",signature=\"" + _sig + "\""
        _resp = _req.get("https://api.mch.weixin.qq.com" + _url, headers={"Authorization": _auth, "Accept": "application/json"}, timeout=10)
        if _resp.status_code == 200:
            return _resp.json()
        logger.warning("[query_complaint] 查询失败 complaint_id=%s status=%s", complaint_id, _resp.status_code)
    except Exception as _e:
        logger.error("[query_complaint] 异常 complaint_id=%s err=%s", complaint_id, str(_e))
    return None

def _complaint_scheduler():
    """后台线程：每30秒扫描未处理的微信投诉（status=0），进行退款+回复"""
    import time
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT * FROM complaints WHERE status IN ('0','1') AND type='wechat' AND created_at < NOW() - INTERVAL '2 minutes' ORDER By created_at LIMIT 10")
            rows = c.fetchall()
            conn.close()
            conn = None
            for row in rows:
                comp = dict(row)
                cid = comp.get("id", 0)
                wxid = comp.get("wx_complaint_id", "")
                ono = comp.get("order_no", "")
                cstatus = comp.get("status", "0")
                # 如果投诉无订单号，主动查询微信API
                if not ono and wxid:
                    _qd = _query_wechat_complaint(wxid)
                    if _qd:
                        ono = _qd.get("out_trade_no", "") or _qd.get("transaction_id", "")
                        cmch = _qd.get("complainted_mchid", "")
                        cphone = _qd.get("payer_phone", "")
                        if ono or cmch or cphone:
                            try:
                                _cc2 = get_db().cursor()
                                _cc2.execute("UPDATE complaints SET order_no=COALESCE(order_no,%s), mch_id=COALESCE(mch_id,%s), user_phone=COALESCE(user_phone,%s) WHERE id=%s",
                                             (ono if ono else None, cmch if cmch else None, cphone if cphone else None, cid))
                                _cc2.connection.commit()
                                _cc2.close()
                            except:
                                pass
                logger.info("[complaint_scheduler] 处理投诉 id=%s wx_id=%s order_no=%s status=%s", cid, wxid, ono, cstatus)
                if cstatus == "0":
                    refund_ok, refund_msg = _auto_refund_complaint_order(ono, transaction_id="", complaint_id=wxid)
                    if not refund_ok:
                        logger.warning("[complaint_scheduler] 退款失败 id=%s msg=%s", cid, refund_msg)
                    cmch = comp.get('mch_id', '') or ''
                    ccert = WX_CERT_SERIAL_NO
                    ckey = WX_KEY_PATH
                    _auto_reply_complaint(wxid, order_no=ono, transaction_id="", mch_id=cmch, cert_serial=ccert, private_key_path=ckey)
                elif cstatus == "1":
                    # Use complaint's mch_id to get correct merchant cert
                    cmch = comp.get('mch_id', '') or ''
                    ccert = WX_CERT_SERIAL_NO
                    ckey = WX_KEY_PATH
                    if cmch:
                        conn2 = None
                        try:
                            conn2 = get_db()
                            c3 = conn2.cursor()
                            c3.execute('SELECT cert_serial_no, cert_name FROM payment_channels WHERE mch_id=%s', (cmch,))
                            pc = c3.fetchone()
                            if pc:
                                ccert = pc[0]
                                ckey = f'/home/ubuntu/smart-locker/cert/{pc[1]}_key.pem'
                            c3.close()
                            conn2.close()
                            conn2 = None
                        except:
                            pass
                        finally:
                            if conn2:
                                try:
                                    conn2.close()
                                except:
                                    pass
                    _cmch = cmch
                    if not _cmch:
                        _fc_conn = None
                        try:
                            _fc_conn = get_db()
                            _fc = _fc_conn.cursor()
                            _fc.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id ASC LIMIT 1")
                            _fr = _fc.fetchone()
                            if _fr:
                                _cmch = _fr['mch_id']
                            _fc.close()
                            _fc_conn.close()
                            _fc_conn = None
                        except:
                            pass
                        finally:
                            if _fc_conn:
                                try:
                                    _fc_conn.close()
                                except:
                                    pass
                    if _cmch:
                        _auto_complete_complaint(wxid, _cmch, ccert, ckey)
                    else:
                        logger.error('[投诉自动处理] 无可用商户号')
                    conn2 = None
                    try:
                        conn2 = get_db()
                        c2 = conn2.cursor()
                        c2.execute("UPDATE complaints SET status=2 WHERE id=%s AND status='1'", (cid,))
                        conn2.commit()
                        conn2.close()
                        conn2 = None
                    except Exception as e:
                        logger.error('[投诉自动处理] 更新投诉状态失败: %s', e)
                    finally:
                        if conn2:
                            try:
                                conn2.close()
                            except:
                                pass

            # non-wechat auto complaints (with refund if has order)
            conn3 = None
            conn4 = None
            try:
                conn3 = get_db()
                c3 = conn3.cursor()
                c3.execute("SELECT * FROM complaints WHERE status=0 AND (type!='wechat' OR type IS NULL) AND created_at < NOW() - INTERVAL '2 minutes' ORDER BY id LIMIT 10")
                rows2 = c3.fetchall()
                conn3.close()
                conn3 = None
                for row2 in rows2:
                    comp2 = dict(row2)
                    cid2 = comp2.get("id", 0)
                    ono2 = comp2.get("order_no", "")
                    phone2 = comp2.get("user_phone", "")
                    logger.info("[complaint_scheduler] non-wechat complaint id=%s phone=%s order=%s", cid2, phone2, ono2)
                    if ono2:
                        refund_ok2, refund_msg2 = _auto_refund_complaint_order(ono2, transaction_id="", complaint_id="")
                        if refund_ok2:
                            logger.info("[complaint_scheduler] non-wechat refund ok id=%s order=%s", cid2, ono2)
                        else:
                            logger.warning("[complaint_scheduler] non-wechat refund fail id=%s order=%s msg=%s", cid2, ono2, refund_msg2)
                    reply_text = '您好，您的投诉已收到，我们会尽快处理。如有紧急情况请联系客服，感谢您的理解与支持！'
                    try:
                        conn4 = get_db()
                        c4 = conn4.cursor()
                        c4.execute("UPDATE complaints SET reply=%s, status=2, reply_time=CURRENT_TIMESTAMP WHERE id=%s", (reply_text, cid2))
                        conn4.commit()
                        conn4.close()
                        conn4 = None
                        logger.info("[complaint_scheduler] non-wechat complaint done id=%s", cid2)
                    except Exception as e4:
                        logger.error("[complaint_scheduler] non-wechat update error: %s", e4)
                    finally:
                        if conn4:
                            try:
                                conn4.close()
                            except:
                                pass
            except Exception as e2:
                logger.error("[complaint_scheduler] non-wechat error: %s", e2, exc_info=True)
            finally:
                if conn3:
                    try:
                        conn3.close()
                    except:
                        pass
                if conn4:
                    try:
                        conn4.close()
                    except:
                        pass

        except Exception as e:
            logger.error("[complaint_scheduler] 异常: %s", e, exc_info=True)
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass
        time.sleep(30)

# 启动调度器
_scheduler_thread = threading.Thread(target=_complaint_scheduler, daemon=True)
_scheduler_thread.start()
logger.info("[启动] 投诉自动处理调度器已启动")

@bp.route("/admin/dashboard", methods=["GET"])
def admin_v2_dashboard():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as total FROM cabinets")
        total_cabinets = c.fetchone()[0]
        conn.close()
        return json_response(data={"total_cabinets": total_cabinets})
    except Exception as e:
        logger.error(f"[dashboard] {e}")
        return json_response(message=str(e), code=500)

@bp.route("/admin/devices", methods=["GET"])
def admin_v2_devices():
    try:
        page = request.args.get("page", 1, type=int)
        limit = request.args.get("limit", 10, type=int)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as total FROM cabinets")
        total = c.fetchone()[0]
        offset = (page - 1) * limit
        c.execute("SELECT c.*, l.name as location_name FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id ORDER BY c.id LIMIT %s OFFSET %s", (limit, offset))
        list_data = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={"list": list_data, "total": total, "page": page, "limit": limit})
    except Exception as e:
        logger.error(f"[devices] {e}")
        return json_response(message=str(e), code=500)
