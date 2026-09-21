# -*- coding: utf-8 -*-
"""
S521 阶段② — 新订单按 id 认人（开关 new_order；随时可秒切回 off）
=================================================================
改动（只动支付宝/微信共用的"订单身份"这一处；微信老路径在开关 off 时一字不变）：
  1) helpers.py 新增 _s521_ident_kind(openid, mp_openid, alipay_uid)
     —— 判定这次请求的身份类别：公众号 openid / 小程序 openid / 支付宝 uid
        （用 wx_config.appid_by_openid 的前缀映射判定；判不出来再按字段、最后按 is_new_mp_identity 兜底）
  2) routes/user.py 的 store_init：把 `_store_uid = _resolve_user(...)` 换成开关控制：
       开关 new_order/all 且能拿到 id  -> resolve_user_by_ident(cursor, kind, ident)
       开关 off 或拿不到 id            -> 一字不变走原来的 _resolve_user（含 S405 新公众号隔离）
  3) 把 system_settings.identity_strict_mode 置为 'new_order'

钱的路子不动：余额/退款/提现仍走原函数（按手机号/wallet 行），本次只改"订单属于谁"。
用法：python patch_s521_stage2.py <helpers.py> <routes/user.py>
"""
import hashlib
import shutil
import sys

HP, UP = sys.argv[1], sys.argv[2]
h = open(HP, encoding='utf-8', newline='').read()
u = open(UP, encoding='utf-8', newline='').read()
mh0 = hashlib.md5(h.encode('utf-8')).hexdigest()
mu0 = hashlib.md5(u.encode('utf-8')).hexdigest()
assert '_s521_ident_kind' not in h, 'helpers.py 已打过阶段②'
assert 'S521] store_init 按 id 认人' not in u, 'user.py 已打过阶段②'
errors = []


def rep(text, old, new, tag):
    n = text.count(old)
    if n != 1:
        errors.append('%s 锚点命中 %d 次（应为1）' % (tag, n))
        return text
    return text.replace(old, new, 1)


# ---------- 1) helpers.py：身份类别判定 ----------
H_ANCHOR = "def resolve_user_by_ident(cursor, kind, ident, auto_create=True):"
h = rep(h,
        H_ANCHOR,
        '''def _s521_ident_kind(openid='', mp_openid='', alipay_uid=''):
    """[S521] 判断本次请求的身份类别，返回 (kind, ident)。

    规则（老板口径：小程序看小程序 ID、公众号看公众号 ID、各自算各自的）：
      · openid 前缀属于【公众号】账号  -> ('oa_openid', openid)   （H5 入口）
      · openid 前缀属于【小程序】账号  -> ('mp_openid', openid)   （小程序客户端常把 mp openid 放在 openid 字段）
      · 前缀判不出来但 is_new_mp_identity 为真（新体系）-> ('mp_openid', openid)
      · 其余：mp_openid 非空 -> ('mp_openid', mp_openid)；alipay_uid 非空 -> ('alipay_uid', alipay_uid)
      · 什么 id 都没有 -> ('', '')，调用方回落老逻辑
    """
    o = _clean(openid)
    m = _clean(mp_openid)
    a = _clean(alipay_uid)
    if o:
        try:
            if appid_by_openid(o, 'oa'):
                return 'oa_openid', o
        except Exception:
            pass
        try:
            if appid_by_openid(o, 'mp'):
                return 'mp_openid', o
        except Exception:
            pass
        try:
            if is_new_mp_identity(openid=o):
                return 'mp_openid', o
        except Exception:
            pass
        return 'oa_openid', o
    if m:
        return 'mp_openid', m
    if a:
        return 'alipay_uid', a
    return '', ''


''' + H_ANCHOR,
        'helpers 新增 _s521_ident_kind')

# ---------- 2) user.py：store_init 订单身份 ----------
U_OLD = """        _resolve_phone401 = '' if _iso401 else user_phone
        _resolve_union401 = '' if _iso401 else unionid
        _store_uid = _resolve_user(cursor, openid=openid, mp_openid=mp_openid, phone=_resolve_phone401,
                                   unionid=_resolve_union401, strict_openid=_strict_new)"""
U_NEW = """        _resolve_phone401 = '' if _iso401 else user_phone
        _resolve_union401 = '' if _iso401 else unionid
        # [S521-阶段②] 开关 new_order/all 时：订单身份【按平台 id 认】——
        #   小程序看 mp_openid、公众号看 openid、支付宝看 alipay_uid；手机号只作联系方式。
        #   开关 off 或拿不到任何 id 时，一字不变地走原来的 _resolve_user（含 S405 新公众号隔离）。
        _store_uid = 0
        try:
            from helpers import identity_strict_mode as _ism521
            from helpers import _s521_ident_kind as _kind521
            from helpers import resolve_user_by_ident as _ruid521
            if _ism521() in ('new_order', 'all'):
                _k521, _i521 = _kind521(openid=openid, mp_openid=mp_openid,
                                        alipay_uid=str(data.get('alipay_uid') or ''))
                if _k521 and _i521:
                    _store_uid = _ruid521(cursor, _k521, _i521)
                    if _store_uid:
                        logger.info('[S521] store_init 按 id 认人 kind=%s ident=%s... -> user_id=%s',
                                    _k521, str(_i521)[:10], _store_uid)
                else:
                    logger.info('[S521] store_init 没拿到平台 id，回落老逻辑认人 phone=%s', user_phone)
        except Exception as _e521:
            logger.warning('[S521] store_init 按 id 认人异常，回落老逻辑: %s', _e521)
            _store_uid = 0
        if not _store_uid:
            _store_uid = _resolve_user(cursor, openid=openid, mp_openid=mp_openid, phone=_resolve_phone401,
                                       unionid=_resolve_union401, strict_openid=_strict_new)"""
u = rep(u, U_OLD, U_NEW, 'store_init 订单身份')

if errors:
    print('❌ 有锚点没命中，未写任何文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

for path, text in ((HP, h), (UP, u)):
    shutil.copy2(path, path + '.bak_s521s2')
    open(path, 'w', encoding='utf-8', newline='').write(text)
print('helpers.py %s -> %s' % (mh0, hashlib.md5(h.encode('utf-8')).hexdigest()))
print('user.py    %s -> %s' % (mu0, hashlib.md5(u.encode('utf-8')).hexdigest()))
print("标记: _s521_ident_kind=%d  store_init按id认人=%d" % (h.count('_s521_ident_kind'), u.count('S521] store_init 按 id 认人')))
