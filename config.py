"""
智能寄存柜系统 - 全局配置
"""
import os

# ============================================
# 基础配置
# ============================================
SECRET_KEY = 'smart-locker-secret-key-2024'
DATABASE = 'locker.db'

# PostgreSQL config
DATABASE_URL = None
DATABASE_URL = 'postgresql://locker_admin:locker_pass_2024@127.0.0.1:6432/smart_locker'
DEBUG = False
HOST = '127.0.0.1'
PORT = 5001

# ============================================
# 微信支付配置
# ============================================
WX_MCH_ID = '1747762575'  # 默认回退商户号（已受限），优先使用数据库动态选择
WX_API_KEY = 'lichengju0904LICHENGJU0904libaip'
WX_API_V3_KEY = 'lichengju0904LICHENGJU0904libaip'
WX_CERT_SERIAL_NO = '73AB063E7593B2FC5DDF37C0F6A269826675D119'
WX_APP_ID = 'wxd85204d0ec930d46'
WX_APP_SECRET = '552e27fa9a260a6640bf6983bd3470f5'
WX_MP_APP_ID = 'wx57eaea52dcfff4e8'
WX_MP_APP_SECRET = 'eac6e21d37bf5621730633d4e249275b'
WX_MP_TOKEN = 'smartlocker2024'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WX_CERT_PATH = os.path.join(BASE_DIR, 'cert', 'apiclient_cert.pem')
WX_KEY_PATH = os.path.join(BASE_DIR, 'cert', 'apiclient_key.pem')
WX_PAY_NOTIFY_URL = 'https://locker.cqdyxl.com/api/pay/notify'
WX_REFUND_NOTIFY_URL = 'https://locker.cqdyxl.com/api/refund/notify'

# ============================================
# 主板品牌默认配置
# ============================================
BRAND_DEFAULTS = {
    'YBM': {'serial_port': 'ttyS4', 'baud_rate': 9600},
    'WT': {'serial_port': 'ttyS3', 'baud_rate': 115200},
    'QM': {'serial_port': 'ttyS2', 'baud_rate': 9600},
}

# ============================================
# APK版本信息
# ============================================
LATEST_VERSION_CODE = 201
LATEST_VERSION_NAME = "1.2.65"
AUTO_UPDATE_ENABLED = False
APK_DOWNLOAD_URL = "https://locker.cqdyxl.com/static/locker.apk"

# ============================================
# 订单隐藏配置常量
# ============================================
ORDER_HIDE_SECRET = 'smart_locker_hide_2024'
# ============================================
# PushPlus 推送配置
# ============================================
PUSHPLUS_TOKEN = '43993c7f92d14ebd8762dc08d34e6151'
