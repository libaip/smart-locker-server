"""api_auth.py — 认证模块 POST /api/auth/login"""
import sqlite3, uuid
from flask import Blueprint, request, jsonify
auth_bp = Blueprint('auth', __name__)
def get_db():
    conn = sqlite3.connect('locker.db')
    conn.row_factory = sqlite3.Row
    return conn
@auth_bp.route('/auth/login', methods=['POST'])
def login():
    try:
        data = request.get_json(force=True)
        username = data.get('username', '')
        password = data.get('password', '')
        if username == 'admin' and password == 'admin123':
            token = str(uuid.uuid4())
            # Save token to database so require_auth can verify it
            from database import get_db
            db = get_db()
            db.execute('UPDATE admin_users SET auth_token=%s WHERE username=%s', (token, username))
            db.commit()
            db.close()
            return jsonify({'code': 0, 'msg': 'success', 'data': {'token': token}})
        else:
            return jsonify({'code': -1, 'msg': '用户名或密码错误'})
    except Exception as e:
        return jsonify({'code': -1, 'msg': str(e)})
