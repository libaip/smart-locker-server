# -*- coding: utf-8 -*-
"""[S347 2026-09-20] 用户入口模式 全局总开关（第一版：开关 + 入口分流骨架）

设计依据：S345_入口模式全局切换方案_20260920.md §2.1 / §2.3 / §2.2

开关落点：wx_config_items.entry_mode（DB 里已有该 key，历史备注 "h5 / mp / auto"）
读写机制：wx_config.get_config / set_config（30 秒缓存；set_config 会主动 clear_cache()
          → 改完立刻生效，不需要重启、不需要 HUP、不需要发版）

四个取值：
    mp      纯小程序   —— 微信里进 H5 → 第 1 步后跳小程序（= 现状，默认）
    oa      纯公众号   —— 微信里引导关注 → 留在 H5 网页存包，不进小程序
    h5      H5 跳小程序 —— 和 mp 在"入口"这一层行为一致（= 现状）
    alipay  纯支付宝   —— 微信里扫 → 纯静态提示页「请用支付宝扫码使用」

⚠️ 安全口径（红线，别改）：
    1. 读不到配置 / 任何异常 / 值非法 → 一律回落 DEFAULT_MODE = 'mp'（= 现状），
       保证"开关切错了也不至于全站进不来"。
    2. 只有 'oa' / 'alipay' 会让入口走**新**分支；'mp'、'h5' 以及任何未知值都走
       store_page() 里原来那段一模一样的代码 —— 即"默认值与现在完全一致"。
    3. 本模块**只读配置，永不写库**（改值请用 wx_config.set_config 或后台）。
"""

MODE_MP = 'mp'
MODE_OA = 'oa'
MODE_H5 = 'h5'
MODE_ALIPAY = 'alipay'

VALID_MODES = (MODE_MP, MODE_OA, MODE_H5, MODE_ALIPAY)

# 读不到配置时的回落值。必须是 'mp'：= 今天线上跑的行为，最安全
DEFAULT_MODE = MODE_MP

CONFIG_KEY = 'entry_mode'

MODE_LABELS = {
    MODE_MP: '纯小程序（现状）',
    MODE_OA: '纯公众号',
    MODE_H5: 'H5 跳小程序（现状）',
    MODE_ALIPAY: '纯支付宝',
}

# "现状口径"：这两个取值在入口层行为完全相同（都走老逻辑），
# 所以线上当前 wx_config_items.entry_mode='h5' 时，行为与今天逐字节一致
LEGACY_MODES = (MODE_MP, MODE_H5)


def normalize_mode(value, default=DEFAULT_MODE):
    """把任意配置值规整成合法模式；非法/空/异常 → default（默认 'mp'）"""
    try:
        v = ('' if value is None else str(value)).strip().lower()
    except Exception:
        return default
    return v if v in VALID_MODES else default


def get_entry_mode(default=DEFAULT_MODE):
    """读全局入口模式（请求级调用一次即可）。

    缓存：直接用 wx_config 自己的 30 秒缓存（CONFIG_CACHE_TTL=30），
          set_config 会 clear_cache → 热更新；所以这里**不再自己加缓存**
          （加了反而会导致"改了不生效"）。
    绝不抛异常：读库失败、配置中心炸、值非法 → 全部回落 default（'mp' = 现状）。
    """
    try:
        from wx_config import get_config
        raw = get_config(CONFIG_KEY, default)
    except Exception:
        return default
    return normalize_mode(raw, default)


def get_raw_entry_mode(default=None):
    """读原始字符串（只给后台/诊断用，不做规整）"""
    try:
        from wx_config import get_config
        return get_config(CONFIG_KEY, default)
    except Exception:
        return default


def describe():
    """诊断/后台页面用：实际生效的模式 + 原始值 + 是否取自默认回落"""
    raw = get_raw_entry_mode(default=None)
    mode = normalize_mode(raw, DEFAULT_MODE)
    return {
        'key': CONFIG_KEY,
        'mode': mode,
        'label': MODE_LABELS.get(mode, mode),
        'raw_value': raw,
        'valid_modes': list(VALID_MODES),
        'default_mode': DEFAULT_MODE,
        'is_fallback': (normalize_mode(raw, None) is None),
    }
