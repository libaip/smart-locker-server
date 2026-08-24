import psycopg2
from psycopg2.extras import RealDictCursor
import json
import os
"""
管理后台V2 API - 补全admin_v2前端所需的所有接口
包括：仪表盘统计、设备列表、订单管理、会员管理、提现管理等
"""
import logging
import time
from datetime import datetime, timedelta
from flask import Blueprint, request, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db
import threading, uuid
from helpers import json_response, manage_user_tokens, require_auth, logger, connected_devices, supersede_force_update_cmds, \
    upsert_user_balance_row, find_user_balance_row
from config import WX_API_V3_KEY, WX_MCH_ID, WX_CERT_SERIAL_NO, WX_KEY_PATH, WX_CERT_PATH, WX_APP_ID, WX_APP_SECRET, WX_MP_APP_ID, WX_MP_APP_SECRET

# ===== 微信投诉自动处理话术（2026-08-19 定版，全部投诉统一话术）=====
WECHAT_FIRST_REPLY = '您好，您的投诉已收到，我们正在为您核实处理。您的预付款将在30分钟内原路退回，请注意查收。如有疑问请拨打客服电话4006981080。'
WECHAT_ARRIVAL_NOTICE = '您好，您的退款¥{amount}已原路退回，请注意查收。如未退款请联系人工客服帮您处理，客服电话4006981080。'
WECHAT_MANUAL_REPLY = '您好，您的退款遇到异常，请联系人工客服帮您处理，客服电话4006981080。'
WECHAT_NO_REFUND = '您好，经核实您的订单已退款或无需退款，如有疑问请拨打客服电话4006981080。'
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



bp = Blueprint('admin_v2', __name__)

_door_status_results = {}
_door_status_lock = threading.Lock()


def _ensure_door_status_table():
    """门状态查询结果共享表(多worker跨进程可见)"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS door_status_queries (
                request_id text PRIMARY KEY,
                result jsonb,
                created_at timestamp DEFAULT CURRENT_TIMESTAMP,
                updated_at timestamp DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    except Exception:
        pass


_ensure_door_status_table()



@bp.route('/device/lock-status-report', methods=['POST'])
def device_lock_status_report():
    try:
        data = request.get_json(force=True) or {}
        request_id = data.get('request_id', '')
        logger.info('[lock-status-report] received req=%s full=%s', request_id, json.dumps(data)[:300])
        result_body = None
        if request_id:
            result_body = {
                'board_no': data.get('board_no'),
                'lock_no': data.get('lock_no'),
                'is_open': bool(data.get('is_open', False)),
                'door_status': data.get('door_status', 'unknown'),
                'query_success': bool(data.get('status') == 'ok' or data.get('query_success')),
                'status': data.get('status', 'ok' if data.get('query_success') else 'read_failed')
            }
            # DB 共享写入(任意worker可见)
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO door_status_queries (request_id, result, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (request_id) DO UPDATE
                    SET result = EXCLUDED.result, updated_at = CURRENT_TIMESTAMP
                ''', (request_id, json.dumps(result_body)))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error('[lock-status-report] db write failed: %s', e)
        if request_id and request_id in _door_status_results:
            with _door_status_lock:
                entry = _door_status_results.get(request_id)
                if entry:
                    entry['result'] = result_body
                    entry['event'].set()
                    logger.info('[lock-status-report] result set for req=%s', request_id)
        return json_response(data={'received': True})
    except Exception as e:
        logger.error('[lock-status-report] %s', e)
        return json_response(message=str(e), code=500)

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
        # 统计在线设备：最近 120 秒有心跳
        import datetime as dt_mod
        now = dt_mod.datetime.now()
        online_ids = set()
        c.execute("SELECT mainboard_device_id, last_heartbeat FROM cabinets WHERE last_heartbeat >= NOW() - INTERVAL '120 seconds'")
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
            result.insert(0, {'date': date, 'count': row['cnt'] if row else 0, 'amount': round(float(row['amt'] if row else 0), 2)})
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
        location_id = (data or {}).get('location_id', '') or request.args.get('location_id', '')
        agent_id = (data or {}).get('agent_id', '') or request.args.get('agent_id', '')
        device_id = (data or {}).get('device_id', '') or request.args.get('device_id', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if keyword:
            where += ' AND (cabinet_code LIKE %s OR name LIKE %s OR mainboard_device_id LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
        if device_id:
            where += ' AND mainboard_device_id LIKE %s'
            params.append(f'%{device_id}%')
        if location_id:
            where += ' AND c.location_id = %s'
            params.append(location_id)
        if agent_id:
            where += " AND c.location_id IN (SELECT id FROM locations WHERE merchant_id IN (SELECT id FROM merchants WHERE agent_id = %s))"
            params.append(agent_id)
        if status == 'online':
            where += " AND last_heartbeat >= NOW() - INTERVAL '120 seconds'"
        elif status == 'offline':
            where += " AND (last_heartbeat IS NULL OR last_heartbeat < NOW() - INTERVAL '120 seconds')"
        c.execute(f'SELECT COUNT(*) FROM cabinets c WHERE {where}', params)
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
        from helpers import is_heartbeat_online
        for row in c.fetchall():
            d = dict(row)
            d['is_online'] = is_heartbeat_online(d.get('last_heartbeat'))
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


def _push_usage_rules_to_device(device_id):
    """保存寄存规则后向在线设备推送 usage_rules_update"""
    try:
        import json as _json
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""SELECT c.usage_rules, c.rules_title, c.reopen_times,
                              l.usage_rules as loc_usage_rules, l.rules_title as loc_rules_title, l.reopen_times as loc_reopen_times,
                              l.show_slot_count
                       FROM cabinets c LEFT JOIN locations l ON c.location_id = l.id
                       WHERE c.mainboard_device_id = %s""", (device_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return
        rules = row['usage_rules'] or row['loc_usage_rules'] or ''
        title = row['rules_title'] or row['loc_rules_title'] or ''
        rt = row['reopen_times']
        if rt is None or rt == '' or int(rt) <= 0:
            rt = row['loc_reopen_times']
        ssc = row['show_slot_count']
        cmd = {'type': 'usage_rules_update', 'usage_rules': rules, 'rules_title': title,
               'reopen_times': '' if rt is None or rt == '' else str(rt),
               'show_slot_count': 1 if ssc is None or ssc == '' else int(ssc)}
        # 通过 ws_proxy(5004) 的 /send 转发: 设备WS长连接在5004进程内, 这里直接查本进程connected_devices是空的
        import urllib.request as _req
        _body = _json.dumps({"device_id": str(device_id), "command": cmd}).encode()
        _r = _req.urlopen("http://127.0.0.1:5004/send", data=_body, timeout=3)
        _resp = _json.loads(_r.read())
        if _resp.get("success"):
            logger.info('[usage_rules_update] pushed to device=%s via 5004', device_id)
        else:
            logger.warning('[usage_rules_update] push to device=%s failed: %s', device_id, _resp.get('error'))
    except Exception as e:
        logger.warning('[usage_rules_update] push failed: %s', e)


def _push_usage_rules_to_location(location_id):
    """保存网点规则后向该网点所有在线设备推送"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT mainboard_device_id FROM cabinets WHERE location_id=%s AND mainboard_device_id IS NOT NULL AND mainboard_device_id != ''", (location_id,))
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            _push_usage_rules_to_device(row['mainboard_device_id'])
    except Exception as e:
        logger.warning('[usage_rules_update] location push failed: %s', e)


@bp.route('/admin/cabinet/save', methods=['POST'])
@require_auth
def admin_cabinet_save():
    try:
        data = request.get_json()
        # 校验 per_use_price 范围
        _pp = data.get('per_use_price')
        if _pp is not None:
            try:
                _ppv = float(_pp)
                if _ppv < 0 or _ppv > 10000:
                    return json_response(message='per_use_price 必须在 0-10000 分（0-100元）之间', code=400)
            except (ValueError, TypeError):
                return json_response(message='per_use_price 格式错误', code=400)
        # 预付款金额：填 1 个为固定值，填 2 个（如 "15,25"）为随机区间，精度到分
        if 'deposit_range' in data:
            _dr_raw = str(data.get('deposit_range') or '').strip()
            _dr_parts = [p.strip() for p in _dr_raw.replace('，', ',').replace('-', ',').split(',') if p.strip() != '']
            if _dr_parts:
                try:
                    _dr_nums = [round(float(p), 2) for p in _dr_parts[:2]]
                except (ValueError, TypeError):
                    return json_response(message='预付款金额格式错误，请填写1个或2个数字', code=400)
                if len(_dr_nums) == 1:
                    data['deposit_min'] = _dr_nums[0]
                    data['deposit_max'] = _dr_nums[0]
                    data['deposit_amount'] = _dr_nums[0]
                else:
                    data['deposit_min'] = min(_dr_nums)
                    data['deposit_max'] = max(_dr_nums)
            else:
                data['deposit_min'] = None
                data['deposit_max'] = None
        conn = get_db()
        c = conn.cursor()
        if data.get('id'):
            fields = ['name','cabinet_code','location_id','mainboard_device_id','mainboard_source',
                     'total_slots','business_status','status','charge_mode',
                     'deposit_amount','per_use_price','customer_phone',
                     'usage_rules','rules_title','reopen_times','mid_retrieve_limit',
                     'deposit_min','deposit_max','free_days','daily_fee','slot_columns']
            sets, params = [], []
            for f in fields:
                if f in data:
                    v = data[f]
                    if isinstance(v, bool):
                        v = 1 if v else 0
                    elif f in ('deposit_amount','per_use_price') and (v == '' or v is None):
                        v = 0
                    elif f in ('deposit_min','deposit_max') and (v == '' or v is None):
                        v = None
                    elif f == 'free_days' and (v == '' or v is None):
                        v = 1
                    elif f == 'daily_fee' and (v == '' or v is None):
                        v = 0
                    elif f == 'reopen_times' and (v == '' or v is None):
                        v = None
                    elif f == 'mid_retrieve_limit' and (v == '' or v is None):
                        v = None
                    sets.append(f'{f}=%s')
                    params.append(v)
            params.append(data['id'])
            c.execute(f'UPDATE cabinets SET {",".join(sets)},updated_at=CURRENT_TIMESTAMP WHERE id=%s', params)
        else:
            cabinet_code = data.get('cabinet_code') or f'CAB{datetime.now().strftime("%Y%m%d%H%M%S")}'
            c.execute("""INSERT INTO cabinets (cabinet_code,name,location_id,mainboard_device_id,mainboard_source,
                total_slots,business_status,status,charge_mode,deposit_amount,
                customer_phone,per_use_price,usage_rules,rules_title,reopen_times,
                deposit_min,deposit_max,free_days,daily_fee,slot_columns) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (cabinet_code, data.get("name",""), int(data.get("location_id")) if data.get("location_id") and str(data.get("location_id")).strip() else None, data.get("mainboard_device_id"),
                 data.get("mainboard_source","WT"),
                 data.get("total_slots",12), data.get("business_status","open"),
                 data.get("status",1), data.get("charge_mode","deposit"),
                 float(data.get("deposit_amount") or 20),
                 data.get("customer_phone",""),
                 float(data.get("per_use_price") or 0),
                 data.get("usage_rules") or "24h",
                 data.get("rules_title") or "",
                 data.get("reopen_times") if data.get("reopen_times") not in ('', None) else None,
                 data.get("deposit_min") if data.get("deposit_min") not in ('', None) else None,
                 data.get("deposit_max") if data.get("deposit_max") not in ('', None) else None,
                 int(data.get("free_days") or 1),
                 float(data.get("daily_fee") or 0),
                 int(data.get("slot_columns") or 8)))
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
        _did = data.get('mainboard_device_id')
        if _did:
            _push_usage_rules_to_device(_did)
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
        c_apk.execute("SELECT version_name, version_code, download_url, COALESCE(file_md5, '') as file_md5 FROM apk_version ORDER BY version_code DESC LIMIT 1")
        apk_row = c_apk.fetchone()
        if not apk_row:
            conn_apk.close()
            return json_response(message="未找到APK版本信息，请先上传APK", code=400)
        latest_url = apk_row["download_url"]
        latest_ver = apk_row["version_name"]
        latest_code = apk_row["version_code"]
        latest_md5 = apk_row.get("file_md5", "") or ""
        conn_apk.close()

        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '')
        if not device_id:
            return json_response(message='缺少设备ID', code=400)

        import json as _json
        conn3 = get_db()
        c3 = conn3.cursor()
        c3.execute('SELECT id, app_version_code, last_heartbeat FROM cabinets WHERE mainboard_device_id=%s', (device_id,))
        cab = c3.fetchone()
        if not cab:
            conn3.close()
            return json_response(message='设备不存在', code=404)
        from helpers import is_device_online
        if not is_device_online(device_id, cab.get('last_heartbeat')):
            conn3.close()
            return json_response(message='设备离线，无法更新', code=400)
        current_code = cab.get('app_version_code')
        if current_code is not None and int(current_code) >= int(latest_code):
            conn3.close()
            return json_response(message='设备已是最新版本，无需更新', code=400)
        c3.execute("SELECT id FROM pending_lock_cmds WHERE device_id=%s AND (delivered=0 OR status='pending') AND strpos(command,'force_update')>0 AND created_at > NOW() - INTERVAL '10 minutes' LIMIT 1", (device_id,))
        if c3.fetchone():
            conn3.close()
            return json_response(message='该设备已有待执行的更新指令', code=400)
        supersede_force_update_cmds(c3, device_id)
        cmd = _json.dumps({'type': 'force_update', 'device_id': device_id, 'download_url': latest_url, 'version_name': latest_ver, 'version_code': latest_code, 'force': True, 'file_md5': latest_md5})
        c3.execute('INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, status) VALUES (%s,%s,%s,%s)', (device_id, cab['id'], cmd, 'pending'))
        conn3.commit()
        conn3.close()
        logger.info(f'[force_update] OK device={device_id} cabinet={cab["id"]} version={latest_ver}')
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

@bp.route('/admin/device/restart', methods=['POST'])
@require_auth
def admin_device_restart():
    """远程重启设备(推送reboot指令, 设备3秒后系统重启)"""
    try:
        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '')
        if not device_id:
            return json_response(message='缺少设备ID', code=400)
        import json as _json
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id, last_heartbeat FROM cabinets WHERE mainboard_device_id=%s', (device_id,))
        cab = c.fetchone()
        if not cab:
            conn.close()
            return json_response(message='设备不存在', code=404)
        from helpers import is_device_online
        if not is_device_online(device_id, cab.get('last_heartbeat')):
            conn.close()
            return json_response(message='设备离线，无法重启', code=400)
        cmd = _json.dumps({'type': 'reboot', 'device_id': device_id})
        c.execute('INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, status) VALUES (%s,%s,%s,%s)',
                  (device_id, cab['id'], cmd, 'pending'))
        conn.commit()
        conn.close()
        logger.info(f'[reboot] OK device={device_id}')
        return json_response(message='已推送重启指令')
    except Exception as e:
        logger.error(f'[reboot] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/admin/slots/open-all', methods=['POST'])
