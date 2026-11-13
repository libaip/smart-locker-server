"""
统计API - Blueprint
包含：仪表盘、订单统计、网点统计、商家统计、日报统计
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request
from database import get_db
from helpers import json_response, require_auth, logger

bp = Blueprint('stats', __name__)


@bp.route('/stats/dashboard', methods=['GET'])
@require_auth
def stats_dashboard():
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM orders WHERE DATE(created_at) = %s', (today,))
        today_orders = cursor.fetchone()['count']
        cursor.execute('SELECT COUNT(*) as count FROM cabinet_slots WHERE status = 2')
        occupied_slots = cursor.fetchone()['count']
        cursor.execute("SELECT COALESCE(SUM(amount), 0) as total FROM payments WHERE type = 1 AND status = 1 AND DATE(created_at) = %s", (today,))
        today_income = cursor.fetchone()['total']
        cursor.execute("SELECT COUNT(*) as count FROM cabinets WHERE last_heartbeat >= NOW() - INTERVAL '30 seconds'")
        online_cabinets = cursor.fetchone()['count']
        cursor.execute('SELECT COUNT(*) as count FROM cabinets')
        total_cabinets = cursor.fetchone()['count']
        conn.close()
        return json_response({'today_orders': today_orders, 'occupied_slots': occupied_slots,
                              'today_income': today_income, 'online_cabinets': online_cabinets,
                              'total_cabinets': total_cabinets})
    except Exception as e:
        logger.error(f'[stats_dashboard] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/stats/orders', methods=['GET'])
@require_auth
def stats_orders():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        agent_id = request.args.get('agent_id', type=int)
        merchant_id = request.args.get('merchant_id', type=int)
        location_id = request.args.get('location_id', type=int)
        conn = get_db()
        cursor = conn.cursor()

        cabinet_ids = None
        if location_id or merchant_id or agent_id:
            q = 'SELECT DISTINCT cab.id FROM cabinets cab JOIN locations loc ON cab.location_id = loc.id JOIN merchants mer ON loc.merchant_id = mer.id WHERE 1=1'
            p = []
            if location_id:
                q += ' AND cab.location_id = %s'
                p.append(location_id)
            if merchant_id:
                q += ' AND loc.merchant_id = %s'
                p.append(merchant_id)
            if agent_id:
                q += ' AND mer.agent_id = %s'
                p.append(agent_id)
            cursor.execute(q, p)
            cab_ids_set = [row['id'] for row in cursor.fetchall()]
            if not cab_ids_set:
                conn.close()
                return json_response({'total': 0, 'by_status': {}, 'deposit_total': 0, 'refund_total': 0, 'net_income': 0})

        where_clause = '1=1'
        params = []
        if start_date:
            where_clause += ' AND DATE(o.created_at) >= %s'
            params.append(start_date)
        if end_date:
            where_clause += ' AND DATE(o.created_at) <= %s'
            params.append(end_date)

        query = f'SELECT status, COUNT(*) as count FROM orders o WHERE {where_clause} GROUP BY status'
        cursor.execute(query, params)
        by_status = {row['status']: row['count'] for row in cursor.fetchall()}
        cursor.execute(f'SELECT COUNT(*) as total FROM orders o WHERE {where_clause}', params)
        total = cursor.fetchone()['total']
        conn.close()
        return json_response({'total': total, 'by_status': by_status})
    except Exception as e:
        logger.error(f'[stats_orders] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/stats/locations', methods=['GET'])
@require_auth
def stats_locations():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        location_id = request.args.get('location_id', type=int)
        merchant_id = request.args.get('merchant_id', type=int)
        agent_id = request.args.get('agent_id', type=int)
        conn = get_db()
        cursor = conn.cursor()
        where_clause = '1=1'
        params = []
        if start_date:
            where_clause += ' AND DATE(o.created_at) >= %s'
            params.append(start_date)
        if end_date:
            where_clause += ' AND DATE(o.created_at) <= %s'
            params.append(end_date)
        if location_id:
            where_clause += ' AND l.id = %s'
            params.append(location_id)
        if merchant_id:
            where_clause += ' AND l.merchant_id = %s'
            params.append(merchant_id)
        if agent_id:
            where_clause += ' AND m.agent_id = %s'
            params.append(agent_id)
        cursor.execute(f'SELECT l.id, l.name as location_name, m.name as merchant_name, COUNT(o.id) as order_count, SUM(CASE WHEN o.status = 2 THEN 1 ELSE 0 END) as active_count, SUM(CASE WHEN p.type = 1 AND p.status = 1 THEN p.amount ELSE 0 END) as deposit_total, SUM(CASE WHEN p.type = 2 AND p.status = 1 THEN p.amount ELSE 0 END) as refund_total, SUM(CASE WHEN p.type = 1 AND p.status = 1 THEN p.amount ELSE 0 END) - SUM(CASE WHEN p.type = 2 AND p.status = 1 THEN p.amount ELSE 0 END) as net_income FROM locations l JOIN merchants m ON l.merchant_id = m.id LEFT JOIN cabinets c ON l.id = c.location_id LEFT JOIN orders o ON c.id = o.cabinet_id LEFT JOIN payments p ON o.id = p.order_id WHERE {where_clause} GROUP BY l.id ORDER BY order_count DESC', params)
        stats = cursor.fetchall()
        conn.close()
        return json_response([dict(s) for s in stats])
    except Exception as e:
        logger.error(f'[stats_locations] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/stats/merchants', methods=['GET'])
@require_auth
def stats_merchants():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        conn = get_db()
        cursor = conn.cursor()
        where_clause = '1=1'
        params = []
        if start_date:
            where_clause += ' AND DATE(o.created_at) >= %s'
            params.append(start_date)
        if end_date:
            where_clause += ' AND DATE(o.created_at) <= %s'
            params.append(end_date)
        cursor.execute(f'SELECT m.id, m.name as merchant_name, COUNT(DISTINCT l.id) as location_count, COUNT(DISTINCT c.id) as cabinet_count, COUNT(o.id) as order_count, SUM(CASE WHEN p.type = 1 AND p.status = 1 THEN p.amount ELSE 0 END) as income FROM merchants m LEFT JOIN locations l ON m.id = l.merchant_id LEFT JOIN cabinets c ON l.id = c.location_id LEFT JOIN orders o ON c.id = o.cabinet_id LEFT JOIN payments p ON o.id = p.order_id WHERE {where_clause} GROUP BY m.id ORDER BY income DESC', params)
        stats = cursor.fetchall()
        conn.close()
        return json_response([dict(s) for s in stats])
    except Exception as e:
        logger.error(f'[stats_merchants] {e}')
        return json_response(message=str(e), code=500)


@bp.route('/stats/daily', methods=['GET'])
@require_auth
def stats_daily():
    try:
        days = request.args.get('days', 7, type=int)
        start_date_arg = request.args.get('start_date')
        end_date_arg = request.args.get('end_date')
        if start_date_arg and end_date_arg:
            start_date = start_date_arg
            end_date = end_date_arg
        else:
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            end_date = datetime.now().strftime('%Y-%m-%d')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT DATE(o.created_at) as date, COUNT(o.id) as order_count, SUM(CASE WHEN o.status = 2 THEN 1 ELSE 0 END) as active_count, SUM(CASE WHEN p.type = 1 AND p.status = 1 THEN p.amount ELSE 0 END) as deposit_total, SUM(CASE WHEN p.type = 2 AND p.status = 1 THEN p.amount ELSE 0 END) as refund_total FROM orders o LEFT JOIN payments p ON o.id = p.order_id WHERE DATE(o.created_at) >= %s AND DATE(o.created_at) <= %s GROUP BY DATE(o.created_at) ORDER BY date",
                       (start_date, end_date))
        stats = cursor.fetchall()
        conn.close()
        return json_response([dict(s) for s in stats])
    except Exception as e:
        logger.error(f'[stats_daily] {e}')
        return json_response(message=str(e), code=500)