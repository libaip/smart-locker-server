
# ========== Extra device APIs to append ==========

@bp.route('/device/status', methods=['GET'])
def device_status():
    """设备状态查询 - APK定期调用"""
    device_id = request.args.get('device_id', '').strip()
    if not device_id:
        return jsonify({'code': 400, 'message': '缺少设备ID', 'data': None}), 400

    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT * FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if not cabinet:
            db.close()
            return jsonify({'code': 404, 'message': '设备未找到', 'data': None}), 404

        cursor.execute('SELECT * FROM mainboards WHERE cabinet_id = %s ORDER BY board_index LIMIT 1', (cabinet['id'],))
        mainboard = cursor.fetchone()

        # 查询最新APK版本
        cursor.execute('SELECT version_name, version_code, download_url FROM apk_version ORDER BY version_code DESC LIMIT 1')
        latest_apk = cursor.fetchone()

        data = {
            'device_id': device_id,
            'status': 'online',
            'serial_port': mainboard['serial_port'] if mainboard else DEFAULT_CONFIG['serial_port'],
            'baud_rate': mainboard['baud_rate'] if mainboard else DEFAULT_CONFIG['baud_rate'],
            'protocol': cabinet['mainboard_source'] or DEFAULT_CONFIG['protocol'],
            'board_start': 1,
            'board_count': cabinet['total_slots'] // 16 + 1 if cabinet['total_slots'] else 1,
            'server_url': DEFAULT_CONFIG['server_url'],
            'websocket_url': DEFAULT_CONFIG['websocket_url'],
            'store_name': cabinet['name'] or '',
            'app_version': cabinet['app_version'] or '',
            'app_version_code': cabinet['app_version_code'] or 0,
            'has_update': False,
            'latest_version': '',
            'latest_version_code': 0,
            'apk_url': ''
        }

        # 自动更新已禁用，改为管理后台手动推送（远程强制安装）
        # if latest_apk and cabinet['app_version_code'] < latest_apk['version_code']:
        #     data['has_update'] = True
        #     data['latest_version'] = latest_apk['version_name']
        #     data['latest_version_code'] = latest_apk['version_code']
        #     data['apk_url'] = latest_apk['download_url'] or '/static/smart-locker.apk'

        db.close()
        return jsonify({'code': 200, 'message': 'success', 'data': data})

    except Exception as e:
        logger.error(f'[设备状态] 查询失败: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500


@bp.route('/device/heartbeat', methods=['POST', 'GET'])
def device_heartbeat():
    """设备心跳 - APK定期上报"""
    if request.method == 'GET':
        device_id = request.args.get('device_id', '').strip()
        app_version = request.args.get('version', '')
        app_version_code = int(request.args.get('version_code', 0))
    else:
        data = request.get_json(silent=True) or {}
        device_id = data.get('device_id', '').strip()
        app_version = data.get('version', '')
        app_version_code = data.get('version_code', 0)

    if not device_id:
        return jsonify({'code': 400, 'message': '缺少设备ID', 'data': None}), 400

    try:
        from database import get_db
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT id FROM cabinets WHERE mainboard_device_id = %s', (device_id,))
        cabinet = cursor.fetchone()
        if cabinet:
            cursor.execute(
                "UPDATE cabinets SET last_heartbeat=NOW(), app_version=%s, app_version_code=%s WHERE mainboard_device_id=%s",
                (app_version, app_version_code, device_id))
            db.commit()
            db.close()
            return jsonify({'code': 200, 'message': 'ok', 'data': {'status': 'online'}})
        else:
            db.close()
            return jsonify({'code': 404, 'message': '设备未注册', 'data': None}), 404

    except Exception as e:
        logger.error(f'[设备心跳] 失败: {e}')
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500
