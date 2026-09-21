# -*- coding: utf-8 -*-
"""
S523-20260921 修"新小程序"写死清域智 -> 改成跟着【当前生效的小程序账号】走
=====================================================================
问题（老板实测）：生效小程序已切成卓蓝时(wx281a9540a6a5b64d / 前缀 oXTD3x)，
  但代码里"新小程序"写死成清域智(wx0be09d4de1417e01 / oQXFs3)：
    · new_mp_openid_prefix() 返回 oQXFs3 -> strict 只承认 oQXFs3 开头的 id
    · 卓蓝时用户的 openid(oXTD3x…) 被判"没有身份" -> 订单 user_id=0、看不到订单/余额
    · legacy_openid_prefixes() 把卓蓝时的前缀算成"老体系" -> 隔离逻辑反向
改法（三处，都是把"写死常量"换成"读生效账号"，读不到才回落常量）：
  1) 新增 _active_mp_ident() -> (appid, prefix)，取 wx_accounts 里 is_active 的 mp 账号
  2) new_mp_openid_prefix()  用 _active_mp_ident()
  3) is_new_mp_identity()    生效账号 appid / 前缀 也算"新体系"（并保留原清域智判定）
  4) legacy_openid_prefixes()  从"排除写死 appid"改成"排除生效 appid + 写死 appid"
用法：python patch_s523_newmp.py <helpers.py>
"""
import hashlib
import shutil
import sys

P = sys.argv[1]
s = open(P, encoding='utf-8', newline='').read()
before = hashlib.md5(s.encode('utf-8')).hexdigest()
assert '_active_mp_ident' not in s, '已打过 S523'
errors = []


def rep(old, new, tag):
    global s
    if s.count(old) != 1:
        errors.append('%s 锚点命中 %d 次' % (tag, s.count(old)))
        return
    s = s.replace(old, new, 1)


# 1) legacy_openid_prefixes：排除"生效 appid + 写死 appid"
rep("""        # 注意：这里【不能】加 acct_type 过滤 —— mp + oa 的 prefix 都要算老体系。
        cursor.execute(
            "SELECT DISTINCT openid_prefix FROM wx_accounts "
            "WHERE NULLIF(openid_prefix,'') IS NOT NULL AND appid <> %s",
            (NEW_MP_APPID,))""",
    """        # 注意：这里【不能】加 acct_type 过滤 —— mp + oa 的 prefix 都要算老体系。
        # [S523-20260921] 排除的不能只有写死的清域智：当前生效的小程序(卓蓝时)同样属于"新体系"，
        #   否则它会被算成老体系、隔离逻辑反向。读不到生效账号时只排除写死 appid（老行为）。
        _act_appid_523 = ''
        try:
            _act_appid_523 = _active_mp_ident()[0] or ''
        except Exception:
            _act_appid_523 = ''
        cursor.execute(
            "SELECT DISTINCT openid_prefix FROM wx_accounts "
            "WHERE NULLIF(openid_prefix,'') IS NOT NULL AND appid <> %s AND appid <> %s",
            (NEW_MP_APPID, _act_appid_523 or NEW_MP_APPID))""",
    'legacy_openid_prefixes 排除生效账号')

# 2) 新增 _active_mp_ident()（插在 legacy_openid_prefixes 之前）
rep("def legacy_openid_prefixes():",
    '''def _active_mp_ident():
    """[S523-20260921] 当前【生效的小程序账号】(appid, openid_prefix)。

    为什么要有它：原来"新小程序"写死成清域智(wx0be09d4de1417e01/oQXFs3)，
      但老板 2026-09-21 起生效的小程序是卓蓝时(wx281a9540a6a5b64d/oXTD3x)，
      它的用户于是被判成"没有身份"（订单 user_id=0、看不到订单/余额）。
    取值：wx_accounts 里 is_active=1 的 mp 账号；读不到 -> 前缀读不到 -> 再回落写死常量。
    """
    appid, prefix = '', ''
    try:
        import wx_config as _wc523
        _acc523 = _wc523.get_effective_account('mp') or {}
        appid = str(_acc523.get('appid') or '').strip()
        prefix = str(_acc523.get('openid_prefix') or '').strip()
    except Exception as _e523:
        logger.warning('[S523] 取生效小程序账号失败，回落写死常量: %s', _e523)
    if not appid:
        appid = NEW_MP_APPID
    if not prefix:
        try:
            prefix = (_mp_openid_prefix_of(appid) or '').strip()
        except Exception:
            prefix = ''
    if not prefix:
        prefix = _NEW_MP_PREFIX_FALLBACK
    return appid, prefix


def legacy_openid_prefixes():''',
    '新增 _active_mp_ident')

# 3) new_mp_openid_prefix 用生效账号
rep("""    p = ''
    try:
        p = _mp_openid_prefix_of(NEW_MP_APPID) or ''
    except Exception:
        p = ''
    if not p:
        p = _NEW_MP_PREFIX_FALLBACK""",
    """    # [S523-20260921] 以当前生效的小程序账号为准（生效的是卓蓝时，不再是写死的清域智）
    try:
        p = _active_mp_ident()[1] or ''
    except Exception:
        p = ''
    if not p:
        p = _NEW_MP_PREFIX_FALLBACK""",
    'new_mp_openid_prefix 用生效账号')

# 4) is_new_mp_identity：生效账号也算新体系
rep("""    _appid = (appid or '').strip()
    if _appid:
        # 客户端带了 appid：只信 appid，不做前缀猜测
        if _appid == NEW_MP_APPID:
            return True""",
    """    _appid = (appid or '').strip()
    # [S523-20260921] 当前生效的小程序账号同样属于"新体系"
    try:
        _act_appid_523b, _act_prefix_523b = _active_mp_ident()
    except Exception:
        _act_appid_523b, _act_prefix_523b = NEW_MP_APPID, _NEW_MP_PREFIX_FALLBACK
    if _appid:
        # 客户端带了 appid：只信 appid，不做前缀猜测
        if _appid == NEW_MP_APPID or _appid == _act_appid_523b:
            return True""",
    'is_new_mp_identity appid 判定')

rep("""    _oid = (openid or mp_openid or '').strip()
    if not _oid:
        return False
    for _p in legacy_openid_prefixes():""",
    """    _oid = (openid or mp_openid or '').strip()
    if not _oid:
        return False
    # [S523] 前缀 == 当前生效小程序 -> 新体系（先于老体系判断）
    if _act_prefix_523b and _oid.startswith(_act_prefix_523b):
        return True
    for _p in legacy_openid_prefixes():""",
    'is_new_mp_identity 前缀判定')

if errors:
    print('❌ 锚点没命中，未写文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

shutil.copy2(P, P + '.bak_s523')
open(P, 'w', encoding='utf-8', newline='').write(s)
print('helpers.py %s -> %s' % (before, hashlib.md5(s.encode('utf-8')).hexdigest()))
print('标记: _active_mp_ident=%d S523=%d' % (s.count('def _active_mp_ident('), s.count('S523-20260921')))
