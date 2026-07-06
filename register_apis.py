"""
register_apis.py — 注册所有新 API 蓝图
"""
def register_new_api_blueprints(app):
    from api_auth import auth_bp
    from api_box import box_bp
    from api_device import device_new_bp
    from api_netpoint import netpoint_bp
    from api_order import order_bp
    app.register_blueprint(auth_bp, url_prefix='/api')
    app.register_blueprint(box_bp, url_prefix='/api')
    app.register_blueprint(device_new_bp, url_prefix='/api')
    app.register_blueprint(netpoint_bp, url_prefix='/api')
    app.register_blueprint(order_bp, url_prefix='/api')