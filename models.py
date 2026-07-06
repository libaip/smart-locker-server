"""
智能寄存柜系统 - 数据模型常量
"""
from datetime import datetime

# ============================================
# 订单状态常量
# ============================================
ORDER_STATUS = {
    1: '待支付',
    2: '使用中',
    3: '已结算',
    4: '已退款',
    5: '已取消',
    6: '退款异常',
}

# ============================================
# 柜格状态常量
# ============================================
SLOT_STATUS = {
    1: '空闲',
    2: '使用中',
    3: '故障',
    4: '锁定',
}

# ============================================
# 营业状态常量（三态）
# ============================================
BUSINESS_STATUS_INACTIVE = 'inactive'   # 未激活
BUSINESS_STATUS_ACTIVE = 'active'       # 营业中
BUSINESS_STATUS_REPAIR = 'repair'       # 维修中

BUSINESS_STATUS_MAP = {
    'inactive': '未激活',
    'active': '营业中',
    'repair': '维修中',
}

# ============================================
# 主板品牌协议
# ============================================
MAINBOARD_BRANDS = ['YBM', 'WT', 'QM']

BRAND_PROTOCOLS = {
    'YBM': '[addr][0x4F][lockNo][xorChecksum]',
    'WT': '另一品牌协议',
    'QM': '另一品牌协议',
}

# ============================================
# 支付类型
# ============================================
PAY_TYPE_DEPOSIT = 1    # 保证金支付
PAY_TYPE_REFUND = 2     # 退款
PAY_TYPE_MANUAL = 5     # 手动开锁

# ============================================
# 提现状态
# ============================================
WITHDRAWAL_STATUS = {
    0: '待审核',
    1: '已拒绝',
    2: '已通过',
}

# ============================================
# 投诉状态
# ============================================
COMPLAINT_STATUS = {
    0: '待处理',
    1: '已回复',
}

# ============================================
# 柜体默认值
# ============================================
DEFAULT_DEPOSIT_AMOUNT = 20
DEFAULT_SLOT_SIZE = 'M'

# ============================================
# 主板默认配置
# ============================================
BRAND_DEFAULTS = {
    'YBM': {'serial_port': 'ttyS4', 'baud_rate': 9600},
    'WT': {'serial_port': 'ttyS3', 'baud_rate': 115200},
    'QM': {'serial_port': 'ttyS2', 'baud_rate': 9600},
}

# ============================================
# 辅助函数
# ============================================
def generate_order_no():
    """生成订单号"""
    import random, string
    return datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))

def generate_access_code():
    """生成4位取件码"""
    import random, string
    return ''.join(random.choices(string.digits, k=4))

def generate_sms_code():
    """生成6位短信验证码"""
    import random, string
    return ''.join(random.choices(string.digits, k=6))