@require_auth
def admin_slots_open_all():
    """一键开门 - 方案C列表开门(单命令带门列表,设备按序执行), 只开空闲(1)/占用(2), 故障/锁定跳过"""
    try:
        data = request.get_json()
        cabinet_id = data.get('cabinet_id')
        if not cabinet_id:
            return json_response(message='缺少柜体ID', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT mainboard_device_id, last_heartbeat, mainboard_source FROM cabinets WHERE id=%s', (cabinet_id,))
        row = c.fetchone()
        conn.close()
        if not row or not row['mainboard_device_id']:
            return json_response(message='未找到设备', code=400)
        from helpers import is_device_online, send_open_lock_list
        if not is_device_online(str(row['mainboard_device_id']), row.get('last_heartbeat')):
            return json_response(message='设备离线，无法发送开门指令', code=400)
        did = str(row['mainboard_device_id'])
        protocol = row.get('mainboard_source') or 'YBM'
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT cs.slot_number, cs.board_no, cs.lock_no FROM cabinet_slots cs WHERE cs.cabinet_id = %s AND cs.status IN (1, 2) ORDER BY cs.slot_number', (cabinet_id,))
        slots = c.fetchall()
        conn.close()
        if not slots:
            return json_response(message='没有可开的正常柜门', code=400)
        doors = [(s.get('board_no') or 1, s.get('lock_no') or s.get('slot_number') or 1) for s in slots]
        ok = send_open_lock_list(did, doors, protocol=protocol)
        if not ok:
            return json_response(message='开门指令发送失败', code=500)
        return json_response(message=f'已发送{len(doors)}个柜门开锁指令（列表开门）', data={'opened': [s['slot_number'] for s in slots]})
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
        agent_id = (data or {}).get('agent_id', '') or request.args.get('agent_id', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if keyword:
            where += ' AND (l.name LIKE %s OR l.address LIKE %s)'
            params.extend([f'%{keyword}%', f'%{keyword}%'])
        if agent_id:
            where += ' AND l.merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)'
            params.append(agent_id)
        c.execute(f'SELECT COUNT(*) FROM locations l WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT l.*, m.name as merchant_name,
            (SELECT COUNT(*) FROM cabinets WHERE location_id=l.id) as cabinet_count,
            (SELECT COUNT(*) FROM cabinets WHERE location_id=l.id AND last_heartbeat>=NOW() - INTERVAL '120 seconds') as online_count,
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
                     'allow_slot_select','slot_assign_mode','allow_mid_retrieve','mid_retrieve_limit','retrieve_mode',
                     'usage_rules','rules_title','reopen_times','allow_open_after_end',
                     'allow_h5_to_mp','show_qr_follow','force_follow_mp','h5_url',
                     'show_slot_count','screen_show_title','screen_title',
                     'slot_full_alert','slot_full_text','end_alert_minutes',
                     'enable_clear_box','clear_box_time','clear_box_cycle',
                     'deposit_random','deposit_min','deposit_max',
                     'reject_whitelist_after',
                     'withdraw_enabled','show_refunding_status','refund_mode','withdraw_mode',
                     'auto_approve_day',
                     'auto_approve_time','auto_approve_rate','click_free_count',
                     'anti_test_minutes','anti_test_auto_refund','hide_ratio',
                    'hide_start_orders',
                     'whitelist_phones','duplicate_filter_enabled','duplicate_filter_days','duplicate_filter_limit',
                     'refund_approve_rate','refund_approve_start_min','refund_approve_end_min',
                     'balance_hide_enabled','balance_hide_days','wl_max_uses']
            sets, params = [], []
            for f in fields:
                if f in data:
                    v = data[f]
                    if isinstance(v, bool):
                        v = 1 if v else 0
                    elif f == 'wl_max_uses' and (v == '' or v is None):
                        v = 3
                    elif f == 'wl_max_uses':
                        try:
                            v = int(v)
                        except Exception:
                            v = 3
                    elif f in ('deposit_amount','per_use_price') and (v == '' or v is None):
                        v = 0
                    elif f == 'reopen_times' and (v == '' or v is None):
                        v = None
                    elif f == 'mid_retrieve_limit' and (v == '' or v is None):
                        v = None
                    sets.append(f'{f}=%s')
                    params.append(v)
            params.append(data['id'])
            c.execute(f'UPDATE locations SET {",".join(sets)} WHERE id=%s', params)
        else:
            c.execute('''INSERT INTO locations (name,address,longitude,latitude,merchant_id,status,
                contact_name,contact_phone,allow_open_after_end,usage_rules,rules_title,reopen_times,mid_retrieve_limit) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (data.get('name'), data.get('address'), data.get('longitude'),
                 data.get('latitude'), data.get('merchant_id'), data.get('status',1),
                 data.get('contact_name',''), data.get('contact_phone',''),
                 1 if data.get('allow_open_after_end', True) else 0,
                 data.get('usage_rules',''),
                 data.get('rules_title') or '',
                 data.get('reopen_times') if data.get('reopen_times') not in ('', None) else None,
                 data.get('mid_retrieve_limit') if data.get('mid_retrieve_limit') not in ('', None) else None))
            data['id'] = c.lastrowid
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
                        if refund_success or ('已退款' in str(refund_msg)) or ('全额退款' in str(refund_msg)):
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
        _lid = data.get('id')
        if _lid:
            _push_usage_rules_to_location(_lid)
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
        agent_id = data.get('agent_id', '')
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
        if data.get('logic_mark') == 'Y':
            where += " AND (o.logic_mark = 'Y' OR (o.auto_hidden = 1 AND (o.logic_mark IS NULL OR o.logic_mark != 'N')))"
        elif data.get('logic_mark') == 'N':
            where += " AND o.logic_mark = 'N'"
        if data.get('refund_mark'):
            where += ' AND o.refund_mark = %s'
            params.append(data['refund_mark'])
        if data.get('location_id'):
            where += ' AND c.location_id = %s'
            params.append(data['location_id'])
        if agent_id:
            where += ' AND l.merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)'
            params.append(agent_id)
        if data.get('merchant_id'):
            where += ' AND l.merchant_id = %s'
            params.append(data['merchant_id'])
        if data.get('device_id'):
            where += ' AND o.cabinet_id = %s'
            params.append(data['device_id'])
        if data.get('wechat_name'):
            where += ' AND (ub.wechat_name LIKE %s OR up.wechat_name LIKE %s)'
            params.extend(['%' + data['wechat_name'] + '%', '%' + data['wechat_name'] + '%'])
        if data.get('compartment_number'):
            where += ' AND CAST(o.compartment_number AS TEXT) LIKE %s'
            params.append('%' + data['compartment_number'] + '%')
        # Date range filter, default 30 days
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        if start_date and end_date:
            where += ' AND date(o.created_at)>=%s AND date(o.created_at)<=%s'
            params.extend([start_date, end_date])
        elif not start_date and not end_date:
            # Default: last 30 days
            where += " AND o.created_at>=NOW() - INTERVAL '30 days'" 
        c.execute(f'SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON o.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f"""SELECT o.id, o.order_no, o.user_phone, o.access_code as password, o.compartment_number, o.deposit_amount, o.per_use_price, o.refund_status, CASE WHEN o.status=4 THEN COALESCE(o.refund_amount,0) ELSE 0 END as refund_amount, o.status,
            o.store_time, o.retrieve_time, o.created_at, o.group_id, COALESCE(c.cabinet_code, o.cabinet_code) as cabinet_code, c.name as cabinet_name,
            o.transaction_id, o.pay_time, o.refund_time, o.refund_mark, o.logic_mark, o.auto_hidden,
            COALESCE(NULLIF(ub.wechat_name,''), NULLIF(po.wechat_name,''), up.wechat_name) as wechat_name,""" + f"""
            l.id as location_id, l.name as location_name, m.name as merchant_name, m.id as merchant_id, pc.mch_id as pay_mch_id
            FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id
            LEFT JOIN cabinet_slots cs ON o.slot_id=cs.id
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON o.user_phone=po.phone
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
            # 显示规则：手动隐藏或自动隐藏且未手动显示
            if d.get('logic_mark') != 'N' and (d.get('auto_hidden') or 0) == 1:
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
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON o.user_phone=po.phone
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
            dev_row = c.execute("SELECT c.mainboard_device_id FROM cabinet_slots cs JOIN cabinets c ON cs.cabinet_id=c.id WHERE cs.id=%s", (slot_id,))
            dev_row = c.fetchone()
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
        # 重复退款保护：已退款订单禁止再次退款 (2026-08-20)
        if order_dict.get('refund_status') == 'refunded' or float(order_dict.get('refund_amount') or 0) >= float(order_dict.get('deposit_amount') or 0) - 0.001:
            conn.close()
            return json_response(message='订单已退款，无需重复退款', code=400)
        amount = order_dict.get('deposit_amount', 0)
        # 原支付金额优先取微信支付流水，兜底用押金+按次费
        c.execute("SELECT amount FROM payments WHERE order_id=%s AND type=1 AND status=1 AND amount<=1000 ORDER BY id LIMIT 1", (order_id,))
        _paid_row = c.fetchone()
        if _paid_row and _paid_row[0]:
            total_fee = int(float(_paid_row[0]) * 100)
        else:
            total_fee = int((float(amount) + float(order_dict.get('per_use_price') or 0)) * 100)
        refund_fee = int(float(amount) * 100)
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
                    c.execute('SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id DESC LIMIT 1')
                    active_ch = c.fetchone()
                    if active_ch:
                        wxpay_inst, _ = get_channel_wxpay(dict(active_ch))
                    else:
                        return json_response(message='无可用活跃商户，无法退款', code=400)
                refund_result = wxpay_inst.refund(
                    out_trade_no=order_no,
                    total_fee=total_fee,
                    refund_fee=refund_fee,
                    out_refund_no=refund_no,
                    refund_desc=''
                )
                if refund_result and refund_result.get('return_code') == 'SUCCESS' and refund_result.get('result_code') == 'SUCCESS':
                    actual_refund = True
                    logger.info(f'[order_refund] 微信退款成功 order={order_no} refund_no={refund_no}')
                else:
                    err_msg = (refund_result.get('err_code_des') or refund_result.get('err_code') or refund_result.get('return_msg') or '未知错误') if refund_result else '无返回'
                    # 已退款/已全额退款视为成功（之前退款成功但本地DB未更新的场景）
                    if refund_result and ('已退款' in str(refund_result.get('err_code_des') or '') or '全额退款' in str(refund_result.get('err_code_des') or '')):
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
        # 微信退款成功或无transaction_id(MOCK)，才更新本地状态；refund_id 回填微信退款单号(无则用商户单号)
        _rid_val = ''
        try:
            if refund_result and refund_result.get('refund_id'):
                _rid_val = refund_result.get('refund_id')
        except Exception:
            pass
        if not _rid_val:
            _rid_val = refund_no
        c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_id=%s, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                  (_rid_val, amount, order_id))
        c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order_id,))
        # 联动更新待审核的提现记录：按 order_ids 匹配合并记录逐单扣减，避免重复记账/金额虚高 (2026-08-20)
        import json as _json_wd
        _wd_matched = False
        c.execute("SELECT id, amount, order_ids FROM withdrawal_records WHERE status=0 AND (order_ids::jsonb @> %s OR order_id=%s) ORDER BY id",
                  (_json_wd.dumps([order_id]), order_id))
        for _wrow in c.fetchall():
            _wd_matched = True
            try:
                _oids = _json_wd.loads(_wrow['order_ids'] or '[]')
            except Exception:
                _oids = []
            if order_id in _oids:
                _oids.remove(order_id)
            _new_amt = max(0.0, float(_wrow['amount'] or 0) - float(amount))
            if _oids:
                # 还有其他未退订单：保持待处理，扣减金额和订单
                c.execute("UPDATE withdrawal_records SET amount=%s, order_ids=%s, error_msg=NULL, retry_count=0 WHERE id=%s",
                          (round(_new_amt, 2), _json_wd.dumps(_oids), _wrow['id']))
            else:
                # 全部订单退完：置成功
                c.execute("UPDATE withdrawal_records SET status=2, amount=%s, order_ids=%s, approver='管理员', approve_time=CURRENT_TIMESTAMP WHERE id=%s",
                          (round(_new_amt, 2), _json_wd.dumps(_oids), _wrow['id']))
        if not _wd_matched:
            c.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, order_ids, approve_time) VALUES (%s, %s, %s, 2, '管理员', %s, NOW())",
                      (order_id, order_dict.get('user_phone'), amount, f'[{order_id}]'))
        # 记录payments退款流水
        c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, %s)',
                  (order_id, amount, transaction_id, refund_no, datetime.now()))
        # 如果订单是已结算(3)，保证金已从余额退过，需要扣回余额
        if order_dict.get('status') == 3 and order_dict.get('user_phone'):
            c.execute("UPDATE user_balances SET balance = balance + %s, total_withdrawn = total_withdrawn + %s WHERE phone = %s",
                      (-amount, amount, order_dict.get("user_phone")))
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
            c.execute("UPDATE user_balances SET balance = balance + %s, total_deposited = total_deposited + %s WHERE phone = %s",
                      (deposit_amount, deposit_amount, order_dict.get("user_phone")))
            # 写入余额明细（灰度：新提现逻辑）
            c.execute("INSERT INTO user_balance_details (user_phone, order_id, amount, status) VALUES (%s, %s, %s, 'available') ON CONFLICT (order_id) DO NOTHING",
                      (order_dict['user_phone'], order_id, deposit_amount))
        conn.commit()
        
        # 发送寄存结束订阅消息
        # 只认小程序 mp_openid（oWrA8 前缀）；公众号 openid(oLhbm2) 发不了订阅消息
        def _is_mp_openid(v):
            return bool(v) and str(v).startswith('oWrA8')
        ntf_openid = order_dict.get('mp_openid') or ''
        if not _is_mp_openid(ntf_openid):
            ntf_openid = order_dict.get('openid') or ''
        if not _is_mp_openid(ntf_openid) and order_dict.get('user_phone'):
            try:
                c2 = conn.cursor(cursor_factory=RealDictCursor)
                c2.execute("SELECT mp_openid FROM user_balances WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' AND mp_openid LIKE 'oWrA8%%' LIMIT 1", (order_dict['user_phone'],))
                _r = c2.fetchone()
                if _r and _r['mp_openid']:
                    ntf_openid = _r['mp_openid']
                if not ntf_openid:
                    c2.execute("SELECT mp_openid FROM users WHERE phone = %s AND mp_openid IS NOT NULL AND mp_openid != '' AND mp_openid LIKE 'oWrA8%%' ORDER BY updated_at DESC LIMIT 1", (order_dict['user_phone'],))
                    _r = c2.fetchone()
                    if _r and _r['mp_openid']:
                        ntf_openid = _r['mp_openid']
            except Exception as _e:
                logger.warning(f"[order_close] ?openid??: {_e}")
        if ntf_openid:
            try:
                from helpers import send_wx_subscribe_message
                subscribe_data = {
                    'amount6': {'value': '¥{:.2f}'.format(deposit_amount)},
                    'time4': {'value': now},
                    'thing7': {'value': '已退还至小程序用户钱包'},
                    'thing2': {'value': '请自行点击此通知消息跳转“我的钱包”提现'}
                }
                send_wx_subscribe_message(ntf_openid, '5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A', subscribe_data, phone=order_dict.get('user_phone'), page='pages/mine/mine')
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
        user = find_user_balance_row(c, phone=phone)
        if not user or (user['balance'] or 0) <= 0:
            conn.close()
            return json_response(message='用户余额不足或该手机号绑定多个微信，请先选择身份', code=400)
        refund_amount = min(amount, user['balance'] or 0)
        upsert_user_balance_row(c, phone=phone, openid=user.get('openid', ''),
                                unionid=user.get('unionid', '') or '',
                                mp_openid=user.get('mp_openid', '') or '',
                                balance=-refund_amount, total_withdrawn=refund_amount,
                                user_id=user.get('user_id') or 0)
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
                    c.execute('SELECT * FROM payment_channels WHERE is_active=1 ORDER BY id DESC LIMIT 1')
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
                    # 已退款/已全额退款视为成功
                    if refund_result and ('已退款' in str(refund_result.get('err_code_des') or '') or '全额退款' in str(refund_result.get('err_code_des') or '')):
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
            # refund_id 回填微信退款单号(无则用商户单号)，避免订单表退款单号为空
            _rid_val2 = ''
            try:
                if refund_result and refund_result.get('refund_id'):
                    _rid_val2 = refund_result.get('refund_id')
            except Exception:
                pass
            if not _rid_val2:
                _rid_val2 = refund_no
            c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_id=%s, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                      (_rid_val2, order['deposit_amount'], order['id']))
            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order['id'],))
            c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, %s)',
                      (order['id'], refund_amount, order['transaction_id'], refund_no, datetime.now()))
            c.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, order_ids, approve_time) VALUES (%s, %s, %s, 2, '管理员', %s, NOW())",
                      (order['id'], phone, refund_amount, f'[{order["id"]}]'))
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
            user = find_user_balance_row(c, phone=phone)
            if user is None:
                conn.rollback()
                conn.close()
                return json_response(message='手机号 %s 存在多个微信身份，已停用批量退款；请使用按订单退款或提现审批' % phone, code=400)
            if not user or (user['balance'] or 0) <= 0:
                continue
            refund_amount = user['balance'] or 0
            c.execute('UPDATE user_balances SET balance=0, total_withdrawn=total_withdrawn+balance WHERE phone=%s ', (phone,))
            c.execute("SELECT id, order_no, transaction_id, deposit_amount, payment_channel_id FROM orders WHERE user_phone=%s AND status != 4 AND refund_status!='refunded' ORDER BY id DESC LIMIT 1", (phone,))
            order = c.fetchone()
            refund_no = 'RF_B' + datetime.now().strftime('%Y%m%d%H%M%S') + str(phone)[-4:]
            if order:
                # 批量退款不调微信(余额退款)，用商户单号回填 refund_id，避免订单表退款单号为空
                c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_id=%s, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                          (refund_no, order['deposit_amount'], order['id']))
                c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order['id'],))
                c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, %s)',
                          (order['id'], refund_amount, order['transaction_id'], refund_no, datetime.now()))
                c.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, order_ids, approve_time) VALUES (%s, %s, %s, 2, '管理员', %s, NOW())",
                          (order['id'], phone, refund_amount, f'[{order["id"]}]'))
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
        c.execute('''SELECT o.*, c.mainboard_device_id, c.last_heartbeat, cs.slot_number, cs.board_no, cs.lock_no FROM orders o 
            JOIN cabinets c ON o.cabinet_id=c.id 
            LEFT JOIN cabinet_slots cs ON o.slot_id=cs.id
            WHERE o.id=%s''', (order_id,))
        order = c.fetchone()
        conn.close()
        if not order or not order['mainboard_device_id']:
            return json_response(message='订单或设备不存在', code=404)
        order = dict(order)
        from helpers import is_device_online
        if not is_device_online(str(order['mainboard_device_id']), order.get('last_heartbeat')):
            return json_response(message='设备离线，无法开门', code=400)
        device_id = str(order['mainboard_device_id'])
        board_no = order.get('board_no') or 1
        lock_no = order.get('lock_no') or (order.get('slot_number', 1) - (order.get('board_no', 1) - 1) * 16) or 1
        from helpers import send_open_lock
        send_open_lock(device_id, board_no, lock_no, None, order.get('order_no', str(order_id)), require_online=True, manual=True)
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

@bp.route('/admin/recharge-records', methods=['GET'])
@require_auth
def admin_recharge_records():
    """会员充值记录: payments关联orders, 带手机号/商户订单号/时间, 支持时间筛选(默认当天)"""
    try:
        from datetime import datetime as _dt, timedelta as _td
        data = request.args
        page = int(data.get('page', 1))
        limit = int(data.get('limit', 10))
        phone = (data.get('phone') or '').strip()
        start_date = (data.get('start_date') or '').strip()
        end_date = (data.get('end_date') or '').strip()
        today = _dt.now().strftime('%Y-%m-%d')
        if not start_date:
            start_date = today
        if not end_date:
            end_date = today
        conn = get_db()
        c = conn.cursor()
        where = "WHERE p.type IN (1,2) AND p.status = 1"
        params = []
        if phone:
            where += ' AND o.user_phone LIKE %s'
            params.append(f'%{phone}%')
        if start_date:
            where += " AND p.created_at >= %s::timestamp"
            params.append(start_date + ' 00:00:00')
        if end_date:
            where += " AND p.created_at <= %s::timestamp"
            params.append(end_date + ' 23:59:59')
        c.execute(f'''SELECT COUNT(*) FROM payments p
                      JOIN orders o ON o.id = p.order_id
                      {where}''', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT p.id, p.order_id, p.amount, p.transaction_id, p.refund_transaction_id,
                             p.created_at, o.user_phone, o.order_no, o.deposit_amount
                      FROM payments p
                      JOIN orders o ON o.id = p.order_id
                      {where}
                      ORDER BY p.id DESC LIMIT %s OFFSET %s''', params + [limit, (page-1)*limit])
        rows = []
        for r in c.fetchall():
            d = dict(r)
            if d.get('created_at'):
                d['create_time'] = d['created_at'].strftime('%Y-%m-%d %H:%M:%S') if hasattr(d['created_at'], 'strftime') else str(d['created_at'])
            rows.append(d)
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page})
    except Exception as e:
        logger.error(f'[recharge_records] {e}')
        return json_response(message=str(e), code=500)


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
        location_id = (data or {}).get('location_id', '') or request.args.get('location_id', '')
        agent_id = (data or {}).get('agent_id', '') or request.args.get('agent_id', '')
        approver = (data or {}).get('approver', '') or request.args.get('approver', '')
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
        if approver:
            where += ' AND wr.approver LIKE %s'
            params.append(f'%%{approver}%%')
        if date_start:
            where += ' AND wr.created_at >= %s'
            params.append(date_start)
        if date_end:
            where += ' AND wr.created_at <= %s'
            params.append(date_end + ' 23:59:59')
        if location_id:
            where += ' AND ca.location_id = %s'
            params.append(location_id)
        if agent_id:
            where += ' AND lc.merchant_id IN (SELECT id FROM merchants WHERE agent_id = %s)'
            params.append(agent_id)
        join_sql = """FROM withdrawal_records wr
                      LEFT JOIN orders o ON wr.order_id=o.id
                      LEFT JOIN cabinets ca ON o.cabinet_id=ca.id
                      LEFT JOIN locations lc ON ca.location_id=lc.id
                      LEFT JOIN (SELECT phone, MAX(wechat_name) as wechat_name FROM user_balances GROUP BY phone) ub ON wr.user_phone=ub.phone
                      LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON wr.user_phone=po.phone
                      LEFT JOIN user_profiles up ON po.openid=up.openid"""
        c.execute(f'SELECT COUNT(*) {join_sql} WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f"SELECT wr.*, o.order_no, o.refund_id, lc.name as location_name, COALESCE(NULLIF(ub.wechat_name,''), NULLIF(po.wechat_name,''), up.wechat_name, '') as wechat_name {join_sql} WHERE {where} ORDER BY wr.created_at DESC LIMIT %s OFFSET %s",
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
            failed_amount = 0.0
            failed_oids = []
            for oid in order_ids_list:
                c.execute('SELECT deposit_amount, COALESCE(refund_amount,0) as refund_amount FROM orders WHERE id=%s', (oid,))
                od = c.fetchone()
                if od:
                    refund_this = float(od['deposit_amount']) - float(od['refund_amount'])
                    if refund_this > 0.001:
                        ok, rid, rmsg = do_real_refund(order_id=oid, amount=refund_this, payment_channel_id=wd.get('payment_channel_id'))
                        if ok and '已退款' not in rmsg and '全额退款' not in rmsg:
                            c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=NOW(), refund_amount=COALESCE(refund_amount,0)+%s WHERE id=%s', (rid, refund_this, oid))
                            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (oid,))
                        elif ok and ('已退款' in rmsg or '全额退款' in rmsg):
                            # do_real_refund 已按成功同步订单状态，这里只算作成功，不恢复余额
                            pass
                        else:
                            all_ok = False
                            failed_amount += refund_this
                            failed_oids.append(oid)
            c.execute('UPDATE withdrawal_records SET status=%s, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (2 if all_ok else 4, session.get('admin_username', 'admin'), withdrawal_id))
            if failed_amount > 0:
                upsert_user_balance_row(c, phone=wd.get('user_phone', ''), openid=wd.get('openid', ''),
                                        unionid=wd.get('unionid', '') or '',
                                        mp_openid=wd.get('mp_openid', '') or '',
                                        balance=failed_amount, total_withdrawn=-failed_amount,
                                        user_id=wd.get('user_id') or 0)
                for foid in failed_oids:
                    c.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (foid,))
            conn.commit()
            conn.close()
            return json_response(message='审批通过，退款已处理' if all_ok else '审批通过，部分退款失败')
        # 兼容旧逻辑：单个order_id
        # 余额已在用户提现时扣除，无需再次扣除
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
        if refund_success or ('已退款' in str(refund_msg)) or ('全额退款' in str(refund_msg)):
            c.execute('UPDATE withdrawal_records SET status=2, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (session.get('admin_username', 'admin'), withdrawal_id))
            if order_id:
                c.execute('UPDATE orders SET status=4, refund_id=%s, refund_time=%s, refund_amount=%s WHERE id=%s', (refund_id, datetime.now(), amount, order_id))
                c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s", (order_id,))
        else:
            c.execute('UPDATE withdrawal_records SET status=4, error_msg=%s, approver=%s, approve_time=CURRENT_TIMESTAMP WHERE id=%s',
                       (str(refund_msg), session.get('admin_username', 'admin'), withdrawal_id))
            if order_id:
                c.execute("UPDATE orders SET status=3, refund_status='none', refund_mark=0 WHERE id=%s", (order_id,))
                c.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (order_id,))
            upsert_user_balance_row(c, phone=wd.get('user_phone', ''), openid=wd.get('openid', ''),
                                    unionid=wd.get('unionid', '') or '',
                                    mp_openid=wd.get('mp_openid', '') or '',
                                    balance=float(amount), total_withdrawn=-float(amount),
                                    user_id=wd.get('user_id') or 0)
        conn.commit()
        conn.close()
        if refund_success or ('已退款' in str(refund_msg)) or ('全额退款' in str(refund_msg)):
            return json_response(message='审批通过，退款已完成')
        else:
            return json_response(message='审批通过，但退款失败，请手动确认退款')
    except Exception as e:
        logger.error('[withdrawal_approve] ' + str(e))
        return json_response(message=str(e), code=500)


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
            upsert_user_balance_row(c, phone=wd['user_phone'], openid=wd.get('openid', ''),
                                    unionid=wd.get('unionid', '') or '',
                                    mp_openid=wd.get('mp_openid', '') or '',
                                    balance=wd['amount'], total_withdrawn=-wd['amount'],
                                    user_id=wd.get('user_id') or 0)
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
        mch_id = (data or {}).get('mch_id', '') or request.args.get('mch_id', '')
        location_id = (data or {}).get('location_id', '') or request.args.get('location_id', '')
        start_date = (data or {}).get('start_date', '') or request.args.get('start_date', '')
        end_date = (data or {}).get('end_date', '') or request.args.get('end_date', '')
        page = int(request.args.get("page", (data or {}).get("page", 1)))
        page_size = int(request.args.get("limit", (data or {}).get("limit", 20)))
        conn = get_db()
        c = conn.cursor()
        where, params = "1=1", []
        if complaint_type:
            if complaint_type == 'self':
                _search_active = bool(phone or order_no or mch_id or status)
                if _search_active:
                    where += " AND (c.complaint_type IN ('self','complaint') OR c.type IN ('self','complaint'))"
                else:
                    where += " AND (c.complaint_type IN ('self','complaint') OR c.type IN ('self','complaint')) AND (c.status IN ('0','1') OR (c.status='2' AND (c.reply LIKE %s OR c.reply LIKE %s)))"
                    params.extend(['%自动退款失败%', '%退款失败%'])
            else:
                where += ' AND c.complaint_type=%s'
                params.append(complaint_type)
        if status:
            if status == 'pending':
                where += ' AND c.status IN (\'0\', \'1\')'
            elif status == 'processed':
                where += ' AND c.status IN (\'2\', \'3\')'
            elif status == 'error':
                where += ' AND c.status IN (\'4\', \'99\')'
            elif status == 'refund_failed':
                where += " AND c.refund_status = 'refund_failed' AND (o.refund_status IS NULL OR o.refund_status NOT IN ('refunded','success'))"
            else:
                try:
                    where += ' AND c.status=%s'
                    params.append(str(int(status)))
                except:
                    pass
        if phone:
            where += ' AND (c.user_phone LIKE %s OR o.user_phone LIKE %s)'
            params.extend([f'%{phone}%', f'%{phone}%'])
        if order_no:
            where += ' AND (c.order_no LIKE %s OR o.order_no LIKE %s)'
            params.extend([f'%{order_no}%', f'%{order_no}%'])
        if mch_id:
            where += ' AND (c.mch_id LIKE %s OR pc.mch_id LIKE %s)'
            params.extend([f'%{mch_id}%', f'%{mch_id}%'])
        if location_id:
            where += ' AND ca.location_id = %s'
            params.append(location_id)
        if start_date:
            where += ' AND c.created_at >= %s'
            params.append(start_date)
        if end_date:
            where += ' AND c.created_at < %s::date + INTERVAL \'1 day\''
            params.append(end_date)
        c.execute(f'''SELECT COUNT(*) FROM complaints c
            LEFT JOIN orders o ON c.order_id=o.id OR (c.order_no IS NOT NULL AND c.order_no = o.order_no)
            LEFT JOIN payment_channels pc ON o.payment_channel_id=pc.id
            LEFT JOIN cabinets ca ON o.cabinet_id=ca.id
            WHERE {where}''', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT c.*, CASE WHEN c.source IS NOT NULL AND c.source != '' THEN c.source WHEN c.type IN ('self','complaint') OR c.complaint_type IN ('self','complaint') THEN '自有投诉' WHEN c.type='wechat' OR c.complaint_type='wechat' THEN '微信投诉' WHEN c.type='kf' OR c.complaint_type='kf_auto' THEN '客服自动处理' ELSE COALESCE(c.type,'') END as source, COALESCE(NULLIF(po.wechat_name,''), NULLIF(up.wechat_name,''), o.wechat_name, c.nick_name) as nickname, CASE WHEN o.status IN (2,3) THEN o.order_no ELSE c.order_no END as order_no, CASE WHEN c.type = 'self' THEN c.user_phone ELSE COALESCE(o.user_phone, c.user_phone) END as user_phone, pc.mch_id, ca.cabinet_code, l.name as location_name, o.refund_status as order_refund_status
            FROM complaints c LEFT JOIN orders o ON c.order_id=o.id OR (c.order_no IS NOT NULL AND c.order_no = o.order_no) LEFT JOIN (SELECT DISTINCT ON (phone) phone, openid, wechat_name FROM users ORDER BY phone, id DESC) po ON po.phone=COALESCE(NULLIF(o.user_phone,''), NULLIF(c.user_phone,'')) LEFT JOIN user_profiles up ON up.openid=COALESCE(c.openid, po.openid, o.openid) LEFT JOIN payment_channels pc ON o.payment_channel_id=pc.id LEFT JOIN cabinets ca ON o.cabinet_id=ca.id LEFT JOIN locations l ON ca.location_id=l.id
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


@bp.route('/admin/self-complaint/user-orders', methods=['POST'])
@require_auth
def self_complaint_user_orders():
    try:
        data = request.get_json()
        phone = data.get('phone', '')
        if not phone:
            return json_response(message='手机号为空', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT o.id, o.order_no, o.deposit_amount, o.store_time, o.status, pc.mch_id FROM orders o LEFT JOIN payment_channels pc ON o.payment_channel_id=pc.id WHERE o.user_phone = %s AND o.status IN (2,3) AND COALESCE(o.refund_status, '') NOT IN ('success','refunded') ORDER BY o.id DESC LIMIT 50", (phone,))
        orders = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': orders, 'total': len(orders)})
    except Exception as e:
        logger.error('[self_complaint_user_orders] %s' % str(e))
        return json_response(message=str(e), code=500)


@bp.route('/admin/complaint/retry-refund', methods=['POST'])
@require_auth
def admin_complaint_retry_refund():
    try:
        from helpers import do_real_refund
        data = request.get_json()
        complaint_id = data.get('id')
        logger.info('[retry_refund] 管理员手动退款 complaint_id=%s admin=%s', complaint_id, session.get('admin_username', ''))
        if not complaint_id:
            return json_response(message='id为空', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT c.*, o.id as order_id, o.order_no, o.deposit_amount, o.payment_channel_id FROM complaints c LEFT JOIN orders o ON c.order_id=o.id WHERE c.id=%s", (complaint_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return json_response(message='投诉不存在', code=404)
        oid = row.get('order_id')
        ono = row.get('order_no')
        deposit_amount = row.get('deposit_amount', 0)
        payment_channel_id = row.get('payment_channel_id')
        if not oid or not ono:
            # 自有投诉可能没有order_id，尝试通过用户手机号查最近订单
            if row.get('openid') or row.get('user_phone'):
                _phones = []
                _oid = row.get('openid', '')
                if _oid:
                    c.execute('SELECT DISTINCT phone FROM users WHERE unionid = (SELECT unionid FROM users WHERE mp_openid = %s AND unionid IS NOT NULL LIMIT 1) AND phone IS NOT NULL AND phone != chr(39)||chr(39)', (_oid,))
                    _phones = [r[0] for r in c.fetchall()]
                if not _phones and row.get('user_phone'):
                    _phones = [row['user_phone']]
                ord_row = None
                if _phones:
                    phs = ','.join(['%s'] * len(_phones))
                    c.execute('SELECT id, order_no, deposit_amount, payment_channel_id FROM orders WHERE user_phone IN (' + phs + ') AND status IN (2,3) ORDER BY id DESC LIMIT 1', tuple(_phones))
                    ord_row = c.fetchone()
                if ord_row:
                    oid = ord_row[0]
                    ono = ord_row[1]
                    deposit_amount = ord_row[2] or 0
                    payment_channel_id = ord_row[3]

        if not oid:
            oid = data.get("complaint_order_id")
        if not ono:
            ono = data.get("order_no")
        if not deposit_amount:
            deposit_amount = data.get("deposit_amount") or 0
        if not payment_channel_id:
            payment_channel_id = data.get("payment_channel_id")

        order_row = None
        if oid:
            c.execute("SELECT id, order_no, user_phone, deposit_amount, refund_status, status, refund_id, transaction_id, payment_channel_id FROM orders WHERE id=%s", (oid,))
            order_row = c.fetchone()
        if not order_row and ono:
            c.execute("SELECT id, order_no, user_phone, deposit_amount, refund_status, status, refund_id, transaction_id, payment_channel_id FROM orders WHERE order_no=%s", (ono,))
            order_row = c.fetchone()
        if not order_row:
            conn.close()
            return json_response(message='未找到可退款订单', code=400)

        oid = order_row["id"]
        ono = order_row["order_no"]
        deposit_amount = deposit_amount or order_row["deposit_amount"] or 0
        payment_channel_id = payment_channel_id or order_row["payment_channel_id"]
        _r_phone = order_row["user_phone"] or row.get("user_phone") or ""
        refund_status = order_row["refund_status"] or ""
        already_reply = '订单已退款，无需重复退款'

        if refund_status in ('success', 'refunded') or (order_row["status"] == 4 and order_row["refund_id"]):
            c.execute("UPDATE complaints SET status='2', reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s", (already_reply, complaint_id))
            conn.commit()
            conn.close()
            return json_response(message=already_reply)

        logger.info('[retry_refund] 准备退款 order_id=%s order_no=%s amount=%s ch=%s', oid, ono, deposit_amount, payment_channel_id)
        ok, rid, msg = do_real_refund(order_id=oid, order_no=ono, amount=deposit_amount, payment_channel_id=payment_channel_id)
        if ok:
            logger.info('[retry_refund] 退款成功 order_id=%s refund_id=%s', oid, rid)
            c.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=%s, refund_amount=%s, refund_time=CURRENT_TIMESTAMP, refund_mark=1 WHERE id=%s", (rid, deposit_amount, oid))
            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (oid,))
            c.execute("INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, CURRENT_TIMESTAMP)", (oid, deposit_amount, order_row["transaction_id"] or '', rid))
            c.execute("INSERT INTO withdrawal_records (order_id, user_phone, amount, status, approver, order_ids, approve_time) VALUES (%s, %s, %s, 2, '管理员-投诉退款', %s, NOW())", (oid, _r_phone, deposit_amount, '[' + str(oid) + ']'))
            if row.get('complaint_type') == 'self' or str(row.get('type', '')).lower() == 'self':
                c.execute("UPDATE complaints SET status='1', reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s", ('管理员已退款', complaint_id))
            else:
                c.execute("UPDATE complaints SET status='2', reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s", ('管理员已退款', complaint_id))
            conn.commit()
            conn.close()
            return json_response(message='退款成功')
        else:
            logger.warning('[retry_refund] 退款失败 order_id=%s msg=%s', oid, msg)
            conn.close()
            return json_response(message='退款失败: ' + str(msg), code=400)
    except Exception as e:
        logger.error(f'[retry_refund] {e}')
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
        if data.get('id'):
            fields = ['name','contact_name','contact_phone','status','commission_rate','permissions','dashboard_config']
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
            c.execute('INSERT INTO agents (name, contact_name, contact_phone, password_hash, commission_rate, plain_password, permissions, dashboard_config) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                      (data['name'], data.get('contact_name',''), data['contact_phone'], generate_password_hash(pwd), data.get('commission_rate', 0), pwd, data.get('permissions','[]'), data.get('dashboard_config','{}')))
        conn.commit()
        conn.close()
        resp_data = None
        if not data.get('id'):
            resp_data = {'password': pwd}
        elif data.get('password'):
            resp_data = {'password': data['password']}
        return json_response(data=resp_data, message='保存成功')
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
        c.execute(f'SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON o.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
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


# ============ Agent settlement (代理商分成结算) ============

AGENT_SETTLE_COMMISSION_KEY = 'agent_settle_commission_rate'
AGENT_SETTLE_FEE_KEY = 'agent_settle_fee_rate'


def _m2(x):
    from decimal import Decimal, ROUND_HALF_UP
    try:
        return float(Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except Exception:
        return round(float(x or 0), 2)


def _agent_settle_months(start_month, end_month):
    try:
        sy, sm = [int(v) for v in str(start_month).split('-')]
        ey, em = [int(v) for v in str(end_month).split('-')]
    except Exception:
        return []
    months = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append('%04d-%02d' % (y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return months


def _agent_settle_next_month(month):
    try:
        y, m = [int(v) for v in str(month).split('-')]
    except Exception:
        return None
    if m == 12:
        return '%04d-%02d' % (y + 1, 1)
    return '%04d-%02d' % (y, m + 1)


def _agent_settle_config(c):
    commission_rate = 5.0
    fee_rate = 0.6
    try:
        c.execute("SELECT setting_key, setting_value FROM system_settings WHERE setting_key IN (%s, %s)",
                  (AGENT_SETTLE_COMMISSION_KEY, AGENT_SETTLE_FEE_KEY))
        for k, v in c.fetchall():
            try:
                if k == AGENT_SETTLE_COMMISSION_KEY and v:
                    commission_rate = float(v)
                elif k == AGENT_SETTLE_FEE_KEY and v:
                    fee_rate = float(v)
            except Exception:
                pass
    except Exception:
        pass
    return commission_rate, fee_rate


def _agent_settle_calc_month(c, agent_id, month, commission_rate, fee_rate):
    start_dt = month + '-01'
    nm = _agent_settle_next_month(month)
    if not nm:
        return None
    end_dt = nm + '-01'
    c.execute('''
        SELECT COUNT(*) AS order_count,
               COALESCE(SUM(o.deposit_amount), 0) AS deposit_amount,
               COALESCE(SUM(CASE WHEN o.refund_time IS NOT NULL THEN o.refund_amount ELSE 0 END), 0) AS refund_amount,
               COALESCE(SUM(o.per_use_price), 0) AS per_use_amount
        FROM orders o
        JOIN cabinets cab ON o.cabinet_id = cab.id
        JOIN locations l ON cab.location_id = l.id
        JOIN merchants m ON l.merchant_id = m.id
        WHERE m.agent_id = %s AND o.status IN (2, 3, 4)
          AND o.created_at >= %s AND o.created_at < %s
    ''', (agent_id, start_dt, end_dt))
    row = c.fetchone()
    order_count = int(row['order_count'] or 0)
    deposit = _m2(row['deposit_amount'] or 0)
    refund = _m2(row['refund_amount'] or 0)
    balance = _m2(deposit - refund)
    fee = _m2(balance * fee_rate / 100.0)
    commission = _m2(deposit * commission_rate / 100.0)
    per_use = _m2(row['per_use_amount'] or 0)
    per_use_fee = _m2(per_use * fee_rate / 100.0)
    per_use_settle = _m2(per_use - per_use_fee)
    settle = _m2(balance - fee - commission + per_use_settle)
    c.execute('SELECT COALESCE(SUM(delta_amount), 0) AS h FROM agent_settlement_logs WHERE agent_id=%s AND settle_month=%s',
              (agent_id, month))
    settled_before = _m2(c.fetchone()['h'] or 0)
    delta = _m2(settle - settled_before)
    return {
        'order_count': order_count,
        'deposit_amount': deposit,
        'refund_amount': refund,
        'balance_amount': balance,
        'fee_amount': fee,
        'commission_amount': commission,
        'per_use_amount': per_use,
        'per_use_fee_amount': per_use_fee,
        'per_use_settle_amount': per_use_settle,
        'settle_amount': settle,
        'settled_before': settled_before,
        'delta_amount': delta,
    }


def _allocate_cents(target_cents, raw_values):
    if target_cents is None or target_cents <= 0:
        return [0.0] * len(raw_values)
    shares = [int(raw * 100) for raw in raw_values]
    rem = target_cents - sum(shares)
    if rem > 0:
        order = sorted(range(len(raw_values)),
                       key=lambda i: (raw_values[i] * 100 - shares[i], i), reverse=True)
        for k in range(rem):
            shares[order[k % len(order)]] += 1
    return [round(s / 100.0, 2) for s in shares]


def _agent_settle_calc_locations(c, agent_id, month, agent_calc, commission_rate, fee_rate):
    start_dt = month + '-01'
    nm = _agent_settle_next_month(month)
    if not nm:
        return []
    end_dt = nm + '-01'
    c.execute('''
        SELECT l.id AS location_id, l.name AS location_name,
               COUNT(o.id) AS order_count,
               COALESCE(SUM(o.deposit_amount), 0) AS deposit_amount,
               COALESCE(SUM(CASE WHEN o.refund_time IS NOT NULL THEN o.refund_amount ELSE 0 END), 0) AS refund_amount,
               COALESCE(SUM(o.per_use_price), 0) AS per_use_amount
        FROM orders o
        JOIN cabinets cab ON o.cabinet_id = cab.id
        JOIN locations l ON cab.location_id = l.id
        JOIN merchants m ON l.merchant_id = m.id
        WHERE m.agent_id = %s AND o.status IN (2, 3, 4)
          AND o.created_at >= %s AND o.created_at < %s
        GROUP BY l.id, l.name
        ORDER BY l.id
    ''', (agent_id, start_dt, end_dt))
    rows = c.fetchall()
    if not rows:
        return []
    fee_cents = int(round(agent_calc['fee_amount'] * 100))
    comm_cents = int(round(agent_calc['commission_amount'] * 100))
    per_use_fee_cents = int(round(agent_calc['per_use_fee_amount'] * 100))
    raw_fees = []
    raw_comms = []
    raw_per_use_fees = []
    for r in rows:
        dep = float(r['deposit_amount'] or 0)
        ref = float(r['refund_amount'] or 0)
        bal = dep - ref
        raw_fees.append(bal * fee_rate / 100.0)
        raw_comms.append(dep * commission_rate / 100.0)
        raw_per_use_fees.append(float(r['per_use_amount'] or 0) * fee_rate / 100.0)
    fee_shares = _allocate_cents(fee_cents, raw_fees)
    comm_shares = _allocate_cents(comm_cents, raw_comms)
    per_use_fee_shares = _allocate_cents(per_use_fee_cents, raw_per_use_fees)
    out = []
    for i, r in enumerate(rows):
        dep = _m2(r['deposit_amount'] or 0)
        ref = _m2(r['refund_amount'] or 0)
        bal = _m2(dep - ref)
        fee = fee_shares[i]
        comm = comm_shares[i]
        per_use = _m2(r['per_use_amount'] or 0)
        per_use_fee = per_use_fee_shares[i]
        per_use_settle = _m2(per_use - per_use_fee)
        settle = _m2(bal - fee - comm + per_use_settle)
        out.append({
            'location_id': r['location_id'],
            'location_name': r['location_name'],
            'order_count': int(r['order_count'] or 0),
            'deposit_amount': dep,
            'refund_amount': ref,
            'balance_amount': bal,
            'fee_amount': fee,
            'commission_amount': comm,
            'per_use_amount': per_use,
            'per_use_fee_amount': per_use_fee,
            'per_use_settle_amount': per_use_settle,
            'settle_amount': settle,
        })
    return out


def _agent_settle_agents(c, agent_id):
    if agent_id:
        c.execute('SELECT id, name FROM agents WHERE id=%s', (agent_id,))
    else:
        c.execute('SELECT id, name FROM agents WHERE status=1 ORDER BY id')
    return [dict(r) for r in c.fetchall()]


@bp.route('/admin/agent-settlement/preview', methods=['GET'])
@require_auth
def admin_agent_settlement_preview():
    try:
        agent_id = request.args.get('agent_id', type=int)
        start_month = request.args.get('start_month', '')
        end_month = request.args.get('end_month', '')
        if not start_month:
            start_month = end_month
        if not end_month:
            end_month = start_month
        if not start_month or not end_month:
            today = datetime.now()
            y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
            start_month = end_month = '%04d-%02d' % (y, m)
        months = _agent_settle_months(start_month, end_month)
        if not months:
            return json_response(message='月份格式错误，应为YYYY-MM', code=400)
        conn = get_db()
        c = conn.cursor()
        commission_rate, fee_rate = _agent_settle_config(c)
        agents = _agent_settle_agents(c, agent_id)
        rows = []
        for agent in agents:
            for month in months:
                calc = _agent_settle_calc_month(c, agent['id'], month, commission_rate, fee_rate)
                if not calc:
                    continue
                if calc['order_count'] == 0 and calc['settled_before'] == 0 and calc['delta_amount'] == 0:
                    continue
                calc['locations'] = _agent_settle_calc_locations(
                    c, agent['id'], month, calc, commission_rate, fee_rate)
                calc.update({'agent_id': agent['id'], 'agent_name': agent['name'], 'settle_month': month})
                rows.append(calc)
        carry_map = {}
        if agent_id:
            c.execute('SELECT agent_id, COALESCE(carry_amount, 0) AS carry FROM agent_settlement_carry WHERE agent_id=%s',
                      (agent_id,))
        else:
            c.execute('SELECT agent_id, COALESCE(carry_amount, 0) AS carry FROM agent_settlement_carry')
        for r in c.fetchall():
            carry_map[r['agent_id']] = _m2(r['carry'] or 0)
        summary = {
            'order_count': sum(r['order_count'] for r in rows),
            'deposit_amount': _m2(sum(r['deposit_amount'] for r in rows)),
            'refund_amount': _m2(sum(r['refund_amount'] for r in rows)),
            'balance_amount': _m2(sum(r['balance_amount'] for r in rows)),
            'fee_amount': _m2(sum(r['fee_amount'] for r in rows)),
            'commission_amount': _m2(sum(r['commission_amount'] for r in rows)),
            'per_use_amount': _m2(sum(r['per_use_amount'] for r in rows)),
            'per_use_fee_amount': _m2(sum(r['per_use_fee_amount'] for r in rows)),
            'per_use_settle_amount': _m2(sum(r['per_use_settle_amount'] for r in rows)),
            'settle_amount': _m2(sum(r['settle_amount'] for r in rows)),
            'settled_before': _m2(sum(r['settled_before'] for r in rows)),
            'delta_amount': _m2(sum(r['delta_amount'] for r in rows)),
            'agents': []
        }
        for agent in agents:
            agent_rows = [r for r in rows if r['agent_id'] == agent['id']]
            delta_sum = _m2(sum(r['delta_amount'] for r in agent_rows))
            carry = carry_map.get(agent['id'], 0)
            net = _m2(carry + delta_sum)
            summary['agents'].append({
                'agent_id': agent['id'],
                'agent_name': agent['name'],
                'delta_sum': delta_sum,
                'per_use_amount': _m2(sum(r['per_use_amount'] for r in agent_rows)),
                'per_use_fee_amount': _m2(sum(r['per_use_fee_amount'] for r in agent_rows)),
                'per_use_settle_amount': _m2(sum(r['per_use_settle_amount'] for r in agent_rows)),
                'carry': carry,
                'net': net,
                'payable': _m2(max(0, net))
            })
        summary['carry_total'] = _m2(sum(a['carry'] for a in summary['agents']))
        conn.close()
        return json_response(data={
            'rows': rows,
            'summary': summary,
            'rates': {'commission_rate': commission_rate, 'fee_rate': fee_rate},
            'months': months
        })
    except Exception as e:
        logger.error('[agent_settlement_preview] %s', e)
        return json_response(message=str(e), code=500)


@bp.route('/admin/agent-settlement/confirm', methods=['POST'])
@require_auth
def admin_agent_settlement_confirm():
    conn = None
    try:
        data = request.get_json() or {}
        start_month = str(data.get('start_month') or '')
        end_month = str(data.get('end_month') or '')
        if not start_month:
            start_month = end_month
        if not end_month:
            end_month = start_month
        agent_id = data.get('agent_id', 0) or 0
        note = str(data.get('note') or '')
        months = _agent_settle_months(start_month, end_month)
        if not months:
            return json_response(message='请选择结算月份', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT pg_try_advisory_xact_lock(2026081301)')
        if not c.fetchone()[0]:
            conn.close()
            return json_response(message='有其他结算正在进行，请稍后再试', code=400)
        commission_rate, fee_rate = _agent_settle_config(c)
        agents = _agent_settle_agents(c, agent_id)
        if not agents:
            conn.close()
            return json_response(message='没有可结算的代理商', code=400)
        username = session.get('admin_username', 'admin')
        c.execute("INSERT INTO agent_settlement_batches (settle_date, status, created_by, note, confirmed_at) "
                  "VALUES (CURRENT_DATE, 'confirmed', %s, %s, NOW()) RETURNING id", (username, note))
        batch_id = c.fetchone()['id']
        agent_delta = {}
        for agent in agents:
            for month in months:
                calc = _agent_settle_calc_month(c, agent['id'], month, commission_rate, fee_rate)
                if not calc:
                    continue
                if calc['order_count'] == 0 and calc['settled_before'] == 0 and calc['delta_amount'] == 0:
                    continue
                locations = _agent_settle_calc_locations(
                    c, agent['id'], month, calc, commission_rate, fee_rate)
                c.execute('''
                    INSERT INTO agent_settlement_logs
                    (batch_id, agent_id, agent_name, settle_month, order_count, deposit_amount, refund_amount,
                     balance_amount, fee_amount, commission_amount, per_use_amount, per_use_fee_amount,
                     per_use_settle_amount, settle_amount, settled_before, delta_amount, location_detail)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ''', (batch_id, agent['id'], agent['name'], month, calc['order_count'], calc['deposit_amount'],
                      calc['refund_amount'], calc['balance_amount'], calc['fee_amount'], calc['commission_amount'],
                      calc['per_use_amount'], calc['per_use_fee_amount'], calc['per_use_settle_amount'],
                      calc['settle_amount'], calc['settled_before'], calc['delta_amount'],
                      json.dumps(locations, ensure_ascii=False)))
                agent_delta[agent['id']] = _m2(agent_delta.get(agent['id'], 0) + calc['delta_amount'])
        total_payable = 0.0
        for aid, delta_sum in agent_delta.items():
            delta_sum = _m2(delta_sum)
            c.execute('SELECT agent_id, COALESCE(carry_amount, 0) AS carry, name FROM agent_settlement_carry '
                      'LEFT JOIN agents ON agents.id=agent_settlement_carry.agent_id WHERE agent_id=%s FOR UPDATE',
                      (aid,))
            row = c.fetchone()
            carry_before = _m2(row['carry'] or 0) if row else 0
            agent_name = row['name'] if row else ''
            if not agent_name:
                c.execute('SELECT name FROM agents WHERE id=%s', (aid,))
                nr = c.fetchone()
                agent_name = nr['name'] if nr else ''
            net = _m2(carry_before + delta_sum)
            payable = _m2(max(0, net))
            carry_after = _m2(min(0, net))
            total_payable = _m2(total_payable + payable)
            c.execute('''
                INSERT INTO agent_settlement_payments
                (batch_id, agent_id, agent_name, delta_sum, carry_before, payable, carry_after)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            ''', (batch_id, aid, agent_name, delta_sum, carry_before, payable, carry_after))
            c.execute('INSERT INTO agent_settlement_carry (agent_id, carry_amount, updated_at) '
                      'VALUES (%s,%s,NOW()) ON CONFLICT (agent_id) DO UPDATE '
                      'SET carry_amount=EXCLUDED.carry_amount, updated_at=NOW()', (aid, carry_after))
        conn.commit()
        conn.close()
        return json_response(data={'batch_id': batch_id, 'total_payable': total_payable},
                             message='结算已确认落账')
    except Exception as e:
        logger.error('[agent_settlement_confirm] %s', e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return json_response(message=str(e), code=500)


@bp.route('/admin/agent-settlement/history', methods=['GET'])
@require_auth
def admin_agent_settlement_history():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            SELECT b.id, b.settle_date, b.created_by, b.note, b.created_at, b.confirmed_at,
                   MIN(l.settle_month) AS start_month, MAX(l.settle_month) AS end_month,
                   COUNT(DISTINCT l.agent_id) AS agent_count,
                   COALESCE(SUM(l.delta_amount), 0) AS total_delta,
                   COALESCE(SUM(p.payable), 0) AS total_payable
            FROM agent_settlement_batches b
            LEFT JOIN agent_settlement_logs l ON l.batch_id = b.id
            LEFT JOIN agent_settlement_payments p ON p.batch_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC LIMIT 100
        ''')
        rows = [dict(r) for r in c.fetchall()]
        for r in rows:
            r['total_delta'] = _m2(r['total_delta'] or 0)
            r['total_payable'] = _m2(r['total_payable'] or 0)
        conn.close()
        return json_response(data=rows)
    except Exception as e:
        logger.error('[agent_settlement_history] %s', e)
        return json_response(data=[])


@bp.route('/admin/agent-settlement/history/detail', methods=['GET'])
@require_auth
def admin_agent_settlement_history_detail():
    try:
        batch_id = request.args.get('batch_id', type=int)
        if not batch_id:
            return json_response(message='missing batch_id', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM agent_settlement_batches WHERE id=%s', (batch_id,))
        batch = c.fetchone()
        if not batch:
            conn.close()
            return json_response(message='batch not found', code=404)
        c.execute('SELECT * FROM agent_settlement_logs WHERE batch_id=%s ORDER BY settle_month, agent_id', (batch_id,))
        logs = [dict(r) for r in c.fetchall()]
        for log in logs:
            try:
                log['locations'] = json.loads(log.get('location_detail') or '[]')
            except Exception:
                log['locations'] = []
        c.execute('SELECT * FROM agent_settlement_payments WHERE batch_id=%s ORDER BY agent_id', (batch_id,))
        payments = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'batch': dict(batch), 'logs': logs, 'payments': payments})
    except Exception as e:
        logger.error('[agent_settlement_history_detail] %s', e)
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
            0 as order_count,
            0.0 as total_revenue
            FROM merchants m LEFT JOIN agents a ON m.agent_id=a.id
            WHERE {where} ORDER BY m.created_at DESC LIMIT %s OFFSET %s''',
                  params + [page_size, (page-1)*page_size])
        merchants = [dict(r) for r in c.fetchall()]
        if merchants:
            merchant_ids = [m['id'] for m in merchants]
            placeholders = ','.join(['%s'] * len(merchant_ids))
            c.execute(f'''SELECT o.id, o.user_phone, o.logic_mark, o.deposit_amount,
                CASE WHEN o.status=4 THEN COALESCE(o.refund_amount,0) ELSE 0 END as refund_amount,
                o.auto_hidden,
                l.merchant_id, l.hide_ratio, l.whitelist_phones
                FROM orders o
                LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
                LEFT JOIN locations l ON cab.location_id=l.id
                WHERE l.merchant_id IN ({placeholders}) AND o.status NOT IN (5)
                  AND (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)''', merchant_ids)
            counts = {}
            revenues = {}
            for r in c.fetchall():
                if r['logic_mark'] == 'Y':
                    continue
                if r['logic_mark'] != 'N' and (r.get('auto_hidden') or 0) == 1:
                    continue
                counts[r['merchant_id']] = counts.get(r['merchant_id'], 0) + 1
                revenues[r['merchant_id']] = revenues.get(r['merchant_id'], 0.0) + float(r['deposit_amount'] or 0)
            for m in merchants:
                m['order_count'] = counts.get(m['id'], 0)
                m['total_revenue'] = round(revenues.get(m['id'], 0.0), 2)
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
        agent_id = (data or {}).get('agent_id', '') or request.args.get('agent_id', '')
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
        if agent_id:
            where += ' AND e.agent_id=%s'
            params.append(agent_id)
        c.execute(f'SELECT COUNT(*) FROM employees e WHERE {where}', params)
        total = c.fetchone()[0]
        c.execute(f'''SELECT e.*, e.is_locked, m.name as merchant_name, a.name as agent_name
            FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id LEFT JOIN agents a ON e.agent_id=a.id
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
        if data.get('id'):
            fields = ['name','phone','role','merchant_id','agent_id','status','permissions']
            sets, params = [], []
            for f in fields:
                if f in data:
                    sets.append(f'{f}=%s')
                    val = data[f]; params.append(None if val == "" and f in ("merchant_id", "agent_id") else val)
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
            c.execute('INSERT INTO employees (merchant_id, agent_id, name, phone, password_hash, role, permissions, plain_password) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                      (data.get("merchant_id") or None, data.get("agent_id") or None, data["name"], data['phone'], generate_password_hash(pwd), data.get('role','staff'), data.get('permissions','[]'), pwd))
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
        c.execute("SELECT version_name, version_code, download_url, COALESCE(file_md5, '') as file_md5, COALESCE(update_desc, '') as update_desc FROM apk_version ORDER BY version_code DESC LIMIT 1")
        apk = c.fetchone()
        if not apk:
            conn.close()
            return json_response(message="没有找到APK版本信息", code=400)
        cmd = {"type":"force_update","download_url":apk["download_url"],"version_name":apk["version_name"],"version_code":apk["version_code"],"update_desc":apk["update_desc"],"force":True,"file_md5":apk["file_md5"]}
        c.execute("""
            SELECT c.id as cabinet_id, c.mainboard_device_id as device_id
            FROM cabinets c
            WHERE c.mainboard_device_id IS NOT NULL
              AND c.mainboard_device_id != ''
              AND c.last_heartbeat >= NOW() - INTERVAL '120 seconds'
              AND (c.app_version_code IS NULL OR c.app_version_code < %s)
        """, (apk["version_code"],))
        targets = c.fetchall()
        pushed = 0
        for row in targets:
            c.execute("SELECT 1 FROM pending_lock_cmds p WHERE p.device_id=%s AND (p.delivered=0 OR p.status='pending') AND strpos(p.command,'force_update')>0 AND p.created_at > NOW() - INTERVAL '10 minutes' LIMIT 1", (row["device_id"],))
            if c.fetchone():
                continue
            supersede_force_update_cmds(c, row["device_id"])
            c.execute("INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, delivered, status) VALUES (%s, %s, %s, 0, 'pending')",
                      (row["device_id"], row["cabinet_id"], json.dumps(cmd)))
            pushed += 1
        c.execute("SELECT COUNT(*) FROM cabinets WHERE mainboard_device_id IS NOT NULL AND mainboard_device_id != '' AND last_heartbeat >= NOW() - INTERVAL '120 seconds'")
        online_total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cabinets WHERE mainboard_device_id IS NOT NULL AND mainboard_device_id != '' AND (last_heartbeat IS NULL OR last_heartbeat < NOW() - INTERVAL '120 seconds')")
        offline_total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cabinets WHERE mainboard_device_id IS NOT NULL AND mainboard_device_id != '' AND app_version_code IS NOT NULL AND app_version_code >= %s", (apk["version_code"],))
        latest_total = c.fetchone()[0]
        conn.commit()
        conn.close()
        message = f"已向{pushed}台在线且非最新设备推送更新"
        if pushed == 0:
            message = "没有需要更新的在线设备"
        logger.info('[APK推送] push-all result: pushed=%s online=%s offline=%s latest=%s', pushed, online_total, offline_total, latest_total)
        return json_response(data={"pushed": pushed, "online_count": online_total, "offline_count": offline_total, "already_latest_count": latest_total}, message=message)
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
            FROM orders o{where_clause}''', params)
        summary = dict(c.fetchone())
        summary['net_income'] = float(summary.get('deposit_total',0)) - float(summary.get('refund_total',0))
        # Location stats - join through cabinets since orders have no location_id
        c.execute('''SELECT l.name as location_name, m.name as merchant_name,
            COUNT(o.id) as order_count,
            SUM(CASE WHEN o.status=2 THEN 1 ELSE 0 END) as active_count,
            COALESCE(SUM(o.deposit_amount),0) as deposit_total,
            COALESCE(SUM(CASE WHEN o.status=4 THEN o.refund_amount ELSE 0 END),0) as refund_total
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
            where_parts.append('l.merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)')
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
        
        # 订单汇总统计（含隐藏订单统计）- 按使用日(created_at)口径
        c.execute(f'''SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN (o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0) THEN 1 END) as visible_count,
            SUM(CASE WHEN o.status=2 THEN 1 ELSE 0 END) as active_count,
            COALESCE(SUM(o.deposit_amount),0) as deposit_total,
            COALESCE(SUM(CASE WHEN o.refund_time IS NOT NULL THEN o.refund_amount ELSE 0 END),0) as refund_total,
            COUNT(DISTINCT o.user_phone) as user_count
            FROM orders o
            LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
            LEFT JOIN locations l ON cab.location_id=l.id
            {where_clause}''', params)
        row = c.fetchone()
        # 退款实时金额: 独立按退款日(refund_time)统计, 不限使用日(筛选条件同网点/代理商/省份, 时间按refund_time)
        refund_where = ["o.status NOT IN (5)", "o.refund_time IS NOT NULL"]
        refund_params = []
        if location_id:
            refund_where.append('cab.location_id=%s')
            refund_params.append(location_id)
        if agent_id:
            refund_where.append('l.merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)')
            refund_params.append(agent_id)
        if province:
            refund_where.append('l.province=%s')
            refund_params.append(province)
        if start_date and end_date:
            refund_where.append('date(o.refund_time)>=%s AND date(o.refund_time)<=%s')
            refund_params.extend([start_date, end_date])
        refund_where_sql = ' WHERE ' + ' AND '.join(refund_where)
        c.execute(f'''SELECT COALESCE(SUM(o.refund_amount),0)
            FROM orders o
            LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
            LEFT JOIN locations l ON cab.location_id=l.id
            {refund_where_sql}''', refund_params)
        refund_today_total = float(c.fetchone()[0] or 0)
        orderStats = {
            'total': row[0] if row else 0,
            'visible_count': row[1] if row else 0,
            'active_count': row[2] if row else 0,
            'deposit_total': round(float(row[3] if row and row[3] else 0), 2),
            'refund_total': round(float(row[4] if row and row[4] else 0), 2),
            'refund_today_total': round(refund_today_total, 2),
            'user_count': row[5] if row else 0,
            'net_income': round(float(row[3] if row and row[3] else 0) - float(row[4] if row and row[4] else 0), 2)
        }
        

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
                o.auto_hidden,
                o.deposit_amount,
                CASE WHEN o.refund_time IS NOT NULL THEN o.refund_amount ELSE 0 END as refund_amount,
                date(o.refund_time) as refund_date,
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
                o.auto_hidden,
                o.deposit_amount,
                CASE WHEN o.refund_time IS NOT NULL THEN o.refund_amount ELSE 0 END as refund_amount,
                date(o.refund_time) as refund_date,
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
        grouped = defaultdict(lambda: {'order_count': 0, 'visible_count': 0, 'deposit_total': 0, 'refund_total': 0, 'user_phones': set(), 'location_name': ''})
        
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
            if user_phone:
                grouped[key]['user_phones'].add(user_phone)
            if loc_name:
                grouped[key]['location_name'] = loc_name
            
            # 计算visible_count
            is_hidden = False
            if logic_mark == 'Y':
                is_hidden = True
            elif logic_mark != 'N' and (row.get('auto_hidden') or 0) == 1:
                is_hidden = True
            
            if not is_hidden:
                grouped[key]['visible_count'] += 1
        
        # 退款实时明细: 独立按退款日(refund_time)统计, 不限使用日; 支持网点/代理商/省份/退款日期筛选
        refund_today_details = []
        _rtd_where = ["o.status NOT IN (5)", "o.refund_time IS NOT NULL"]
        _rtd_params = []
        if location_id:
            _rtd_where.append('cab.location_id=%s')
            _rtd_params.append(location_id)
        if agent_id:
            _rtd_where.append('l.merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)')
            _rtd_params.append(agent_id)
        if province:
            _rtd_where.append('l.province=%s')
            _rtd_params.append(province)
        if start_date and end_date:
            _rtd_where.append('date(o.refund_time)>=%s AND date(o.refund_time)<=%s')
            _rtd_params.extend([start_date, end_date])
        _rtd_where_sql = ' WHERE ' + ' AND '.join(_rtd_where)
        _rtd_group_by = 'l.id, l.name, date(o.refund_time)' if location_id else 'date(o.refund_time)'
        _rtd_select = ('date(o.refund_time) as refund_date, l.id as location_id, l.name as location_name,'
                       ' COALESCE(SUM(o.refund_amount),0) as refund_today_total, COUNT(DISTINCT o.user_phone) as user_count'
                       if location_id else
                       'date(o.refund_time) as refund_date, COALESCE(SUM(o.refund_amount),0) as refund_today_total,'
                       ' COUNT(DISTINCT o.user_phone) as user_count')
        try:
            c.execute(f'''SELECT {_rtd_select}
                FROM orders o
                LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
                LEFT JOIN locations l ON cab.location_id=l.id
                {_rtd_where_sql}
                GROUP BY {_rtd_group_by}
                ORDER BY refund_date DESC''', _rtd_params)
            for r in c.fetchall():
                d = dict(r)
                d['refund_date'] = str(d.get('refund_date') or '')
                d['refund_today_total'] = round(float(d.get('refund_today_total') or 0), 2)
                d['user_count'] = d.get('user_count') or 0
                refund_today_details.append(d)
        except Exception as e:
            logger.error(f'[biz_stats_refund_today] {e}')
            refund_today_details = []

        # 按日期建退款实时金额索引: (日期[, 网点id]) -> 金额, 用于合并进使用日明细
        _rtd_map = {}
        for _rd in refund_today_details:
            if location_id:
                _rtd_map[(_rd.get('refund_date'), _rd.get('location_id'))] = _rd.get('refund_today_total', 0)
            else:
                _rtd_map[_rd.get('refund_date')] = _rd.get('refund_today_total', 0)

        # 转换为列表
        for (stat_date, loc_id), data in grouped.items():
            if has_location_filter:
                _rt_amt = _rtd_map.get((stat_date, loc_id), 0)
                location_details.append({
                    'location_id': loc_id,
                    'location_name': data['location_name'],
                    'stat_date': stat_date,
                    'order_count': data['order_count'],
                    'visible_count': data['visible_count'],
                    'deposit_total': round(data['deposit_total'], 2),
                    'refund_total': round(data['refund_total'], 2),
                    'refund_today_total': round(_rt_amt, 2),
                    'user_count': len(data['user_phones']),
                    'balance': round(data['deposit_total'] - data['refund_total'], 2)
                })
            else:
                _rt_amt = _rtd_map.get(stat_date, 0)
                location_details.append({
                    'stat_date': stat_date,
                    'order_count': data['order_count'],
                    'visible_count': data['visible_count'],
                    'deposit_total': round(data['deposit_total'], 2),
                    'refund_total': round(data['refund_total'], 2),
                    'refund_today_total': round(_rt_amt, 2),
                    'user_count': len(data['user_phones']),
                    'balance': round(data['deposit_total'] - data['refund_total'], 2)
                })
        
        # 按天聚合趋势
        daily = []
        if start_date and end_date:
            day_count = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
            for i in range(day_count):
                date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
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
                'deposit_total': round(float(row[1] if row and row[1] else 0), 2),
                'refund_total': round(float(row[2] if row and row[2] else 0), 2)
                })
        else:
            day_count = 30
            for i in range(day_count):
                date = (datetime.now() - timedelta(days=day_count-1-i)).strftime("%Y-%m-%d")
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
                'deposit_total': round(float(row[1] if row and row[1] else 0), 2),
                'refund_total': round(float(row[2] if row and row[2] else 0), 2)
                })
        
        conn.close()
        return json_response(data={
            'orderStats': orderStats,
            'locationDetails': location_details,
            'refundTodayDetails': refund_today_details,
            'daily': daily
        })
    except Exception as e:
        logger.error(f'[biz_stats] {e}')
        return json_response(message=str(e), code=500)


# ============ Channels ============

@bp.route('/admin/channels/balance', methods=['GET'])
@require_auth
def admin_channels_balance():
    """返回各微信商户号最近一次资金账单日终余额（昨日余额，只读）"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT pc.mch_id,
                   b.balance,
                   b.balance_date
            FROM payment_channels pc
            LEFT JOIN LATERAL (
                SELECT pcb.balance, pcb.balance_date
                FROM payment_channel_balance pcb
                WHERE pcb.mch_id = pc.mch_id
                ORDER BY pcb.balance_date DESC, pcb.id DESC
                LIMIT 1
            ) b ON true
            WHERE pc.channel_type='wechat' AND pc.mch_id IS NOT NULL AND pc.mch_id != ''
            ORDER BY pc.id
        """)
        result = []
        for r in c.fetchall():
            result.append({
                'mch_id': str(r['mch_id']),
                'balance': float(r['balance']) if r['balance'] is not None else None,
                'balance_time': str(r['balance_date']) if r['balance_date'] else None,
                'balance_error': None if r['balance'] is not None else '暂无账单',
            })
        conn.close()
        logger.info('[channels_balance] done, channels=%s', len(result))
        return json_response(data=result)
    except Exception as e:
        logger.error(f'[channels_balance] {e}')
        return json_response(data=[], code=500)


@bp.route('/admin/channels', methods=['GET', 'POST'])
@require_auth
def admin_channels():
    try:
        conn = get_db()
        c = conn.cursor()
        mch_filter = request.args.get('mch_id', '') or (request.get_json(silent=True) or {}).get('mch_id', '')
        mch_where = ''
        mch_params = []
        if mch_filter:
            mch_where = ' WHERE pc.mch_id LIKE %s'
            mch_params.append(f'%{mch_filter}%')
        c.execute("""
            CREATE TABLE IF NOT EXISTS wechat_trade_bills (
                id BIGSERIAL PRIMARY KEY,
                mch_id VARCHAR(32) NOT NULL,
                bill_date DATE NOT NULL,
                trade_time VARCHAR(32),
                app_id VARCHAR(64),
                wx_order_no VARCHAR(64),
                out_trade_no VARCHAR(64),
                user_id VARCHAR(64),
                trade_type VARCHAR(32),
                trade_state VARCHAR(32),
                bank_type VARCHAR(32),
                currency VARCHAR(16),
                settled_amount NUMERIC(12,2) DEFAULT 0,
                coupon_amount NUMERIC(12,2) DEFAULT 0,
                wx_refund_no VARCHAR(64),
                out_refund_no VARCHAR(64),
                refund_amount NUMERIC(12,2) DEFAULT 0,
                recharge_refund_amount NUMERIC(12,2) DEFAULT 0,
                refund_type VARCHAR(32),
                refund_state VARCHAR(32),
                product_name TEXT,
                merchant_data TEXT,
                fee NUMERIC(12,4) DEFAULT 0,
                fee_rate VARCHAR(16),
                order_amount NUMERIC(12,2) DEFAULT 0,
                apply_refund_amount NUMERIC(12,2) DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wechat_trade_bills
                ON wechat_trade_bills (mch_id, bill_date, out_trade_no, COALESCE(out_refund_no, ''))
        """)
        c.execute(f"""
            SELECT pc.*,
                   COALESCE(oi.paid_count, 0) as paid_total_count,
                   COALESCE(oi.paid_amount, 0) as paid_total_amount,
                   COALESCE(ri.refund_count, 0) as refund_total_count,
                   COALESCE(ri.refund_amount, 0) as refund_total_amount,
                   COALESCE(bi.bill_paid_count, 0) as bill_paid_count,
                   COALESCE(bi.bill_paid_amount, 0) as bill_paid_amount,
                   COALESCE(bi.bill_refund_count, 0) as bill_refund_count,
                   COALESCE(bi.bill_refund_amount, 0) as bill_refund_amount,
                   bi.bill_synced_until,
                   COALESCE(pcb.balance, 0) as yesterday_balance
            FROM payment_channels pc
            LEFT JOIN (SELECT payment_channel_id, COUNT(*) as paid_count, COALESCE(SUM(deposit_amount), 0) as paid_amount
                       FROM orders WHERE status IN (2,3,4) GROUP BY payment_channel_id) oi
                   ON pc.id = oi.payment_channel_id
            LEFT JOIN (SELECT payment_channel_id, COUNT(*) as refund_count, COALESCE(SUM(refund_amount), 0) as refund_amount
                       FROM orders WHERE refund_time IS NOT NULL AND COALESCE(refund_amount,0) > 0 GROUP BY payment_channel_id) ri
                   ON pc.id = ri.payment_channel_id
            LEFT JOIN (
                SELECT mch_id,
                       COUNT(*) FILTER (WHERE trade_state='SUCCESS') as bill_paid_count,
                       COALESCE(SUM(settled_amount) FILTER (WHERE trade_state='SUCCESS'),0) as bill_paid_amount,
                       COUNT(*) FILTER (WHERE out_refund_no IS NOT NULL AND out_refund_no != '' AND out_refund_no != '0' AND refund_state='SUCCESS') as bill_refund_count,
                       COALESCE(SUM(refund_amount) FILTER (WHERE out_refund_no IS NOT NULL AND out_refund_no != '' AND out_refund_no != '0' AND refund_state='SUCCESS'),0) as bill_refund_amount,
                       MAX(bill_date) as bill_synced_until
                FROM wechat_trade_bills GROUP BY mch_id
            ) bi ON bi.mch_id = pc.mch_id
            LEFT JOIN (
                SELECT pcb.mch_id, pcb.balance
                FROM payment_channel_balance pcb
                INNER JOIN (
                    SELECT mch_id, MAX(balance_date) as max_date
                    FROM payment_channel_balance
                    WHERE account_type = 'BASIC'
                    GROUP BY mch_id
                ) m ON pcb.mch_id = m.mch_id AND pcb.balance_date = m.max_date
                WHERE pcb.account_type = 'BASIC'
            ) pcb ON pcb.mch_id = pc.mch_id
            {mch_where}
            ORDER BY pc.created_at DESC
        """, mch_params)
        channels = [dict(r) for r in c.fetchall()]
        for ch in channels:
            # 仅当对账单确有交易数据且金额接近订单口径(>=90%)时才用对账单，否则回退订单表统计(修复8-16对账单统计后数字变0/偏小)
            _bill_ok = (ch.get('bill_paid_count') or 0) > 0 and float(ch.get('bill_paid_amount') or 0) >= float(ch.get('paid_total_amount') or 0) * 0.9
            if ch.get('bill_synced_until') is not None and _bill_ok:
                ch['paid_total_count'] = ch['bill_paid_count']
                ch['paid_total_amount'] = ch['bill_paid_amount']
                ch['refund_total_count'] = ch['bill_refund_count']
                ch['refund_total_amount'] = ch['bill_refund_amount']
            ch['total_count'] = ch.get('paid_total_count', 0)
            ch['total_amount'] = ch.get('paid_total_amount', 0)
            ch['refund_total_count'] = ch.get('refund_total_count', 0)
            ch['refund_total_amount'] = ch.get('refund_total_amount', 0)
        conn.close()
        return json_response(data=channels)
    except Exception as e:
        logger.error(f'[channels] {e}')
        return json_response(data=[])


@bp.route('/admin/channels/sync-bills', methods=['POST'])
@require_auth
def admin_channels_sync_bills():
    try:
        data = request.get_json() or {}
        mch_id = str(data.get('mch_id') or '')
        days = int(data.get('days') or 3)
        import pull_trade_bills as _tb
        conn = _tb._connect()
        with conn.cursor() as cur:
            cur.execute(_tb.CREATE_TABLE_SQL)
        conn.commit()
        channels = _tb.channels_with_cert(conn)
        if mch_id:
            channels = [ch for ch in channels if ch['mch_id'] == mch_id]
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        results = []
        for ch in channels:
            results.append(_tb.sync_mch(conn, ch['mch_id'], ch['cert_serial_no'], ch['cert_name'], start_date, end_date))
        conn.close()
        return json_response(data={'results': results, 'message': '同步完成'})
    except Exception as e:
        logger.error(f'[channels_sync_bills] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/channel/save', methods=['POST'])
@require_auth
def admin_channel_save():
    try:
        data = request.get_json()
        conn = get_db()
        c = conn.cursor()
        if data.get('id'):
            fields = ['name','channel_type','app_id','mch_id','api_key','app_secret','cert_name','cert_serial_no','is_active','rotation_index']
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
                c.execute('SELECT id FROM payment_channels WHERE mch_id=%s AND is_active=1', (mch_id,))
                if c.fetchone():
                    conn.close()
                    return json_response(message=f'商户号 {mch_id} 已存在，请勿重复添加', code=400)
            # Auto extract cert serial from file
            cert_serial = ''
            cert_name_file = data.get('cert_name', '')
            if cert_name_file:
                import os
                pem = f'/home/ubuntu/smart-locker/cert/{cert_name_file}_cert.pem'
                if os.path.exists(pem):
                    import subprocess
                    r = subprocess.run(['openssl', 'x509', '-in', pem, '-noout', '-serial'], capture_output=True, text=True)
                    if r.returncode == 0:
                        cert_serial = r.stdout.strip().replace('serial=', '')
            c.execute('''INSERT INTO payment_channels (name,channel_type,app_id,mch_id,api_key,app_secret,cert_name,cert_serial_no,is_active,rotation_index) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                      (data.get('name'), data.get('channel_type'), data.get('app_id') or WX_APP_ID,
                       data.get('mch_id'), data.get('api_key'), data.get('app_secret') or WX_APP_SECRET, data.get('cert_name'), cert_serial, data.get('status',1), data.get('rotation_index', 0)))
        conn.commit()
        conn.close()
        return json_response(message='保存成功')
    except Exception as e:
        logger.error(f'[channel_save] {e}')
        return json_response(message=str(e), code=500)


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
                    c.execute("UPDATE payment_channels SET cert_serial_no=%s WHERE mch_id=%s AND is_active=1", (serial, mch_id))
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
            'total_deposit': round(float(row['total_deposit']), 2),
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
        if action == 'approve':
            c.execute("UPDATE withdrawal_records SET status=1, approve_time=NOW(), approver=%s WHERE id=%s",
                      (session.get('admin_user', 'admin'), wid))
        elif action == 'reject':
            c.execute("SELECT user_phone, amount, openid, unionid, mp_openid, user_id FROM withdrawal_records WHERE id=%s", (wid,))
            r = c.fetchone()
            if r and r['user_phone']:
                upsert_user_balance_row(c, phone=r['user_phone'],
                                        openid=r.get('openid', '') or '',
                                        unionid=r.get('unionid', '') or '',
                                        mp_openid=r.get('mp_openid', '') or '',
                                        balance=r['amount'], total_withdrawn=-r['amount'],
                                        user_id=r.get('user_id') or 0)
            c.execute("UPDATE withdrawal_records SET status=3, approve_time=NOW(), approver=%s WHERE id=%s",
                      (session.get('admin_user', 'admin'), wid))
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
def alarms_list():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
        status = request.args.get('status', '')
        offset = (page - 1) * size
        conn = get_db()
        c = conn.cursor()
        sql = """SELECT a.*, c.cabinet_code, c.name as cabinet_name, l.name as location_name
                FROM alarms a LEFT JOIN cabinets c ON a.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id WHERE 1=1"""
        params = []
        if status != '':
            sql += " AND a.status=%s"
            params.append(int(status))
        sql += " ORDER BY a.id DESC LIMIT %s OFFSET %s"
        params += [size, offset]
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        count_sql = "SELECT COUNT(*) FROM alarms a WHERE 1=1"
        count_params = []
        if status != '':
            count_sql += " AND a.status=%s"
            count_params.append(int(status))
        c.execute(count_sql, count_params)
        total = c.fetchone()[0]
        conn.close()
        return json_response(data={'list': rows, 'total': total, 'page': page, 'size': size})
    except Exception as e:
        logger.error(f'[alarms_list] {e}')
        return json_response(message=str(e), code=500)

@bp.route('/alarms/resolve', methods=['POST'])
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

@bp.route('/settings/save', methods=['POST'])
def save_settings():
    try:
        data = request.get_json() or {}
        if isinstance(data.get('settings'), list):
            data = {item.get('setting_key'): item.get('setting_value') for item in data['settings'] if item and item.get('setting_key')}
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
        c.execute('SELECT e.*, m.name as merchant_name, a.name as agent_name FROM employees e LEFT JOIN merchants m ON e.merchant_id=m.id LEFT JOIN agents a ON e.agent_id=a.id WHERE e.id=%s', (emp_id,))
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

# ==================== 自动提现异步退款 ====================
_AUTO_WITHDRAW_SCHEDULER_LOCK_FILE = "/tmp/auto_withdraw_scheduler.lock"
_AUTO_WITHDRAW_BATCH_SIZE = 50
_AUTO_WITHDRAW_SCAN_SECONDS = 5
_AUTO_WITHDRAW_TEMPLATE_ID = "YsfB8FH4eMrISAS92oUzBhoXe178AnxP8XSA0_24YoE"


def _reset_stale_auto_claims():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE withdrawal_records SET error_msg=NULL WHERE status=0 AND error_msg='PROCESSING' AND created_at < NOW() - INTERVAL '15 minutes'")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error('[auto_withdraw] 重置超时处理标记失败: %s', e)


def _claim_auto_withdrawal(wid):
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE withdrawal_records SET error_msg='PROCESSING', approve_time=NOW() WHERE id=%s AND status=0 AND (error_msg IS NULL OR error_msg <> 'PROCESSING') RETURNING id", (wid,))
        row = c.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        logger.error('[auto_withdraw] 认领失败 id=%s: %s', wid, e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _release_auto_claim(wid):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE withdrawal_records SET error_msg=NULL WHERE id=%s AND status=0", (wid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error('[auto_withdraw] 释放认领失败 id=%s: %s', wid, e)


def _send_withdraw_subscribe(phone, amount, thing3, thing2, openid='', unionid=''):
    try:
        from helpers import send_wx_subscribe_message
        wd_data = {
            'amount8': {'value': '¥{:.2f}'.format(float(amount))},
            'time6': {'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
            'thing3': {'value': thing3},
            'thing2': {'value': thing2}
        }
        # 只认小程序 mp_openid（oWrA8 前缀）；公众号 openid(oLhbm2) 发不了订阅消息
        _ok = openid or ''
        if not (str(_ok).startswith('oWrA8')):
            _ok = ''
        send_wx_subscribe_message(_ok, _AUTO_WITHDRAW_TEMPLATE_ID, wd_data, phone=phone, page='pages/mine/mine', unionid=unionid)
    except Exception as e:
        logger.error('[auto_withdraw] 订阅通知失败 phone=%s: %s', phone, e)


def _process_auto_withdrawal_record(wid):
    claimed = _claim_auto_withdrawal(wid)
    if not claimed:
        return
    done = False
    conn = None
    conn2 = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT w.user_phone, w.amount, w.order_id, w.order_ids, w.openid AS w_openid,
                   (SELECT o.unionid FROM orders o WHERE o.id = COALESCE(w.order_id, NULL)) AS w_unionid
            FROM withdrawal_records w
            WHERE w.id=%s
        """, (wid,))
        row = c.fetchone()
        conn.close()
        conn = None
        if not row:
            done = True
            return
        phone = row['user_phone']
        amount = float(row['amount'])
        order_ids = []
        try:
            import json as _json
            parsed = _json.loads(row['order_ids'] or '[]') if row.get('order_ids') else []
            order_ids = [int(x) for x in parsed]
        except Exception:
            order_ids = []
        if not order_ids and row.get('order_id'):
            order_ids = [int(row['order_id'])]
        if not order_ids:
            done = True
            return
        from helpers import do_real_refund
        conn2 = get_db()
        c2 = conn2.cursor()
        failed = []
        failed_amount = 0.0
        first_msg = ''
        for oid in order_ids:
            c2.execute("""SELECT o.order_no, o.payment_channel_id, o.refund_status,
                COALESCE(bd.amount, 0) as bd_amount,
                COALESCE(o.deposit_amount,0) - COALESCE(o.refund_amount,0) as remain_amount
                FROM orders o
                LEFT JOIN (SELECT order_id, MAX(amount) as amount FROM user_balance_details
                           WHERE status IN ('available','pending') GROUP BY order_id) bd
                    ON bd.order_id = o.id
                WHERE o.id=%s""", (oid,))
            od = c2.fetchone()
            if not od:
                failed.append(oid)
                continue
            if od.get('refund_status') == 'refunded':
                c2.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (oid,))
                continue
            refund_this = float(od['bd_amount'] or od['remain_amount'] or 0)
            if refund_this <= 0:
                continue
            success, refund_id, msg = do_real_refund(
                order_id=oid,
                order_no=od['order_no'],
                amount=refund_this,
                payment_channel_id=od['payment_channel_id'],
            )
            if success or ('订单已全额退款' in str(msg)) or ('该订单已全额退款' in str(msg)):
                c2.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=COALESCE(%s, refund_id), refund_amount=GREATEST(COALESCE(refund_amount,0), %s), refund_time=NOW(), refund_mark=1 WHERE id=%s", (refund_id, refund_this, oid))
                c2.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (oid,))
            else:
                failed.append(oid)
                failed_amount += refund_this
                if not first_msg:
                    first_msg = str(msg)
        if not failed:
            c2.execute("UPDATE withdrawal_records SET status=2, approve_time=NOW(), error_msg=NULL, retry_count=0, next_attempt_at=NULL WHERE id=%s", (wid,))
            conn2.commit()
            _send_withdraw_subscribe(phone, amount, '????', '??0-3??????', row.get('w_openid') or '', row.get('w_unionid') or '')
            logger.info('[auto_withdraw] ???? id=%s orders=%s', wid, order_ids)
            done = True
        else:
            c2.execute("UPDATE withdrawal_records SET retry_count=retry_count+1, error_msg=%s, next_attempt_at=NULL WHERE id=%s", (first_msg, wid))
            c2.execute("SELECT retry_count FROM withdrawal_records WHERE id=%s", (wid,))
            rcnt_row = c2.fetchone()
            rcnt = int(rcnt_row[0]) if rcnt_row else 3
            if ('余额不足' in str(first_msg) or 'NOTENOUGH' in str(first_msg).upper()):
                # 商户号余额不足：直接拒绝不重试，避免队列积压 (2026-08-19)
                # 2026-08-23: 失败单不退余额, 余额明细保持pending(隐藏); 2026-08-23晚: 不再同步打logic_mark=Y(避免破坏商户订单比例隐藏)
                if failed_amount > 0:
                    for foid in failed:
                        c2.execute("UPDATE user_balance_details SET status='pending' WHERE order_id=%s", (foid,))
                _reject_msg = '商户号余额不足，该部分余额已隐藏'
                c2.execute("UPDATE withdrawal_records SET status=3, error_msg=%s, dedup_key=NULL, next_attempt_at=NULL, approve_time=NOW(), approver='自动' WHERE id=%s", (_reject_msg, wid))
                try:
                    c2.execute("INSERT INTO alarms (type, device_id, content, status, created_at) VALUES ('withdraw_refund_failed', NULL, %s, '0', NOW())", (('余额不足自动拒绝: ' + _reject_msg)[:500],))
                except Exception as _alarm_e:
                    logger.error('[auto_withdraw] alarm insert fail: %s', _alarm_e)
                conn2.commit()
                _send_withdraw_subscribe(phone, amount, '提现被拒绝', '商户号余额不足，请稍后重试', row.get('w_openid') or '', row.get('w_unionid') or '')
                logger.warning('[auto_withdraw] 余额不足直接拒绝 id=%s orders=%s msg=%s', wid, order_ids, first_msg)
                done = True
            elif rcnt >= 3:
                if failed_amount > 0:
                    c2.execute("UPDATE user_balances SET balance=balance+%s, total_withdrawn=GREATEST(total_withdrawn-%s,0) WHERE phone=%s ", (failed_amount, failed_amount, phone))
                    if c2.rowcount == 0:
                        c2.execute("INSERT INTO user_balances (phone, balance, total_withdrawn, first_use_time) VALUES (%s, %s, 0, NOW())", (phone, failed_amount))
                    for foid in failed:
                        c2.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (foid,))
                c2.execute("UPDATE withdrawal_records SET status=4, error_msg=%s, dedup_key=NULL, next_attempt_at=NULL, approve_time=NOW(), approver='自动' WHERE id=%s", (first_msg, wid))
                try:
                    c2.execute("INSERT INTO alarms (type, device_id, content, status, created_at) VALUES ('withdraw_refund_failed', NULL, %s, '0', NOW())", (('自动退款失败: ' + str(first_msg))[:500],))
                except Exception as _alarm_e:
                    logger.error('[auto_withdraw] alarm insert fail: %s', _alarm_e)
                conn2.commit()
                logger.error('[auto_withdraw] ???????? id=%s orders=%s msg=%s', wid, order_ids, first_msg)
                done = True
            else:
                delay = '30 seconds' if rcnt == 1 else '2 minutes'
                c2.execute("UPDATE withdrawal_records SET next_attempt_at=NOW() + INTERVAL %s WHERE id=%s", (delay, wid))
                conn2.commit()
                logger.warning('[auto_withdraw] ????????? id=%s orders=%s msg=%s retry=%s/3', wid, order_ids, first_msg, rcnt)
                done = True
    except Exception as e:
        logger.error('[auto_withdraw] ???? id=%s: %s', wid, e, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if conn2 is not None:
            try:
                conn2.close()
            except Exception:
                pass
        if not done:
            _release_auto_claim(wid)


def _process_auto_withdrawal_batch(max_rows=None):
    if max_rows is None:
        max_rows = _AUTO_WITHDRAW_BATCH_SIZE
    _reset_stale_auto_claims()
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT w.id
            FROM withdrawal_records w
            JOIN orders o ON w.order_id = o.id
            JOIN cabinets cb ON o.cabinet_id = cb.id
            JOIN locations l ON cb.location_id = l.id
            WHERE w.status = 0 AND l.withdraw_mode = 'auto_approve'
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
              AND (w.auto_approve_time IS NULL OR w.auto_approve_time::timestamp <= NOW())
              AND (w.next_attempt_at IS NULL OR w.next_attempt_at <= NOW())
            ORDER BY w.id
            LIMIT %s
        """, (max_rows,))
        ids = [r['id'] for r in c.fetchall()]
        conn.close()
        conn = None
        for wid in ids:
            _process_auto_withdrawal_record(wid)
    except Exception as e:
        logger.error('[auto_withdraw] 批量处理异常: %s', e, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _auto_withdraw_scheduler():
    import time
    while True:
        lock_fd = None
        try:
            import fcntl
            lock_fd = open(_AUTO_WITHDRAW_SCHEDULER_LOCK_FILE, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                time.sleep(_AUTO_WITHDRAW_SCAN_SECONDS)
                continue
            _process_auto_withdrawal_batch()
        except Exception as e:
            logger.error('[auto_withdraw] 调度异常: %s', e)
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    try:
                        lock_fd.close()
                    except Exception:
                        pass
        time.sleep(_AUTO_WITHDRAW_SCAN_SECONDS)


_auto_withdraw_thread = threading.Thread(target=_auto_withdraw_scheduler, daemon=True)
_auto_withdraw_thread.start()

# ==================== 人工审批超时提醒 ====================
_MANUAL_APPROVAL_ALERT_LOCK_FILE = "/tmp/manual_approval_alert.lock"
_MANUAL_APPROVAL_ALERT_INTERVAL = 1800


def _send_manual_approval_timeout_alert():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*)
            FROM withdrawal_records w
            JOIN orders o ON w.order_id = o.id
            JOIN cabinets cb ON o.cabinet_id = cb.id
            JOIN locations l ON cb.location_id = l.id
            WHERE w.status = 0 AND l.withdraw_mode = 'manual_approve'
              AND w.created_at < NOW() - INTERVAL '24 hours'
        """)
        cnt = int(c.fetchone()[0] or 0)
        if cnt <= 0:
            conn.close()
            conn = None
            return
        content = '人工审批超时：%s 笔提现超过24小时未处理' % cnt
        c.execute("""
            INSERT INTO alarms (type, content, level, status)
            SELECT 'manual_approval_timeout', %s, 1, 0
            WHERE NOT EXISTS (SELECT 1 FROM alarms WHERE type='manual_approval_timeout' AND status=0)
        """, (content,))
        conn.commit()
        logger.warning('[manual_approval_alert] 已写入提醒: %s', content)
    except Exception as e:
        logger.error('[manual_approval_alert] 写入提醒失败: %s', e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _manual_approval_alert_scheduler():
    import time
    time.sleep(30)
    while True:
        lock_fd = None
        try:
            import fcntl
            lock_fd = open(_MANUAL_APPROVAL_ALERT_LOCK_FILE, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                time.sleep(_MANUAL_APPROVAL_ALERT_INTERVAL)
                continue
            _send_manual_approval_timeout_alert()
        except Exception as e:
            logger.error('[manual_approval_alert] 调度异常: %s', e)
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    try:
                        lock_fd.close()
                    except Exception:
                        pass
        time.sleep(_MANUAL_APPROVAL_ALERT_INTERVAL)


_manual_approval_alert_thread = threading.Thread(target=_manual_approval_alert_scheduler, daemon=True)
_manual_approval_alert_thread.start()

# ==================== P1: 批量自动提现 ====================

_BATCH_AUTO_LOCK_FILE = "/tmp/withdrawal_batch_auto.lock"

def _run_withdrawal_batch_auto():
    """批量自动退款核心逻辑（队列审批+人工审批+自动审批），供独立脚本调用"""
    import random as _rnd
    conn = None
    try:
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        approved = 0
        rejected = 0

        # 0. 白名单必退：当天投诉白名单用户结束订单时入队(approver='whitelist_auto', status=0)
        #    必定原路退款，不走通过率/网点模式判断（避免微信退款阻塞结束订单事务锁柜格）
        rows_wl = c.execute("""
            SELECT w.id, w.user_phone, w.amount, w.order_id, w.order_ids
            FROM withdrawal_records w
            WHERE w.status = 0 AND w.approver = 'whitelist_auto'
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
            LIMIT 200
        """).fetchall()
        for rw in rows_wl:
            _wid = rw['id']
            _w_phone = rw['user_phone'] or ''
            _w_oids = []
            import json as _json_wl
            try:
                _w_oids = [int(x) for x in _json_wl.loads(rw.get('order_ids') or '[]')]
            except Exception:
                _w_oids = []
            if not _w_oids and rw.get('order_id'):
                _w_oids = [int(rw['order_id'])]
            if not _w_oids:
                c.execute("UPDATE withdrawal_records SET status=4, error_msg='订单不存在', approve_time=NOW(), approver='白名单' WHERE id=%s", (_wid,))
                rejected += 1
                continue
            _all_ok = True
            _failed_oids = []
            _failed_amt = 0.0
            _first_msg = ''
            for _oid in _w_oids:
                c.execute("SELECT order_no, payment_channel_id, deposit_amount, COALESCE(refund_amount,0) AS refund_amount, refund_status FROM orders WHERE id=%s", (_oid,))
                _ord = c.fetchone()
                if not _ord:
                    _all_ok = False
                    _failed_oids.append(_oid)
                    continue
                if _ord.get('refund_status') == 'refunded':
                    continue
                _rthis = float(_ord.get('deposit_amount') or 0) - float(_ord.get('refund_amount') or 0)
                if _rthis <= 0:
                    continue
                from helpers import do_real_refund
                _suc, _rid, _msg = do_real_refund(order_id=_oid, order_no=_ord['order_no'], amount=_rthis, payment_channel_id=_ord['payment_channel_id'])
                if _suc:
                    c.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=COALESCE(%s, refund_id), refund_amount=GREATEST(COALESCE(refund_amount,0), %s), refund_time=NOW(), refund_mark=1 WHERE id=%s", (_rid, _rthis, _oid))
                    c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (_oid,))
                else:
                    _all_ok = False
                    _failed_oids.append(_oid)
                    _failed_amt += _rthis
                    if not _first_msg:
                        _first_msg = str(_msg)
            if _all_ok:
                c.execute("UPDATE withdrawal_records SET status=2, approve_time=NOW(), approver='白名单' WHERE id=%s", (_wid,))
                approved += 1
            else:
                # 退款失败：退余额（用户可再提现），记录错误，后台下轮可重试
                if _failed_amt > 0 and _w_phone:
                    c.execute("UPDATE user_balances SET balance=balance+%s, total_withdrawn=GREATEST(COALESCE(total_withdrawn,0)-%s,0) WHERE phone=%s", (_failed_amt, _failed_amt, _w_phone))
                    if c.rowcount == 0:
                        c.execute("INSERT INTO user_balances (phone, balance, total_withdrawn, first_use_time) VALUES (%s, %s, 0, NOW())", (_w_phone, _failed_amt))
                    for _foid in _failed_oids:
                        c.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (_foid,))
                c.execute("UPDATE withdrawal_records SET status=0, error_msg=%s, approve_time=NOW(), approver='白名单' WHERE id=%s", ((_first_msg or '退款失败')[:500], _wid))
                try:
                    c.execute("INSERT INTO alarms (type, device_id, content, status, created_at) VALUES ('whitelist_refund_failed', NULL, %s, '0', NOW())", (('白名单退款失败: ' + (_first_msg or '退款失败'))[:500],))
                except Exception:
                    pass
        conn.commit()

        # 1. 队列审批：到 auto_approve_time 的按通过率退款
        rows = c.execute("""
            SELECT w.id, w.user_phone, w.amount, w.order_id, w.order_ids, w.auto_approve_time, w.openid,
                   l.refund_approve_rate,
                   ww.openid as wl_openid
            FROM withdrawal_records w
            JOIN orders o ON w.order_id = o.id
            JOIN cabinets cb ON o.cabinet_id = cb.id
            JOIN locations l ON cb.location_id = l.id
            LEFT JOIN withdrawal_whitelist ww
                   ON ((ww.unionid IS NOT NULL AND ww.unionid <> '' AND ww.unionid = o.unionid)
                       OR (COALESCE(ww.unionid, '') = '' AND ww.openid = w.openid))
                   AND (ww.expires_at IS NULL OR ww.expires_at > NOW())
                   AND (ww.remain_count = -1 OR ww.remain_count > 0)
            WHERE w.status = 0 AND l.withdraw_mode = 'queue_approve'
            AND w.auto_approve_time IS NOT NULL
            AND w.auto_approve_time::timestamp <= NOW()
            AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
        """).fetchall()
        for r in rows:
            rate = (r['refund_approve_rate'] or 80) / 100.0
            _wl_hit = bool(r.get('wl_openid'))
            if _wl_hit or _rnd.random() < rate:
                # 按打包订单逐单退款，避免用总额退单笔
                from helpers import do_real_refund
                import json as _json_q
                try:
                    order_ids = [int(x) for x in _json_q.loads(r.get('order_ids') or '[]')]
                except Exception:
                    order_ids = []
                if not order_ids and r.get('order_id'):
                    order_ids = [int(r['order_id'])]
                if not order_ids:
                    c.execute("UPDATE withdrawal_records SET status=4, error_msg='订单不存在', approve_time=datetime('now'), approver='自动' WHERE id=%s", (r['id'],))
                    continue
                c2 = conn.cursor()
                all_ok = True
                failed_oids = []
                failed_amount = 0.0
                first_msg = ''
                for oid in order_ids:
                    c2.execute("SELECT o.order_no, o.payment_channel_id, o.deposit_amount, COALESCE(o.refund_amount,0) AS refund_amount, o.refund_status FROM orders o WHERE id=%s", (oid,))
                    ord = c2.fetchone()
                    if not ord:
                        all_ok = False
                        failed_oids.append(oid)
                        continue
                    if ord.get('refund_status') == 'refunded':
                        continue
                    refund_this = float(ord.get('deposit_amount') or 0) - float(ord.get('refund_amount') or 0)
                    if refund_this <= 0:
                        continue
                    success, refund_id, msg = do_real_refund(order_id=oid, order_no=ord['order_no'], amount=refund_this, payment_channel_id=ord['payment_channel_id'])
                    if success:
                        c2.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=COALESCE(%s, refund_id), refund_amount=GREATEST(COALESCE(refund_amount,0), %s), refund_time=datetime('now'), refund_mark=1 WHERE id=%s", (refund_id, refund_this, oid))
                        c2.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (oid,))
                    else:
                        all_ok = False
                        failed_oids.append(oid)
                        failed_amount += refund_this
                        if not first_msg:
                            first_msg = str(msg)
                if all_ok:
                    c2.execute("UPDATE withdrawal_records SET status=2, approve_time=datetime('now'), approver='自动' WHERE id=%s", (r['id'],))
                    approved += 1
                    if _wl_hit:
                        # 白名单免审放行: 消耗一次白名单次数(限次来源)
                        try:
                            from helpers import consume_whitelist
                            consume_whitelist(r.get('openid') or '')
                        except Exception:
                            pass
                else:
                    if failed_amount > 0:
                        c2.execute("UPDATE user_balances SET balance=balance+%s, total_withdrawn=GREATEST(total_withdrawn-%s,0) WHERE phone=%s ", (failed_amount, failed_amount, r['user_phone']))
                        if c2.rowcount == 0 and r.get('user_phone'):
                            c2.execute("INSERT INTO user_balances (phone, balance, total_withdrawn, first_use_time) VALUES (%s, %s, 0, NOW())", (r['user_phone'], failed_amount))
                        for foid in failed_oids:
                            c2.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (foid,))
                    c2.execute("UPDATE withdrawal_records SET status=4, error_msg=%s, dedup_key=NULL, approve_time=datetime('now'), approver='自动' WHERE id=%s", ((first_msg or '退款失败')[:500], r['id']))
                    try:
                        c2.execute("INSERT INTO alarms (type, device_id, content, status, created_at) VALUES ('withdraw_refund_failed', NULL, %s, '0', NOW())", (('队列退款失败: ' + (first_msg or '退款失败'))[:500],))
                    except Exception:
                        pass
            else:
                # 通过率未达标：退余额并拒绝
                c.execute("UPDATE user_balances SET balance = balance + %s, total_withdrawn = total_withdrawn - %s WHERE phone = %s ",
                          (r['amount'], r['amount'], r['user_phone']))
                import json as _json_r
                try:
                    rids = [int(x) for x in _json_r.loads(r.get('order_ids') or '[]')]
                except Exception:
                    rids = []
                if not rids and r.get('order_id'):
                    rids = [int(r['order_id'])]
                for roid in rids:
                    c.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (roid,))
                c.execute("UPDATE withdrawal_records SET status=3, error_msg='自动拒绝', dedup_key=NULL, approve_time=datetime('now'), approver='队列' WHERE id=%s", (r['id'],))
                rejected += 1
        conn.commit()

        # 2. 人工审批：白名单或达到自动审批条件的按通过率退款
        rows2 = c.execute("""
            SELECT w.id, w.amount, w.user_phone, w.order_id, w.order_ids, w.openid, l.auto_approve_rate,
                   ww.openid as wl_openid
            FROM withdrawal_records w
            JOIN orders o ON w.order_id = o.id
            JOIN cabinets cb ON o.cabinet_id = cb.id
            JOIN locations l ON cb.location_id = l.id
            LEFT JOIN withdrawal_whitelist ww
                   ON ((ww.unionid IS NOT NULL AND ww.unionid <> '' AND ww.unionid = o.unionid)
                       OR (COALESCE(ww.unionid, '') = '' AND ww.openid = w.openid))
                   AND (ww.expires_at IS NULL OR ww.expires_at > NOW())
                   AND (ww.remain_count = -1 OR ww.remain_count > 0)
            WHERE w.status = 0 AND l.withdraw_mode = 'manual_approve'
              AND (w.error_msg IS NULL OR w.error_msg <> 'PROCESSING')
              AND (ww.openid IS NOT NULL
                   OR ((l.auto_approve_day IS NULL OR l.auto_approve_day <= 0
                        OR w.created_at::date <= CURRENT_DATE - l.auto_approve_day::integer)
                       AND (l.auto_approve_time IS NULL OR l.auto_approve_time = ''
                            OR CURRENT_TIME >= l.auto_approve_time::time)))
            LIMIT 1000
        """).fetchall()
        conn.commit()
        conn.close()
        conn = None
        for r in rows2:
            local_conn = None
            try:
                local_conn = get_db()
                lc = local_conn.cursor()
                rate = (r['auto_approve_rate'] or 0) / 100.0
                _wl_hit = bool(r.get('wl_openid'))
                if _wl_hit or _rnd.random() < rate:
                    # 按打包订单逐单退款，全部成功才置为通过
                    from helpers import do_real_refund
                    import json as _json_b2
                    try:
                        order_ids = [int(x) for x in _json_b2.loads(r.get('order_ids') or '[]')]
                    except Exception:
                        order_ids = []
                    if not order_ids and r.get('order_id'):
                        order_ids = [int(r['order_id'])]
                    if not order_ids:
                        lc.execute("UPDATE withdrawal_records SET status=4, error_msg=%s, approve_time=NOW(), approver='自动', dedup_key=NULL WHERE id=%s", ('订单不存在', r['id']))
                        continue
                    all_ok = True
                    failed_oids = []
                    failed_amount = 0.0
                    first_msg = ''
                    for oid in order_ids:
                        lc.execute("SELECT order_no, payment_channel_id, deposit_amount, COALESCE(refund_amount,0) AS refund_amount, refund_status FROM orders WHERE id=%s", (oid,))
                        ord_row = lc.fetchone()
                        if not ord_row:
                            all_ok = False
                            failed_oids.append(oid)
                            continue
                        if ord_row.get('refund_status') == 'refunded':
                            continue
                        refund_this = float(ord_row.get('deposit_amount') or 0) - float(ord_row.get('refund_amount') or 0)
                        if refund_this <= 0:
                            continue
                        success, refund_id, msg = do_real_refund(order_id=oid, order_no=ord_row['order_no'], amount=refund_this, payment_channel_id=ord_row['payment_channel_id'])
                        if success:
                            lc.execute("UPDATE orders SET status=4, refund_status='refunded', refund_id=COALESCE(%s, refund_id), refund_amount=GREATEST(COALESCE(refund_amount,0), %s), refund_time=NOW(), refund_mark=1 WHERE id=%s", (refund_id, refund_this, oid))
                            lc.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (oid,))
                        else:
                            all_ok = False
                            failed_oids.append(oid)
                            failed_amount += refund_this
                            if not first_msg:
                                first_msg = str(msg)
                    if all_ok:
                        lc.execute("UPDATE withdrawal_records SET status=2, approve_time=NOW(), approver='自动' WHERE id=%s", (r['id'],))
                        approved += 1
                        if _wl_hit:
                            # 白名单免审放行: 消耗一次白名单次数(限次来源)
                            try:
                                from helpers import consume_whitelist
                                consume_whitelist(r.get('openid') or '')
                            except Exception:
                                pass
                    else:
                        _rp = r.get('user_phone') or ''
                        if failed_amount > 0:
                            lc.execute("UPDATE user_balances SET balance=balance+%s, total_withdrawn=GREATEST(total_withdrawn-%s,0) WHERE phone=%s", (failed_amount, failed_amount, _rp))
                            if lc.rowcount == 0 and _rp:
                                lc.execute("INSERT INTO user_balances (phone, balance, total_withdrawn, first_use_time) VALUES (%s, %s, 0, NOW())", (_rp, failed_amount))
                            for foid in failed_oids:
                                lc.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (foid,))
                        lc.execute("UPDATE withdrawal_records SET status=4, error_msg=%s, approve_time=NOW(), approver='自动', dedup_key=NULL WHERE id=%s", ((first_msg or '退款失败')[:500], r['id']))
                        try:
                            lc.execute("INSERT INTO alarms (type, device_id, content, status, created_at) VALUES ('withdraw_refund_failed', NULL, %s, '0', NOW())", (('自动审批退款失败: ' + (first_msg or '退款失败'))[:500],))
                        except Exception:
                            pass
                else:
                    _oid = r.get('openid', '') or ''
                    if _oid:
                        lc.execute("UPDATE user_balances SET balance=balance+%s, total_withdrawn=GREATEST(total_withdrawn-%s,0) WHERE openid=%s", (r['amount'], r['amount'], _oid))
                    if not _oid or lc.rowcount == 0:
                        lc.execute("UPDATE user_balances SET balance=balance+%s, total_withdrawn=GREATEST(total_withdrawn-%s,0) WHERE phone=%s ", (r['amount'], r['amount'], r['user_phone']))
                        if lc.rowcount == 0:
                            lc.execute("INSERT INTO user_balances (phone, balance, total_withdrawn, first_use_time) VALUES (%s, %s, 0, NOW())", (r['user_phone'], r['amount']))
                    _oids_str = r.get('order_ids') or '[]'
                    try:
                        if _oids_str and _oids_str != '[]':
                            import json as _json_batch
                            _oids = _json_batch.loads(_oids_str)
                            if _oids:
                                lc.execute("UPDATE user_balance_details SET status='available' WHERE order_id::text = ANY(%s) AND status='pending'", (list(map(str, _oids)),))
                    except:
                        pass
                    if not _oids_str or _oids_str == '[]':
                        if r.get('order_id'):
                            lc.execute("UPDATE user_balance_details SET status='available' WHERE order_id=%s AND status='pending'", (r['order_id'],))
                    lc.execute("UPDATE withdrawal_records SET status=3, error_msg='自动拒绝(通过率未达标)' WHERE id=%s", (r['id'],))
                    rejected += 1
                local_conn.commit()
            except Exception as _e:
                logger.error(f'[batch_manual] 处理提现失败 id={r["id"]}: {_e}')
            finally:
                if local_conn is not None:
                    try:
                        local_conn.close()
                    except Exception:
                        pass
        _process_auto_withdrawal_batch(50)
        return approved, rejected
    except Exception as e:
        logger.error(f'[withdrawal_batch_auto] 批处理异常: {e}')
        return 0, 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@bp.route('/admin/withdrawal/batch-auto', methods=['POST'])
def withdrawal_batch_auto():
    """队列+人工审批自动退款 - 转独立进程执行，避免阻塞gunicorn worker"""
    try:
        # 文件锁防并发
        lock_fd = None
        try:
            import fcntl
            lock_fd = open(_BATCH_AUTO_LOCK_FILE, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return jsonify({'code': 200, 'message': '退款任务已在执行中，跳过本次触发', 'data': {'skipped': True}})
        except Exception:
            lock_fd = None
        # 后台线程执行，接口立即返回
        import threading as _th_batch
        def _batch_worker():
            try:
                approved, rejected = _run_withdrawal_batch_auto()
                logger.info(f'[withdrawal_batch_auto] 后台执行完成: 通过{approved}笔, 拒绝{rejected}笔')
            except Exception as _e:
                logger.error(f'[withdrawal_batch_auto] 后台执行异常: {_e}')
            finally:
                if lock_fd is not None:
                    try:
                        import fcntl
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        lock_fd.close()
                    except Exception:
                        pass
        t = _th_batch.Thread(target=_batch_worker, daemon=True)
        t.start()
        return jsonify({'code': 200, 'message': '退款任务已转入后台执行', 'data': {'approved': 0, 'rejected': 0}})
    except Exception as e:
        logger.error(f'[withdrawal_batch_auto] 触发失败: {e}')
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
        
        where_parts = ["(o.logic_mark IS NULL OR o.logic_mark != 'Y') AND (o.logic_mark = 'N' OR COALESCE(o.auto_hidden, 0) = 0)", "o.status NOT IN (5)"]
        params = []
        
        if agent_id:
            where_parts.append('loc.merchant_id IN (SELECT id FROM merchants WHERE agent_id=%s)')
            params.append(agent_id)
        if merchant_id:
            where_parts.append('loc.merchant_id=%s')
            params.append(merchant_id)
        if start_date and end_date:
            where_parts.append("date(o.created_at) >= %s AND date(o.created_at) <= %s")
            params.extend([start_date, end_date])
        else:
            where_parts.append("o.created_at >= NOW() - INTERVAL '30 days'")
        
        where_clause = ' WHERE ' + ' AND '.join(where_parts)
        
        c.execute(f'''SELECT
            o.id, o.user_phone, o.logic_mark, o.auto_hidden, o.deposit_amount,
            CASE WHEN o.status=4 THEN COALESCE(o.refund_amount,0) ELSE 0 END as refund_amount,
            loc.id as location_id, loc.name as location_name, loc.merchant_id,
            loc.hide_ratio, loc.whitelist_phones,
            m.name as merchant_name, COALESCE(m.commission_per_order,0) as commission_per_order,
            a.name as agent_name
            FROM orders o
            LEFT JOIN cabinets cab ON o.cabinet_id=cab.id
            LEFT JOIN locations loc ON cab.location_id=loc.id
            LEFT JOIN merchants m ON loc.merchant_id=m.id
            LEFT JOIN agents a ON m.agent_id=a.id
            {where_clause}
            ORDER BY loc.name''', params)

        groups = {}
        for r in c.fetchall():
            if r['logic_mark'] == 'Y':
                continue
            if r['logic_mark'] != 'N' and (r.get('auto_hidden') or 0) == 1:
                continue
            key = (
                r['location_id'], r['location_name'], r['merchant_id'], r['merchant_name'],
                float(r['commission_per_order'] or 0), r['agent_name'],
            )
            if key not in groups:
                groups[key] = {
                    'location_id': r['location_id'],
                    'location_name': r['location_name'],
                    'merchant_id': r['merchant_id'],
                    'merchant_name': r['merchant_name'],
                    'commission_per_order': float(r['commission_per_order'] or 0),
                    'agent_name': r['agent_name'],
                    'order_count': 0,
                    'deposit_total': 0.0,
                    'refund_total': 0.0,
                    'share_total': 0.0,
                }
            g = groups[key]
            g['order_count'] += 1
            g['deposit_total'] += float(r['deposit_amount'] or 0)
            g['refund_total'] += float(r['refund_amount'] or 0)

        details = list(groups.values())
        details.sort(key=lambda d: d['location_name'] or '')
        for d in details:
            d['share_total'] = round(d['order_count'] * d['commission_per_order'], 2)

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

@bp.route('/admin/alerts', methods=['GET', 'POST'])
def alerts_list():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 10))
        alert_type = request.args.get('alert_type', '')
        device_id = request.args.get('device_id', '')
        days = int(request.args.get('days', 3))
        logger.info(f'[alerts_list] DB_PATH={DB_PATH}, page={page}, limit={limit}, device={device_id}, type={alert_type}, days={days}')
        # database.py 会把 sqlite3.connect patch 成 PostgreSQL, 这里用原始 SQLite 连接访问 locker.db
        from database import _orig_connect as _sqlite_orig
        conn = _sqlite_orig(DB_PATH)
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
            where += " AND created_at >= datetime(?, ?)"
            params.extend(['now', f'-{days} days'])
        total = c.execute(f"SELECT COUNT(*) FROM device_alerts {where}", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM device_alerts {where} ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, (page-1)*limit]).fetchall()
        result_list = []
        for r in rows:
            d = dict(r)
            ts = d.get('created_at', '')
            if ts and ' ' in str(ts):
                d['created_at'] = str(ts)[:19]
            result_list.append(d)
        # Fill cabinet/location info from device_id
        try:
            cab_conn = get_db()
            cab_c = cab_conn.cursor()
            cab_c.execute("SELECT c.mainboard_device_id, c.cabinet_code, c.name as cabinet_name, l.name as location_name FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id")
            cab_map = {}
            for info in cab_c.fetchall():
                did = info['mainboard_device_id']
                if did:
                    cab_map[did] = info
            cab_conn.close()
            for d in result_list:
                info = cab_map.get(d.get('device_id'))
                if info:
                    d['cabinet_code'] = info['cabinet_code']
                    d['cabinet_name'] = info['cabinet_name']
                    d['location_name'] = info['location_name']
        except Exception as _oe:
            logger.error(f'[alerts_cabinet_info] {_oe}')
        conn.close()
        return jsonify({'code': 200, 'data': {'list': result_list, 'total': total, 'device_summaries': device_summaries}})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})
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
                                COALESCE((SELECT SUM(CASE WHEN cs.status=1 AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.slot_id=cs.id AND o2.status=2) THEN 1 ELSE 0 END) FROM cabinet_slots cs JOIN cabinets cab ON cs.cabinet_id=cab.id WHERE cab.group_id=cg.id),0) as available_slots,
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
            LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON o.user_phone=po.phone
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
        from helpers import is_heartbeat_online
        is_online = is_heartbeat_online(slot.get('last_heartbeat'))
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
        send_open_lock(device_id, board_no, lock_no, protocol, require_online=True)
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
        from helpers import is_heartbeat_online
        is_online = is_heartbeat_online(cabinet.get('last_heartbeat'))
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
        c.execute('SELECT mainboard_device_id, last_heartbeat FROM cabinets WHERE id=%s', (cabinet_id,))
        cabinet = c.fetchone()
        if not cabinet or not cabinet['mainboard_device_id']:
            conn.close()
            return json_response(message='设备未配置', code=400)
        device_id = cabinet['mainboard_device_id']
        board_no = slot['board_no']
        lock_no = slot['lock_no']
        from helpers import is_device_online
        if not is_device_online(str(device_id), cabinet.get('last_heartbeat')):
            conn.close()
            return json_response(message='设备离线，无法开门', code=400)
        conn.close()
        # 调用开门逻辑
        from helpers import send_open_lock
        result = send_open_lock(device_id, board_no, lock_no, order_id=order.get('order_no', str(order_id)), require_online=True, manual=True)
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
    """清柜: 结束所有活跃订单+退押金+通知用户，不开门"""
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
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        from helpers import refund_deposit_to_balance, send_wx_subscribe_message
        # 查询所有活跃订单(使用中2+已结算3)的完整信息
        c.execute("""SELECT id, order_no, slot_id, user_phone, deposit_amount, openid, unionid, mp_openid, wechat_name, status, compartment_number
                     FROM orders WHERE cabinet_id=%s AND status IN (2,3)""", (cabinet_id,))
        active = c.fetchall()
        ended = 0
        notified = 0
        for o in active:
            o_dict = dict(o)
            deposit_amount = float(o_dict.get('deposit_amount') or 0)
            # 结束订单并补齐退款字段
            c.execute("""UPDATE orders SET status=3, refund_mark=1, refund_amount=0, refund_status='none',
                         refund_time=NULL, logical_mark='end', retrieve_time=%s, pickup_time=%s, updated_at=%s WHERE id=%s""",
                      (now, now, now, o_dict['id']))
            if o_dict.get('slot_id'):
                c.execute('UPDATE cabinet_slots SET status=1 WHERE id=%s', (o_dict['slot_id'],))
            # 只有使用中订单才退押金并通知；已结算的不重复退
            if o_dict.get('status') == 2 and deposit_amount > 0 and o_dict.get('user_phone'):
                refunded, mp_openid, already_credited = refund_deposit_to_balance(c, o_dict)
                if not refunded:
                    continue
                if not already_credited:
                    try:
                        subscribe_data = {
                            'amount6': {'value': '¥{:.2f}'.format(deposit_amount)},
                            'time4': {'value': now},
                            'thing7': {'value': '已退还至小程序用户钱包'},
                            'thing2': {'value': '请自行点击此通知消息跳转“我的钱包”提现'}
                        }
                        send_wx_subscribe_message(mp_openid or '', '5OZIN-PdIT48ovySMI0qeiqED-cXxGvxQcgz6DEh79A', subscribe_data, phone=o_dict.get('user_phone'), page='pages/mine/mine', unionid=o_dict.get('unionid') or '')
                        notified += 1
                    except Exception as e:
                        logger.error(f'[clear_all] 发送订阅消息失败 order={o_dict["id"]}: {e}')
            ended += 1
        conn.commit()
        conn.close()
        return json_response(message=f'已结束{ended}个订单（通知{notified}人）')
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
        
        c.execute(f'SELECT COUNT(*) FROM orders o LEFT JOIN cabinets c ON o.cabinet_id=c.id LEFT JOIN locations l ON c.location_id=l.id LEFT JOIN (SELECT DISTINCT ON (phone) * FROM user_balances ORDER BY phone, id DESC) ub ON o.user_phone=ub.phone LEFT JOIN (SELECT DISTINCT ON (phone) * FROM users ORDER BY phone, id DESC) po ON o.user_phone=po.phone LEFT JOIN user_profiles up ON po.openid=up.openid WHERE {where}', params)
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

# ============ 微信投诉通知验签/解密辅助 ============
_wx_dec_key_cache = {}
_wx_platform_cert_cache = {}

def _verify_wechatpay_signature(headers, raw_body):
    """验证微信支付回调签名（平台证书公钥 RSA-SHA256）"""
    import base64 as _b64
    ts = headers.get('Wechatpay-Timestamp', '')
    nonce = headers.get('Wechatpay-Nonce', '')
    sig = headers.get('Wechatpay-Signature', '')
    serial = headers.get('Wechatpay-Serial', '')
    if not (ts and nonce and sig and serial):
        return False, '缺少验签请求头'
    cert_pem, err = _load_platform_cert(serial)
    if not cert_pem:
        # 新制"公钥"商户(平台证书404 RESOURCE_NOT_EXISTS)自动降级: 改用微信支付公钥验签
        cert_pem, err2 = _load_wechatpay_public_key(serial)
        if not cert_pem:
            return False, err + ' | ' + err2
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        cert_obj = x509.load_pem_x509_certificate(cert_pem.encode())
        message = (ts + '\n' + nonce + '\n' + raw_body.decode('utf-8') + '\n').encode('utf-8')
        cert_obj.public_key().verify(_b64.b64decode(sig), message, padding.PKCS1v15(), hashes.SHA256())
        return True, ''
    except Exception as e:
        return False, '验签失败: %s' % e


def _load_platform_cert(serial_no):
    """加载微信支付平台证书：磁盘缓存 → 首次自动下载（用商户证书签名 GET /v3/certificates，APIv3密钥解密）"""
    import os as _os, time as _t, base64 as _b64
    if serial_no in _wx_platform_cert_cache:
        return _wx_platform_cert_cache[serial_no], ''
    cert_path = _os.path.join(_os.path.dirname(WX_KEY_PATH), 'wechatpay_platform_%s.pem' % serial_no)
    if _os.path.exists(cert_path):
        with open(cert_path, 'r') as f:
            pem = f.read()
        _wx_platform_cert_cache[serial_no] = pem
        return pem, ''
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key_path = WX_KEY_PATH
        cert_serial = WX_CERT_SERIAL_NO
        mch_id = WX_MCH_ID
        if not _os.path.exists(key_path):
            _pc = get_db()
            _pc_cur = _pc.cursor()
            _pc_cur.execute("SELECT mch_id, cert_serial_no, cert_name FROM payment_channels WHERE cert_name IS NOT NULL AND cert_name != '' AND is_active=1 LIMIT 1")
            _pc_row = _pc_cur.fetchone()
            _pc.close()
            if _pc_row:
                mch_id = _pc_row[0]
                cert_serial = _pc_row[1]
                key_path = '/home/ubuntu/smart-locker/cert/%s_key.pem' % _pc_row[2]
        with open(key_path, 'r') as f:
            pk = serialization.load_pem_private_key(f.read().encode(), password=None)
        ts = str(int(_t.time()))
        nonce = _os.urandom(16).hex()
        url_path = '/v3/certificates'
        sign_str = 'GET\n' + url_path + '\n' + ts + '\n' + nonce + '\n\n'
        sig = _b64.b64encode(pk.sign(sign_str.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())).decode()
        auth = 'WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",timestamp="%s",serial_no="%s",signature="%s"' % (mch_id, nonce, ts, cert_serial, sig)
        import requests as _req
        r = _req.get('https://api.mch.weixin.qq.com' + url_path, headers={'Authorization': auth, 'Accept': 'application/json'}, timeout=10)
        if r.status_code != 200:
            return None, '下载平台证书失败 %s %s' % (r.status_code, r.text[:200])
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        for item in r.json().get('data', []):
            if item.get('serial_no') == serial_no:
                enc = item['encrypt_certificate']
                aesgcm = AESGCM(WX_API_V3_KEY.encode('utf-8'))
                pem = aesgcm.decrypt(enc['nonce'].encode('utf-8'), _b64.b64decode(enc['ciphertext']), enc['associated_data'].encode('utf-8')).decode('utf-8')
                with open(cert_path, 'w') as f:
                    f.write(pem)
                _wx_platform_cert_cache[serial_no] = pem
                return pem, ''
        return None, '平台证书序列号不匹配: %s' % serial_no
    except Exception as e:
        return None, '加载平台证书异常: %s' % e


def _load_wechatpay_public_key(serial_no):
    """加载微信支付公钥（新制"公钥"商户）：磁盘缓存 → 首次自动下载 GET /v3/pay/public-key（2026-08-23 投诉验签404修复）"""
    import os as _os, time as _t, base64 as _b64
    if serial_no in _wx_platform_cert_cache:
        return _wx_platform_cert_cache[serial_no], ''
    cert_path = _os.path.join(_os.path.dirname(WX_KEY_PATH), 'wechatpay_public_%s.pem' % serial_no)
    if _os.path.exists(cert_path):
        with open(cert_path, 'r') as f:
            pem = f.read()
        _wx_platform_cert_cache[serial_no] = pem
        return pem, ''
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key_path = WX_KEY_PATH
        cert_serial = WX_CERT_SERIAL_NO
        mch_id = WX_MCH_ID
        if not _os.path.exists(key_path):
            _pc = get_db()
            _pc_cur = _pc.cursor()
            _pc_cur.execute("SELECT mch_id, cert_serial_no, cert_name FROM payment_channels WHERE cert_name IS NOT NULL AND cert_name != '' AND is_active=1 LIMIT 1")
            _pc_row = _pc_cur.fetchone()
            _pc.close()
            if _pc_row:
                mch_id = _pc_row[0]
                cert_serial = _pc_row[1]
                key_path = '/home/ubuntu/smart-locker/cert/%s_key.pem' % _pc_row[2]
        with open(key_path, 'r') as f:
            pk = serialization.load_pem_private_key(f.read().encode(), password=None)
        ts = str(int(_t.time()))
        nonce = _os.urandom(16).hex()
        url_path = '/v3/pay/public-key'
        sign_str = 'GET\n' + url_path + '\n' + ts + '\n' + nonce + '\n\n'
        sig = _b64.b64encode(pk.sign(sign_str.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())).decode()
        auth = 'WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",timestamp="%s",serial_no="%s",signature="%s"' % (mch_id, nonce, ts, cert_serial, sig)
        import requests as _req
        r = _req.get('https://api.mch.weixin.qq.com' + url_path, headers={'Authorization': auth, 'Accept': 'application/json'}, timeout=10)
        if r.status_code != 200:
            return None, '下载支付公钥失败 %s %s' % (r.status_code, r.text[:200])
        data = r.json()
        if data.get('serial_no') != serial_no:
            return None, '支付公钥序列号不匹配: %s' % serial_no
        pem = data.get('public_key', '')
        if not pem:
            return None, '支付公钥为空'
        with open(cert_path, 'w') as f:
            f.write(pem)
        _wx_platform_cert_cache[serial_no] = pem
        return pem, ''
    except Exception as e:
        return None, '加载支付公钥异常: %s' % e


def _decrypt_complaint_resource(resource):
    """解密投诉通知 resource：使用主商户 APIv3 密钥（WX_API_V3_KEY）。
    注：payment_channels 表无 api_v3_key 列，各商户共用同一把 APIv3 密钥；
    若将来个别商户启用独立密钥，需给表加列后在此按 complained_mchid 取密钥。"""
    import base64 as _b64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ciphertext_b64 = resource.get('ciphertext', '')
    nonce = resource.get('nonce', '')
    associated_data = resource.get('associated_data', '')
    if not ciphertext_b64:
        return None, '无ciphertext'
    ciphertext = _b64.b64decode(ciphertext_b64)
    keys = [WX_API_V3_KEY]
    last_err = '未知错误'
    for key in keys:
        try:
            aesgcm = AESGCM(key.encode('utf-8'))
            plaintext = aesgcm.decrypt(nonce.encode('utf-8'), ciphertext, associated_data.encode('utf-8'))
            data = json.loads(plaintext.decode('utf-8'))
            return data, ''
        except Exception as e:
            last_err = str(e)
            continue
    return None, '解密失败: %s' % last_err


# ============ 微信投诉通知API (骨架) ============
# 注意：此API需要用户在微信支付后台配置投诉通知URL才能实际接收投诉
# 微信支付投诉通知URL格式: https://your-domain.com/api/admin_v2/wechat-complaint/notify
# 需要配置微信支付API证书和密钥才能解密投诉通知

@bp.route('/wechat-complaint/notify', methods=['POST'])
def wechat_complaint_notify():
    """微信支付投诉通知接收 - 自动解密+回复"""
    import hashlib, base64
    try:
        raw_body = request.get_data()
        _vok, _verr = _verify_wechatpay_signature(request.headers, raw_body)
        if not _vok:
            logger.warning('[wechat_complaint_notify] 验签失败: %s', _verr)
            return jsonify({'code': 'FAIL', 'message': 'verify fail'}), 401
        data = request.get_json() or {}
        logger.info('[wechat_complaint_notify] 收到通知: %s', json.dumps(data, ensure_ascii=False)[:500])
        
        # 解密通知内容 (AEAD_AES_256_GCM，自动匹配商户 APIv3 密钥)
        resource = data.get('resource', {})
        complaint_data, _derr = _decrypt_complaint_resource(resource)
        if complaint_data is None:
            logger.warning('[wechat_complaint_notify] %s', _derr)
            return jsonify({'code': 'FAIL', 'message': 'decrypt fail'}), 400
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
            # 不再即时拉白：改为退款成功后才拉白(见 _auto_refund_complaint_order / _auto_complete_complaint)
            logger.info('[wechat_complaint_notify] 已保存投诉: complaint_id=%s', complaint_id)
        else:
            logger.info('[wechat_complaint_notify] 投诉已存在: complaint_id=%s', complaint_id)
        conn.close()
        
        # 从 complaint_order_info 提取真正的支付 transaction_id
        transaction_id = ''
        if complaint_order_info:
            transaction_id = complaint_order_info[0].get('transaction_id', '')
        
        # 自动处理投诉（v2：只回复首响，不退款不结案；退款由调度器在5分钟后执行，失败3次转人工）
        if complaint_id:
            _search_no = order_no or transaction_id or ''
            # 查找正确的商户凭证
            _mch_id = complained_mchid
            if not _mch_id:
                # 从订单关联的商户获取
                try:
                    _cc_conn = get_db()
                    _cc = _cc_conn.cursor()
                    _cc.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id DESC LIMIT 1")
                    _cr = _cc.fetchone()
                    if _cr:
                        _mch_id = _cr['mch_id']
                    _cc.close()
                    _cc_conn.close()
                except:
                    pass
            _cert_serial = WX_CERT_SERIAL_NO
            _key_path = WX_KEY_PATH
            if complained_mchid:
                try:
                    conn5 = get_db()
                    c5 = conn5.cursor()
                    c5.execute("SELECT cert_serial_no, cert_name FROM payment_channels WHERE mch_id=%s ", (complainted_mchid,))
                    pc5 = c5.fetchone()
                    if pc5:
                        _cert_serial = pc5[0]
                        _key_path = f'/home/ubuntu/smart-locker/cert/{pc5[1]}_key.pem'
                    c5.close()
                    conn5.close()
                except:
                    pass
            if _mch_id:
                _auto_reply_complaint(complaint_id, _search_no, transaction_id, mch_id=_mch_id, cert_serial=_cert_serial, private_key_path=_key_path, content=WECHAT_FIRST_REPLY, complete_now=False)
            else:
                logger.error('[投诉处理] 无可用商户号，投诉 %s 未回复', complaint_id)
        
        # 拉正拉取投诉详情调用详细并
        if complaint_id:
            _fmch = complained_mchid
            if not _fmch:
                try:
                    _fc_conn = get_db()
                    _fc = _fc_conn.cursor()
                    _fc.execute('SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id DESC LIMIT 1')
                    _fr = _fc.fetchone()
                    if _fr: _fmch = _fr[0]
                    _fc.close()
                    _fc_conn.close()
                except: pass
            if _fmch:
                _fcert = WX_CERT_SERIAL_NO; _fkey = WX_KEY_PATH
                try:
                    _fcc_conn = get_db()
                    _fcc = _fcc_conn.cursor()
                    _fcc.execute('SELECT cert_serial_no,cert_name FROM payment_channels WHERE mch_id=%s', (_fmch,))
                    _fcr = _fcc.fetchone()
                    if _fcr:
                        _fcert = _fcr[0]
                        _fkey = f'/home/ubuntu/smart-locker/cert/{_fcr[1]}_key.pem'
                    _fcc.close()
                    _fcc_conn.close()
                except: pass
                _fetch_and_update_complaint(complaint_id, _fmch, _fcert, _fkey)

        return jsonify({'code': 'SUCCESS', 'message': 'ok'})
    except Exception as e:
        logger.error('[wechat_complaint_notify] 错误: %s', e, exc_info=True)
        return jsonify({'code': 'FAIL', 'message': str(e)})



def _auto_refund_complaint_order(order_no, transaction_id="", complaint_id="", payer_phone=""):
    """投诉自动原路退款：找到对应订单，调用微信退款API退回押金"""
    conn = None
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
        if not order and payer_phone:
            like_phone = payer_phone.replace('*', '_')
            if len(like_phone) >= 7:
                c.execute('SELECT id, order_no, transaction_id, deposit_amount, refund_amount, refund_mark, refund_status, status, slot_id, payment_channel_id, user_phone FROM orders WHERE user_phone LIKE %s ORDER BY id DESC LIMIT 1', (like_phone,))
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
        logger.info('[auto_refund_complaint] 处理投诉 id=%s 订单=%s 状态=%s 金额=%.2f refund_status=%s', complaint_id, order.get('order_no', ''), status, deposit, refund_status)
        if refund_status in ('success', 'refunded') or (status == 4 and order.get('refund_id')):
            conn.close()
            logger.info('[auto_refund_complaint] 订单已退款，跳过重复退款 order_id=%s', order_id)
            # 已退款=投诉诉求已达成: 补拉白(按网点次数,30天有效)
            try:
                from helpers import grant_complaint_whitelist
                _loc_id = None
                _gconn = get_db()
                _gcur = _gconn.cursor()
                _gcur.execute('SELECT cb.location_id FROM orders o2 JOIN cabinets cb ON cb.id = o2.cabinet_id WHERE o2.id=%s LIMIT 1', (order_id,))
                _lr = _gcur.fetchone()
                if _lr:
                    _loc_id = _lr[0]
                _gconn.close()
                grant_complaint_whitelist(phone=order.get('user_phone') or '', location_id=_loc_id)
            except Exception as _e5:
                logger.warning('[auto_refund_complaint] 已退订单补拉白失败: %s', _e5)
            return True, '订单已退款，无需重复退款'
        
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
        logger.info('[auto_refund_complaint] 即将退款 order_id=%s amount=%.2f payment_channel_id=%s', order_id, refund_amount, order.get('payment_channel_id'))
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
            c.execute("UPDATE orders SET refund_mark=1, refund_status='refunded', status=4, refund_id=%s, refund_amount=%s, refund_time=CURRENT_TIMESTAMP WHERE id=%s",
                      (refund_id or '', deposit, order_id))
            c.execute("UPDATE user_balance_details SET status='withdrawn' WHERE order_id=%s AND status IN ('available','pending')", (order_id,))
            # 释放柜格（如果还在使用中）
            if status == 2 and order.get('slot_id'):
                c.execute('UPDATE cabinet_slots SET status=1 WHERE id=%s', (order['slot_id'],))
            # 记录退款流水
            c.execute('INSERT INTO payments (order_id, type, amount, transaction_id, refund_transaction_id, status, created_at) VALUES (%s, 2, %s, %s, %s, 1, CURRENT_TIMESTAMP)',
                      (order_id, refund_amount, order.get('transaction_id', ''), refund_id or ''))
            # 更新投诉记录关联
            if complaint_id:
                c.execute('UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE wx_complaint_id=%s OR id=%s',
                          ('已自动原路退款', complaint_id, complaint_id))
            conn.commit()
            conn.close()
            logger.info('[auto_refund_complaint] 退款成功 order_id=%s amount=%.2f refund_id=%s', order_id, refund_amount, refund_id)
            # 退款成功才拉白(按网点 wl_max_uses 次数, 30天有效)
            try:
                from helpers import grant_complaint_whitelist
                _loc_id = None
                _gconn = get_db()
                _gcur = _gconn.cursor()
                _gcur.execute('SELECT cb.location_id FROM orders o2 JOIN cabinets cb ON cb.id = o2.cabinet_id WHERE o2.id=%s LIMIT 1', (order_id,))
                _lr = _gcur.fetchone()
                if _lr:
                    _loc_id = _lr[0]
                _gconn.close()
                grant_complaint_whitelist(phone=order.get('user_phone') or '', location_id=_loc_id)
            except Exception as _e4:
                logger.warning('[auto_refund_complaint] 拉白失败: %s', _e4)
            return True, refund_id
        else:
            conn.close()
            logger.error('[auto_refund_complaint] 退款失败 order_id=%s msg=%s', order_id, msg)
            # 永久性错误：订单在微信不存在，不再重试
            if '记录不存在' in msg or 'ORDERNOTEXIST' in msg:
                logger.info('[auto_refund_complaint] ORDERNOTEXIST, 标记投诉=%s为已处理', complaint_id)
                try:
                    c2 = get_db()
                    cur2 = c2.cursor()
                    cur2.execute('UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE wx_complaint_id=%s OR id=%s',
                              ('订单在微信不存在，自动退款失败', complaint_id, complaint_id))
                    c2.commit()
                    c2.close()
                except Exception as _e3:
                    logger.warning('[auto_refund_complaint] 更新投诉状态失败: %s', _e3)
                return True, '订单不存在(已标记)'
            return False, msg
    except Exception as e:
        logger.error('[auto_refund_complaint] 异常: %s', e, exc_info=True)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return False, str(e)


def _auto_reply_complaint(complaint_id, order_no="", transaction_id="", mch_id="", cert_serial="", private_key_path="", content=None, complete_now=True):
    """自动回复微信投诉"""
    import time, requests, base64
    try:
        if not complaint_id:
            logger.warning("[auto_reply] 投诉ID为空，跳过回复")
            return
        # 根据订单支付渠道选择对应商户证书
        if not mch_id:
            try:
                _ac_conn = get_db()
                _ac = _ac_conn.cursor()
                _ac.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id DESC LIMIT 1")
                _ar = _ac.fetchone()
                if _ar:
                    mch_id = _ar['mch_id']
                _ac.close()
                _ac_conn.close()
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
        if content is not None:
            reply_content = content
        elif _refunded:
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
            
            # 投诉完成后调用complete API标记已处理（complete_now=False 时暂不结案，由调度器在退款后处理）
            if not complete_now:
                logger.info('[auto_reply] complete_now=False，暂不结案 complaint_id=%s', complaint_id)
            else:
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
            # 兜底: 投诉对应订单已退款则补拉白(防 refund 分支漏拉)
            try:
                from helpers import grant_complaint_whitelist
                import re as _re
                _gconn = get_db()
                _gcur = _gconn.cursor()
                _gcur.execute("SELECT order_no, user_phone FROM complaints WHERE wx_complaint_id=%s LIMIT 1", (str(complaint_id),))
                _cr = _gcur.fetchone()
                if _cr is None and _re.fullmatch(r'\d{1,9}', str(complaint_id)):
                    _gcur.execute("SELECT order_no, user_phone FROM complaints WHERE id=%s LIMIT 1", (int(complaint_id),))
                    _cr = _gcur.fetchone()
                if _cr and _cr['order_no']:
                    _gcur.execute("SELECT o.id, o.user_phone, cb.location_id FROM orders o JOIN cabinets cb ON cb.id = o.cabinet_id WHERE o.order_no=%s AND o.refund_status IN ('refunded','success') LIMIT 1", (_cr['order_no'],))
                    _or = _gcur.fetchone()
                    if _or:
                        grant_complaint_whitelist(phone=_or[1] or '', location_id=_or[2])
                _gconn.close()
            except Exception as _e6:
                logger.warning('[auto_complete] 补拉白失败: %s', _e6)
            return True
        else:
            logger.warning('[auto_complete] 完成失败 complaint_id=%s status=%s resp=%s', complaint_id, resp.status_code, resp.text[:300])
            return False
    except Exception as e:
        logger.error('[auto_complete] 异常 complaint_id=%s err=%s', complaint_id, e)
        return False



def _query_door_status(device_id, board_no, lock_no, protocol):
    import json as _j
    from helpers import connected_devices as _cd
    request_id = str(uuid.uuid4())
    with _door_status_lock:
        _door_status_results[request_id] = {'result': None, 'event': threading.Event()}
    # DB 注册(多worker共享)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO door_status_queries (request_id, result) VALUES (%s, NULL) ON CONFLICT (request_id) DO NOTHING', (request_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning('[door_status] db register failed: %s', e)
    direct_ws = False
    ws_sent = False
    cmd = {'type': 'query_door_status', 'request_id': request_id, 'board_no': board_no, 'lock_no': lock_no, 'protocol': protocol}
    if device_id in _cd:
        try:
            _cd[device_id].send(_j.dumps(cmd))
            ws_sent = True
            direct_ws = True
            logger.info('[door_status] WS sent: device=%s req=%s', device_id, request_id)
        except Exception as e:
            logger.warning('[door_status] WS send failed: %s', e)
    if not ws_sent:
        try:
            import urllib.request as _req
            _body = _j.dumps({'device_id': device_id, 'command': cmd}).encode()
            _req.urlopen('http://127.0.0.1:5004/send', data=_body, timeout=3)
            ws_sent = True
            logger.info('[door_status] ws_proxy sent: device=%s req=%s', device_id, request_id)
        except Exception as e:
            logger.warning('[door_status] ws_proxy send failed: %s, fallback to polling', e)
    # 仅当WS推送失败时才插poll兜底, 避免双通道导致设备重复执行/上报混乱
    if not ws_sent:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT id FROM cabinets WHERE mainboard_device_id=%s', (device_id,))
            cab = cur.fetchone()
            if cab:
                poll_cmd = {'type': 'query_lock_status', 'request_id': request_id, 'board_no': board_no, 'lock_no': lock_no, 'protocol': protocol}
                cur.execute("INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, status, delivered) VALUES (%s,%s,%s,'pending',0)", (device_id, cab['id'], _j.dumps(poll_cmd)))
                conn.commit()
                logger.info('[door_status] poll cmd queued (WS失败兜底): device=%s req=%s', device_id, request_id)
            conn.close()
        except Exception as e:
            logger.error('[door_status] poll fallback failed: %s', e)
            with _door_status_lock:
                _door_status_results.pop(request_id, None)
            raise
    with _door_status_lock:
        evt = _door_status_results.get(request_id, {}).get('event')
    deadline = time.time() + (8 if direct_ws else 70)
    result = None
    while time.time() < deadline:
        if evt and evt.is_set():
            with _door_status_lock:
                result = _door_status_results.pop(request_id, {}).get('result')
            break
        # DB 轮询(跨worker结果共享)
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT result FROM door_status_queries WHERE request_id=%s AND result IS NOT NULL', (request_id,))
            row = cur.fetchone()
            conn.close()
            if row and row['result']:
                r = row['result']
                result = r if isinstance(r, dict) else _j.loads(r)
                break
        except Exception:
            pass
        time.sleep(0.5)
    with _door_status_lock:
        _door_status_results.pop(request_id, None)
    # 清理DB残留
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM door_status_queries WHERE request_id=%s', (request_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return result


@bp.route('/admin/device/query-door-status', methods=['POST'])
def admin_query_door_status():
    data = request.get_json()
    device_id = data.get('device_id', '')
    board_no = data.get('board_no', 1)
    lock_no = data.get('lock_no', 1)
    protocol = data.get('protocol', 'YBM')
    if not device_id:
        return json_response(message='device_id required', code=400)
    try:
        result = _query_door_status(device_id, board_no, lock_no, protocol)
        if result:
            return json_response(data=result)
        return json_response(message='query timeout', code=504)
    except Exception as e:
        return json_response(message='send failed: ' + str(e), code=500)
@bp.route('/admin/device/slot-status', methods=['POST'])
def admin_slot_status():
    data = request.get_json(silent=True) or {}
    slot_id = data.get('slot_id')
    if not slot_id:
        return json_response(code=400, message='缺少参数')
    db = get_db()
    try:
        slot = db.execute('SELECT cs.*, c.mainboard_device_id, c.mainboard_source, c.last_heartbeat FROM cabinet_slots cs LEFT JOIN cabinets c ON cs.cabinet_id = c.id WHERE cs.id=?', (slot_id,)).fetchone()
        if not slot:
            return json_response(code=404, message='柜门不存在')
        status_map = {1: '空闲', 2: '占用', 3: '故障', 4: '锁定'}
        status_text = status_map.get(slot['status'], '未知')
        from helpers import is_device_online
        dev_id = slot['mainboard_device_id']
        online = bool(dev_id) and is_device_online(dev_id, slot.get('last_heartbeat'))
        board_no = slot.get('board_no') or 1
        lock_no = slot.get('lock_no') or slot.get('slot_number') or 1
        protocol = slot.get('mainboard_source') or 'YBM'
        result = None
        if online:
            try:
                result = _query_door_status(dev_id, int(board_no), int(lock_no), protocol)
            except Exception as e:
                logger.error(f'物理查询异常: {e}')
        if result and result.get('query_success'):
            physical_text = '开' if result.get('is_open') else '关'
            message = f"物理状态：{physical_text}；柜格状态：{status_text}"
            data = {
                'status': physical_text,
                'slot_label': slot.get('slot_label') or slot.get('slot_number', ''),
                'device_online': True,
                'detail': message,
                'message': message,
                'is_open': bool(result.get('is_open')),
                'query_success': True
            }
        elif online:
            message = f"物理查询超时；柜格状态：{status_text}(设备在线)"
            data = {
                'status': status_text,
                'slot_label': slot.get('slot_label') or slot.get('slot_number', ''),
                'device_online': True,
                'detail': message,
                'message': message,
                'query_success': False
            }
        else:
            detail = status_text + '(设备离线)'
            data = {
                'status': status_text,
                'slot_label': slot.get('slot_label') or slot.get('slot_number', ''),
                'device_online': False,
                'detail': detail,
                'message': detail,
                'query_success': False
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
    import json as _json
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT version_name, version_code, download_url, COALESCE(file_md5, '') as file_md5 FROM apk_version ORDER BY version_code DESC LIMIT 1")
        apk = cur.fetchone()
        if not apk:
            db.close()
            return json_response(code=400, message='未找到APK版本信息')
        cur.execute('SELECT id, app_version_code, last_heartbeat FROM cabinets WHERE mainboard_device_id=%s', (device_id,))
        cab = cur.fetchone()
        if not cab:
            db.close()
            return json_response(code=404, message='设备不存在')
        from helpers import is_device_online
        if not is_device_online(device_id, cab.get('last_heartbeat')):
            db.close()
            return json_response(code=400, message='设备离线，无法更新')
        if cab.get('app_version_code') is not None and int(cab['app_version_code']) >= int(apk['version_code']):
            db.close()
            return json_response(code=400, message='设备已是最新版本，无需更新')
        cur.execute("SELECT id FROM pending_lock_cmds WHERE device_id=%s AND (delivered=0 OR status='pending') AND strpos(command,'force_update')>0 AND created_at > NOW() - INTERVAL '10 minutes' LIMIT 1", (device_id,))
        if cur.fetchone():
            db.close()
            return json_response(code=400, message='该设备已有待执行的更新指令')
        supersede_force_update_cmds(cur, device_id)
        msg = {
            'type': 'force_update',
            'download_url': apk['download_url'],
            'version_name': apk['version_name'],
            'version_code': apk['version_code'],
            'force': True,
            'file_md5': apk['file_md5']
        }
        cmd_json = _json.dumps(msg)
        cur.execute('INSERT INTO pending_lock_cmds (device_id, cabinet_id, command, delivered) VALUES (%s, %s, %s, 0)',
                    (device_id, cab['id'], cmd_json))
        db.commit()
        db.close()
        logger.info(f'[PUSH_UPDATE] cmd inserted for {device_id}: {msg}')
    except Exception as e:
        logger.error(f'[PUSH_UPDATE] DB insert failed: {e}')
        return json_response(code=500, message=f'写入命令失败: {e}')
    return json_response(data={'sent': msg})


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
def _fetch_and_update_complaint(complaint_id, mch_id, cert_serial_no, cert_key_path):
    """调用微信询询技技夹计详情并更新本郑记"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64, subprocess

        with open(cert_key_path) as f:
            pk = serialization.load_pem_private_key(f.read().encode(), password=None)

        _cert_path = cert_key_path.replace('_key.pem', '_cert.pem')
        r = subprocess.run(['openssl', 'x509', '-in', _cert_path, '-serial', '-noout'], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout:
            logger.warning(f'[fetch_complaint] 证书应序列号获取失败 {complaint_id}')
            return
        serial_no = r.stdout.strip().split('=')[1]

        ts = str(int(time.time()))
        nonce = 'fetch_c'
        url_path = f'/v3/merchant-service/complaints-v2/{complaint_id}'
        msg = f'GET\n{url_path}\n{ts}\n{nonce}\n\n'
        sig = base64.b64encode(pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        auth = f'WECHATPAY2-SHA256-RSA2048 mchid="{mch_id}",nonce_str="{nonce}",timestamp="{ts}",serial_no="{serial_no}",signature="{sig}"'

        resp = requests.get(f'https://api.mch.weixin.qq.com{url_path}', headers={"Authorization": auth, "Content-Type": 'application/json'}, timeout=15)

        if resp.status_code != 200:
            logger.warning(f'[fetch_complaint] API失败 {complaint_id}: {resp.status_code}')
            return

        data = resp.json()
        conn = get_db()
        c = conn.cursor()
        updates = []; params = []

        phone_val = data.get('payer_phone', '') or ''
        if phone_val:
            updates.append('user_phone=%s')
            params.append(phone_val)

        order_val = data.get('out_trade_no', '') or ''
        if order_val:
            updates.append('order_no=%s')
            params.append(order_val)
            _oc_conn = get_db()
            _oc = _oc_conn.cursor()
            _oc.execute('SELECT id FROM orders WHERE order_no=%s LIMIT 1', (order_val,))
            _or = _oc.fetchone()
            if _or:
                updates.append('order_id=%s')
                params.append(_or[0])
            _oc.close()
            _oc_conn.close()

        mch_val = data.get('complainted_mchid', '') or ''
        if mch_val:
            updates.append('mch_id=%s')
            params.append(mch_val)
        txn_list = data.get('complaint_order_info', [])
        if txn_list and txn_list[0].get('transaction_id'):
            updates.append('transaction_id=%s')
            params.append(txn_list[0]['transaction_id'])

        detail_val = data.get('complaint_detail', '') or ''
        if detail_val:
            updates.append('content=%s')
            params.append(detail_val)

        if updates:
            params.append(complaint_id)
            c.execute(f'UPDATE complaints SET ' + ', '.join(updates) + ' WHERE wx_complaint_id=%s', params)
            conn.commit()
            logger.info(f'[fetch_complaint] 更新投诉 {complaint_id}: phone={phone_val} order={order_val}')

        conn.close()
    except Exception as e:
        logger.error(f'[fetch_complaint] 异常 {complaint_id}: {e}')

_nonwechat_refund_retry = {}
_NONWECHAT_REFUND_RETRY_LIMIT = 3


def _bump_nonwechat_retry(cid2, msg2):
    """失败投诉有限重试：超过上限后标记人工处理，停止自动重试。"""
    cnt = _nonwechat_refund_retry.get(cid2, 0) + 1
    _nonwechat_refund_retry[cid2] = cnt
    if cnt >= _NONWECHAT_REFUND_RETRY_LIMIT:
        logger.warning("[complaint_scheduler] 退款失败次数达上限，停止自动重试 id=%s msg=%s", cid2, msg2)
        try:
            conn_done = get_db()
            cur_done = conn_done.cursor()
            cur_done.execute("UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s",
                             ("自动退款失败，已停止自动重试，需人工处理", cid2))
            conn_done.commit()
            conn_done.close()
        except Exception as _e:
            logger.error("[complaint_scheduler] 标记投诉人工处理失败: %s", _e)
    else:
        logger.warning("[complaint_scheduler] 退款失败 id=%s msg=%s retry=%s/%s", cid2, msg2, cnt, _NONWECHAT_REFUND_RETRY_LIMIT)


def _bump_transient_retry(cid2, msg2):
    """瞬时错误（余额不足/操作过于频繁）：保持待处理并按 reply_time 冷却后自动重试，不永久标红。"""
    try:
        _tconn = get_db()
        _tcur = _tconn.cursor()
        _tcur.execute("UPDATE complaints SET status='0', reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s",
                      ('自动退款失败，稍后自动重试', cid2))
        _tconn.commit()
        _tconn.close()
        logger.info("[complaint_scheduler] transient refund fail, will retry later id=%s msg=%s", cid2, msg2)
    except Exception as _te:
        logger.error("[complaint_scheduler] mark transient retry error: %s", _te)


_COMPLAINT_SCHEDULER_LOCK_FILE = "/tmp/complaint_scheduler.lock"


def _complaint_scheduler():
    """后台线程：每30秒扫描未处理的微信投诉（status=0），进行退款+回复"""
    import time
    while True:
        lock_fd = None
        conn = None
        try:
            import fcntl
            lock_fd = open(_COMPLAINT_SCHEDULER_LOCK_FILE, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock_fd.close()
                lock_fd = None
                time.sleep(30)
                continue
            # 定期清红：订单已退款但投诉仍显示“自动退款失败”
            try:
                _clean_conn = get_db()
                _clean_cur = _clean_conn.cursor()
                _clean_cur.execute("""
                    UPDATE complaints c
                    SET reply='订单已退款，无需重复退款', reply_time=CURRENT_TIMESTAMP
                    FROM orders o
                    WHERE c.order_no = o.order_no
                      AND c.status='2'
                      AND (POSITION('自动退款失败' IN c.reply) > 0 OR POSITION('退款失败' IN c.reply) > 0)
                      AND o.status=4
                      AND COALESCE(o.refund_status,'') IN ('refunded','success')
                """)
                _clean_rows = _clean_cur.rowcount
                _clean_conn.commit()
                _clean_conn.close()
                if _clean_rows:
                    logger.info("[complaint_scheduler] stale red cleared count=%s", _clean_rows)
            except Exception as _ce:
                logger.error("[complaint_scheduler] clear stale red error: %s", _ce)
            # 定期同步: 订单已退但投诉仍标退款失败/转人工的, 自动置完成(投诉状态与订单一致)
            try:
                _sync_conn = get_db()
                _sync_cur = _sync_conn.cursor()
                _sync_cur.execute("""
                    UPDATE complaints c
                    SET status='3', refund_status='refunded', reply=COALESCE(reply,'已退款'),
                        reply_time=CURRENT_TIMESTAMP
                    FROM orders o
                    WHERE c.order_no = o.order_no
                      AND c.type = 'wechat'
                      AND c.status IN ('1','2')
                      AND COALESCE(o.refund_status,'') IN ('refunded','success')
                """)
                _sync_rows = _sync_cur.rowcount
                _sync_conn.commit()
                _sync_conn.close()
                if _sync_rows:
                    logger.info("[complaint_scheduler] complaint synced with refunded order count=%s", _sync_rows)
            except Exception as _se:
                logger.error("[complaint_scheduler] sync complaint status error: %s", _se)
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT * FROM complaints WHERE status IN ('0','1','2') AND type IN ('wechat') AND created_at < NOW() - INTERVAL '2 minutes' AND created_at > NOW() - INTERVAL '7 days' ORDER By created_at LIMIT 100")
            rows = c.fetchall()
            conn.close()
            conn = None
            for row in rows:
                comp = dict(row)
                cid = comp.get("id", 0)
                wxid = comp.get("wx_complaint_id", "")
                ono = comp.get("order_no", "")
                cstatus = comp.get("status", "0")
                logger.info("[complaint_scheduler] 处理投诉 id=%s wx_id=%s status=%s", cid, wxid, cstatus)
                if cstatus == "0":
                    # 回复兜底：回调时已秒发首响，这里补发仍未回复的（只回复，不退款不结案）
                    _txn = ''; _up = comp.get('user_phone', '') or ''
                    if ono:
                        try:
                            _tc_conn = get_db()
                            _tc = _tc_conn.cursor()
                            _tc.execute("SELECT transaction_id FROM orders WHERE order_no=%s LIMIT 1", (ono,))
                            _tr = _tc.fetchone()
                            if _tr and _tr[0]: _txn = _tr[0]
                            _tc.close()
                            _tc_conn.close()
                        except:
                            pass
                    cmch = comp.get('mch_id', '') or ''
                    ccert = WX_CERT_SERIAL_NO
                    ckey = WX_KEY_PATH
                    if cmch:
                        try:
                            c3_conn = get_db()
                            c3 = c3_conn.cursor()
                            c3.execute('SELECT cert_serial_no, cert_name FROM payment_channels WHERE mch_id=%s ', (cmch,))
                            pc = c3.fetchone()
                            if pc:
                                ccert = pc[0]
                                ckey = f'/home/ubuntu/smart-locker/cert/{pc[1]}_key.pem'
                            c3.close()
                            c3_conn.close()
                        except:
                            pass
                    _auto_reply_complaint(wxid, order_no=ono, transaction_id=_txn, mch_id=cmch, cert_serial=ccert, private_key_path=ckey, content=WECHAT_FIRST_REPLY, complete_now=False)
                elif cstatus in ("1", "2"):
                    # 已回复(1)或转人工(2): 满5分钟后执行退款 → 到账通知 → 结案；失败每5分钟重试，3次后转人工
                    # status=2(转人工)也继续自动重试: 商户充值后自动退掉, 无需人工盯
                    _age = 0.0
                    try:
                        _ag_conn = get_db()
                        _ag = _ag_conn.cursor()
                        _ag.execute("SELECT EXTRACT(EPOCH FROM (NOW()-created_at)) FROM complaints WHERE id=%s", (cid,))
                        _ag_row = _ag.fetchone()
                        _ag_conn.close()
                        if _ag_row and _ag_row[0]:
                            _age = float(_ag_row[0])
                    except:
                        pass
                    if _age < 300:
                        continue  # 未满5分钟，等下轮
                    _txn = ''
                    if ono:
                        try:
                            _tc_conn = get_db()
                            _tc = _tc_conn.cursor()
                            _tc.execute("SELECT transaction_id FROM orders WHERE order_no=%s LIMIT 1", (ono,))
                            _tr = _tc.fetchone()
                            if _tr and _tr[0]: _txn = _tr[0]
                            _tc.close()
                            _tc_conn.close()
                        except:
                            pass
                    cmch = comp.get('mch_id', '') or ''
                    ccert = WX_CERT_SERIAL_NO
                    ckey = WX_KEY_PATH
                    if cmch:
                        try:
                            c3_conn = get_db()
                            c3 = c3_conn.cursor()
                            c3.execute('SELECT cert_serial_no, cert_name FROM payment_channels WHERE mch_id=%s', (cmch,))
                            pc = c3.fetchone()
                            if pc:
                                ccert = pc[0]
                                ckey = f'/home/ubuntu/smart-locker/cert/{pc[1]}_key.pem'
                            c3.close()
                            c3_conn.close()
                        except:
                            pass
                    _cmch = cmch
                    if not _cmch:
                        try:
                            _fc_conn = get_db()
                            _fc = _fc_conn.cursor()
                            _fc.execute("SELECT mch_id FROM payment_channels WHERE is_active=1 ORDER BY id DESC LIMIT 1")
                            _fr = _fc.fetchone()
                            if _fr:
                                _cmch = _fr['mch_id']
                            _fc.close()
                            _fc_conn.close()
                        except:
                            pass
                    refund_ok, refund_msg = _auto_refund_complaint_order(ono, transaction_id=_txn, complaint_id=cid, payer_phone=comp.get('user_phone', '') or '')
                    if refund_ok:
                        if refund_msg in ('订单已退款，无需重复退款', '无押金', '无可退金额'):
                            # 无需退款，直接结案
                            if _cmch:
                                _auto_complete_complaint(wxid, _cmch, ccert, ckey)
                            _u_conn = get_db()
                            _u = _u_conn.cursor()
                            _u.execute("UPDATE complaints SET status=3, refund_status='refunded', reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s", (WECHAT_NO_REFUND, cid))
                            _u_conn.commit()
                            _u_conn.close()
                        else:
                            # 退款成功：发到账通知 → 结案
                            _amt = 0.0
                            try:
                                _am_conn = get_db()
                                _am = _am_conn.cursor()
                                _am.execute("SELECT deposit_amount FROM orders WHERE order_no=%s LIMIT 1", (ono,))
                                _am_row = _am.fetchone()
                                _am_conn.close()
                                if _am_row and _am_row[0]:
                                    _amt = float(_am_row[0])
                            except:
                                pass
                            notice = WECHAT_ARRIVAL_NOTICE.format(amount='%.2f' % _amt)
                            _auto_reply_complaint(wxid, order_no=ono, transaction_id=_txn, mch_id=cmch, cert_serial=ccert, private_key_path=ckey, content=notice, complete_now=False)
                            if _cmch:
                                _auto_complete_complaint(wxid, _cmch, ccert, ckey)
                            _u_conn = get_db()
                            _u = _u_conn.cursor()
                            _u.execute("UPDATE complaints SET status=3, refund_status='refunded', reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s", (notice, cid))
                            _u_conn.commit()
                            _u_conn.close()
                    else:
                        # 退款失败：计数重试，3次后转人工（不再自动结案）
                        _u_conn = get_db()
                        _u = _u_conn.cursor()
                        _u.execute("UPDATE complaints SET refund_status='refund_failed', refund_fail_reason=%s, refund_retry=COALESCE(refund_retry,0)+1 WHERE id=%s", (refund_msg[:300], cid))
                        _u_conn.commit()
                        _u.execute("SELECT refund_retry FROM complaints WHERE id=%s", (cid,))
                        _rt = _u.fetchone()
                        _u_conn.close()
                        _retry_n = int(_rt[0]) if _rt and _rt[0] else 0
                        logger.warning('[complaint_scheduler] 退款失败 id=%s msg=%s 第%s次', cid, refund_msg, _retry_n)
                        if _retry_n >= 3:
                            if _cmch:
                                _auto_reply_complaint(wxid, order_no=ono, transaction_id=_txn, mch_id=cmch, cert_serial=ccert, private_key_path=ckey, content=WECHAT_MANUAL_REPLY, complete_now=False)
                            _u_conn = get_db()
                            _u = _u_conn.cursor()
                            _u.execute("UPDATE complaints SET status=2, reply=%s, reply_time=CURRENT_TIMESTAMP WHERE id=%s", (WECHAT_MANUAL_REPLY, cid))
                            _u_conn.commit()
                            _u_conn.close()

            # non-wechat auto complaints (with refund if has order)
            try:
                conn3 = get_db()
                c3 = conn3.cursor()
                c3.execute("SELECT * FROM complaints WHERE status IN ('0','1') AND (type!='wechat' OR type IS NULL) AND created_at < NOW() - INTERVAL '2 minutes' AND NOT (POSITION('稍后自动重试' IN COALESCE(reply,'')) > 0 AND reply_time > NOW() - INTERVAL '30 minutes') ORDER BY id LIMIT 100")
                rows2 = c3.fetchall()
                conn3.close()
                for row2 in rows2:
                    comp2 = dict(row2)
                    cid2 = comp2.get("id", 0)
                    ono2 = comp2.get("order_no", "")
                    phone2 = comp2.get("user_phone", "")
                    src2 = comp2.get("source") or ""
                    openid2 = comp2.get("openid") or ""
                    logger.info("[complaint_scheduler] non-wechat complaint id=%s phone=%s order=%s source=%s", cid2, phone2, ono2, src2)

                    received_reply = '\u60a8\u597d\uff0c\u60a8\u7684\u6295\u8bc9\u5df2\u6536\u5230\uff0c\u6211\u4eec\u4f1a\u5c3d\u5feb\u5904\u7406\u3002\u5982\u6709\u7d27\u6025\u60c5\u51b5\u8bf7\u8054\u7cfb\u5ba2\u670d\uff0c\u611f\u8c22\u60a8\u7684\u7406\u89e3\u4e0e\u652f\u6301\uff01'
                    already_reply = '\u8ba2\u5355\u5df2\u9000\u6b3e\uff0c\u65e0\u9700\u91cd\u590d\u9000\u6b3e'

                    def _finish_nonwechat(cid, reply_text):
                        _fd = get_db()
                        _fc = _fd.cursor()
                        _fc.execute("UPDATE complaints SET reply=%s, status='2', reply_time=CURRENT_TIMESTAMP WHERE id=%s", (reply_text, cid))
                        _fd.commit()
                        _fd.close()

                    is_mp_msg = src2 == 'wechat_mp' or openid2.startswith('oLhbm')
                    if not ono2:
                        if is_mp_msg and phone2:
                            try:
                                _mo_conn = get_db()
                                _mo_cur = _mo_conn.cursor()
                                _mo_cur.execute("SELECT order_no FROM orders WHERE user_phone=%s AND status IN (2,3) AND COALESCE(refund_status,'') NOT IN ('success','refunded') AND COALESCE(refund_id,'') = '' AND COALESCE(transaction_id,'') != '' ORDER BY id DESC LIMIT 1", (phone2,))
                                _mo_row = _mo_cur.fetchone()
                                if _mo_row and _mo_row[0]:
                                    ono2 = _mo_row[0]
                                    _mo_cur.execute("UPDATE complaints SET order_no=%s WHERE id=%s", (ono2, cid2))
                                    logger.info("[complaint_scheduler] matched order by phone id=%s phone=%s order=%s", cid2, phone2, ono2)
                                _mo_conn.commit()
                                _mo_conn.close()
                            except Exception as _me:
                                logger.error("[complaint_scheduler] match order by phone error: %s", _me)
                                try:
                                    _mo_conn.close()
                                except Exception:
                                    pass
                        if not ono2:
                            logger.info("[complaint_scheduler] no order_no, no auto refund id=%s", cid2)
                            _finish_nonwechat(cid2, received_reply)
                            continue

                    try:
                        _precheck_conn = get_db()
                        _precheck_cur = _precheck_conn.cursor()
                        _precheck_cur.execute("SELECT status, refund_status, refund_id FROM orders WHERE order_no=%s LIMIT 1", (ono2,))
                        _precheck = _precheck_cur.fetchone()
                        _precheck_conn.close()
                        if _precheck and (_precheck[0] == 4 or (_precheck[1] in ('success', 'refunded')) or _precheck[2]):
                            try:
                                _wa2_conn = get_db()
                                _wa2_cur = _wa2_conn.cursor()
                                _wa2_cur.execute("""UPDATE withdrawal_records SET status=2, approver='投诉自动退款', approve_time=CURRENT_TIMESTAMP WHERE order_id=(SELECT id FROM orders WHERE order_no=%s LIMIT 1) AND status='0'""", (ono2,))
                                _wa2_conn.commit()
                                _wa2_conn.close()
                            except Exception:
                                pass
                            _finish_nonwechat(cid2, already_reply)
                            continue
                    except:
                        pass

                    try:
                        _dc = get_db()
                        _dcur = _dc.cursor()
                        if ono2:
                            _dcur.execute("""
                                SELECT w.status FROM withdrawal_records w
                                WHERE w.user_phone=%s AND w.status IN (0,1,2)
                                  AND EXISTS (SELECT 1 FROM orders o WHERE o.order_no=%s
                                              AND (w.order_id=o.id OR POSITION(('"' || o.id::text || '"') IN COALESCE(w.order_ids,''))>0))
                                LIMIT 1
                            """, (phone2, ono2))
                        else:
                            _dcur.execute("SELECT status FROM withdrawal_records WHERE user_phone=%s AND status IN (0,1,2) LIMIT 1", (phone2,))
                        _dup = _dcur.fetchone()
                        _dc.close()
                        _dup_status = str(_dup[0]) if _dup else ''
                        if _dup_status == '2':
                            logger.info("[complaint_scheduler] order %s withdrawal already approved, mark complaint done id=%s", ono2, cid2)
                            _finish_nonwechat(cid2, already_reply)
                            continue
                        if _dup_status == '1':
                            logger.info("[complaint_scheduler] order %s withdrawal refunding, finish as received id=%s", ono2, cid2)
                            _finish_nonwechat(cid2, received_reply)
                            continue
                    except:
                        pass

                    _claim_conn = get_db()
                    _claim_cur = _claim_conn.cursor()
                    _claim_cur.execute("UPDATE complaints SET status='1' WHERE id=%s AND status IN ('0','1') RETURNING id", (cid2,))
                    _claimed = _claim_cur.fetchone()
                    _claim_conn.commit()
                    _claim_conn.close()
                    if not _claimed:
                        continue

                    refund_ok2, refund_msg2 = _auto_refund_complaint_order(ono2, transaction_id="", complaint_id="", payer_phone=phone2)
                    if refund_ok2:
                        if str(refund_msg2) == already_reply:
                            _finish_nonwechat(cid2, already_reply)
                        elif str(refund_msg2) in ('无押金', '无可退金额'):
                            _finish_nonwechat(cid2, received_reply)
                        else:
                            _finish_nonwechat(cid2, '已自动原路退款')
                        if _dup_status == '0':
                            try:
                                _wa_conn = get_db()
                                _wa_cur = _wa_conn.cursor()
                                _wa_cur.execute("""UPDATE withdrawal_records SET status=2, approver='投诉自动退款', approve_time=CURRENT_TIMESTAMP WHERE order_id=(SELECT id FROM orders WHERE order_no=%s LIMIT 1) AND status='0'""", (ono2,))
                                _wa_conn.commit()
                                _wa_conn.close()
                                logger.info("[complaint_scheduler] pending withdrawal auto-approved order=%s id=%s", ono2, cid2)
                            except Exception as _we:
                                logger.error("[complaint_scheduler] auto approve withdrawal error: %s", _we)
                        logger.info("[complaint_scheduler] non-wechat refund ok id=%s order=%s msg=%s", cid2, ono2, refund_msg2)
                        continue

                    fail_text2 = str(refund_msg2)
                    if ('\u5df2\u5168\u989d\u9000\u6b3e' in fail_text2 or '\u8bb0\u5f55\u4e0d\u5b58\u5728' in fail_text2
                            or 'ORDERNOTEXIST' in fail_text2 or '\u8ba2\u5355\u4e0d\u5b58\u5728' in fail_text2
                            or '\u672a\u627e\u5230\u5bf9\u5e94\u8ba2\u5355' in fail_text2):
                        _finish_nonwechat(cid2, already_reply if '\u5df2\u5168\u989d\u9000\u6b3e' in fail_text2 else received_reply)
                        continue

                    logger.warning("[complaint_scheduler] non-wechat refund fail id=%s order=%s phone=%s msg=%s", cid2, ono2, phone2, refund_msg2)
                    _reset_conn = get_db()
                    _reset_cur = _reset_conn.cursor()
                    _reset_cur.execute("UPDATE complaints SET status='0' WHERE id=%s AND status='1'", (cid2,))
                    _reset_conn.commit()
                    _reset_conn.close()
                    if ('余额不足' in fail_text2 or '操作过于频繁' in fail_text2 or '请稍后再试' in fail_text2):
                        _bump_transient_retry(cid2, refund_msg2)
                    else:
                        _bump_nonwechat_retry(cid2, refund_msg2)
            except Exception as e2:
                logger.error("[complaint_scheduler] non-wechat error: %s", e2, exc_info=True)
                try:
                    conn3.close()
                except:
                    pass

        except Exception as e:
            logger.error("[complaint_scheduler] 异常: %s", e, exc_info=True)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except:
                    pass
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    try:
                        lock_fd.close()
                    except Exception:
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
import os
from flask import request
from datetime import datetime
from database import get_db
from helpers import json_response, logger

@bp.route('/admin/historical-orders/import', methods=['POST'])
def historical_import():
    try:
        location_id = request.form.get('location_id', type=int)
        file = request.files.get('file')
        if not location_id:
            return json_response(message='请选择网点', code=400)
        if not file:
            return json_response(message='请选择文件', code=400)

        fn = file.filename.lower()
        if fn.endswith('.csv'):
            import csv, io
            _raw = file.read()
            try:
                content = _raw.decode('utf-8')
            except UnicodeDecodeError:
                content = _raw.decode('gbk')
            reader = csv.reader(io.StringIO(content))
            rows = []
            for i, row in enumerate(reader):
                if i == 0 and (row[0].strip() == 'date' or not row[0].strip()):
                    continue
                if len(row) < 3:
                    continue
                date_str = row[0].strip()
                try:
                    d = datetime.strptime(date_str, '%Y%m%d').date()
                except:
                    try:
                        d = datetime.strptime(date_str, '%Y-%m-%d').date()
                    except:
                        continue
                count = int(float(row[2].strip()))
                rows.append((location_id, d, count))
        else:
            import openpyxl
            wb = openpyxl.load_workbook(file, read_only=True)
            ws = wb.active
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                if not row or not row[0]:
                    continue
                date_str = str(row[0]).strip()
                try:
                    d = datetime.strptime(date_str, '%Y%m%d').date()
                except:
                    try:
                        d = datetime.strptime(str(row[0])[:10], '%Y-%m-%d').date()
                    except:
                        continue
                count = int(float(row[2])) if row[2] else 0
                rows.append((location_id, d, count))

        if not rows:
            return json_response(message='未解析到有效数据', code=400)

        conn = get_db()
        c = conn.cursor()
        imported = 0
        for loc_id, d, cnt in rows:
            c.execute("""
                INSERT INTO historical_order_counts (location_id, date, visible_count)
                VALUES (%s, %s, %s)
                ON CONFLICT (location_id, date) DO UPDATE SET visible_count = EXCLUDED.visible_count
            """, (loc_id, d, cnt))
            imported += 1
        conn.commit()
        conn.close()
        logger.info(f'[historical] imported {imported} records for location {location_id}')
        return json_response(data={'imported': imported}, message=f'导入成功')
    except Exception as e:
        logger.error(f'[historical import] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/historical-orders/delete', methods=['POST'])
def historical_delete():
    try:
        data = request.get_json()
        location_id = data.get('location_id')
        if not location_id:
            return json_response(message='请选择网点', code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM historical_order_counts WHERE location_id = %s', (location_id,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        logger.info(f'[historical] deleted {deleted} records for location {location_id}')
        return json_response(message=f'已删除{deleted}条记录')
    except Exception as e:
        logger.error(f'[historical delete] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/historical-orders/list', methods=['GET'])
def historical_list():
    try:
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        c = conn.cursor()
        if location_id:
            c.execute("""
                SELECT h.id, h.location_id, l.name as location_name, h.date, h.visible_count, h.created_at
                FROM historical_order_counts h
                JOIN locations l ON h.location_id = l.id
                WHERE h.location_id = %s
                ORDER BY h.date DESC
            """, (location_id,))
        else:
            c.execute("""
                SELECT h.id, h.location_id, l.name as location_name, h.date, h.visible_count, h.created_at
                FROM historical_order_counts h
                JOIN locations l ON h.location_id = l.id
                ORDER BY h.date DESC LIMIT 100
            """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json_response(data={'list': rows, 'total': len(rows)})
    except Exception as e:
        logger.error(f'[historical list] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/admin/historical-orders/setting', methods=['GET', 'POST'])
def historical_setting():
    try:
        conn = get_db()
        c = conn.cursor()
        if request.method == 'POST':
            data = request.get_json() or {}
            enabled = data.get('enabled', False)
            c.execute("""
                INSERT INTO system_configs (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = %s
            """, ('show_history_enabled', '1' if enabled else '0', '1' if enabled else '0'))
            conn.commit()
            conn.close()
            return json_response(message='设置已保存')
        else:
            c.execute("SELECT value FROM system_configs WHERE key = %s", ('show_history_enabled',))
            row = c.fetchone()
            conn.close()
            enabled = (row and row[0] == '1')
            return json_response(data={'enabled': enabled})
    except Exception as e:
        logger.error(f'[historical setting] {e}')
        return json_response(message=str(e), code=500)


# ==================== 微信交易对账单自动同步 ====================
_TRADE_BILL_SYNC_LOCK_FILE = "/tmp/trade_bill_sync.lock"
_TRADE_BILL_SYNC_INTERVAL = 6 * 3600


def _trade_bill_sync_scheduler():
    time.sleep(600)
    while True:
        lock_fd = None
        try:
            import fcntl
            lock_fd = open(_TRADE_BILL_SYNC_LOCK_FILE, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                lock_fd.close()
                lock_fd = None
            if lock_fd is not None:
                try:
                    import pull_trade_bills as _tb
                    conn = _tb._connect()
                    with conn.cursor() as cur:
                        cur.execute(_tb.CREATE_TABLE_SQL)
                    conn.commit()
                    channels = _tb.channels_with_cert(conn)
                    end_date = datetime.now().date() - timedelta(days=1)
                    start_date = end_date - timedelta(days=2)
                    for ch in channels:
                        _tb.sync_mch(conn, ch['mch_id'], ch['cert_serial_no'], ch['cert_name'], start_date, end_date)
                    conn.close()
                    logger.info('[trade_bill_sync] done channels=%s', len(channels))
                except Exception as e:
                    logger.error('[trade_bill_sync] %s', e)
        except Exception as e:
            logger.error('[trade_bill_sync] lock error %s', e)
        finally:
            if lock_fd is not None:
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass
        time.sleep(_TRADE_BILL_SYNC_INTERVAL)


if os.path.isdir('/tmp'):
    _trade_bill_sync_thread = threading.Thread(target=_trade_bill_sync_scheduler, daemon=True)
    _trade_bill_sync_thread.start()
