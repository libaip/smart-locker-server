# -*- coding: utf-8 -*-
"""
S521 阶段① — 骨架（默认关、零线上影响）
========================================
在 helpers.py 里新增（只加，不改任何现有函数）：
  1) identity_strict_mode()   读开关：off(默认) / new_order / all；异常一律回落 off
  2) resolve_user_by_ident()  按平台 id 认人：
       · 小程序只看 mp_openid、公众号只看 openid、支付宝只看 alipay_uid
       · 手机号完全不参与；查不到就建一条新身份（phone 留空）
       · 同一 id 命中多行（存量脏数据）-> 取最早那行(id 最小) + 告警
       · id 为空或长度异常 -> 直接拒绝（不查询、不兜底；S419 事故根因就是空串变通配符）
       · 插入用"单语句条件插入 + 回查最早行"，并发下最多多出一条空行，由对账脚本发现（不引锁，避免连接池锁泄漏）
  本阶段【不接任何调用点】——线上行为零变化；开关默认 off，等于现在的老逻辑。
用法：python patch_s521.py <helpers.py>
"""
import hashlib
import shutil
import sys

P = sys.argv[1]
src = open(P, encoding='utf-8', newline='').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'resolve_user_by_ident' not in src, '已打过 S521'

ANCHOR = "def resolve_user_identity(cursor, openid='', mp_openid='', phone='', unionid='', user_id=0,"
assert src.count(ANCHOR) == 1, '锚点命中 %d 次' % src.count(ANCHOR)

NEW = '''# ============================================================
# [S521-20260921] 身份改造：新数据按平台 id 认人
#   小程序只看 mp_openid / 公众号只看 openid / 支付宝只看 alipay_uid
#   手机号不再参与认人（只当联系方式）
#   开关 system_settings.identity_strict_mode: off(默认=老逻辑) / new_order / all
#   阶段①只落地函数与开关，不接任何调用点 —— 线上行为零变化
# ============================================================
_IDENT_COL = {'mp_openid': 'mp_openid', 'oa_openid': 'openid', 'alipay_uid': 'alipay_uid'}


def identity_strict_mode():
    """[S521] 身份改造开关；任何异常/非法值一律回落 'off'（等同老逻辑）。"""
    try:
        v = str(get_setting('identity_strict_mode', 'off') or 'off').strip().lower()
    except Exception:
        v = 'off'
    return v if v in ('off', 'new_order', 'all') else 'off'


def resolve_user_by_ident(cursor, kind, ident, auto_create=True):
    """[S521] 按平台 id 认人：kind ∈ {mp_openid, oa_openid, alipay_uid}

    老板口径（2026-09-21）：存量不动；新数据统一按 id 认人；小程序与公众号各算各的、不合并。
      · 只按 id 查/建，手机号不参与；查不到就新建身份（phone 留空）
      · 同一 id 命中多行（历史脏数据）-> 取最早那行(id 最小) + 告警，绝不猜别的行
      · id 空 / 长度不在 16~64 -> 直接拒绝，绝不做兜底查询
      · 并发：用"单语句条件插入 + 回查最早行"，宁可多出一条空行也不引会话级锁
        （连接池 + autocommit 下 pg_advisory_lock 有泄漏风险；空行由对账脚本发现）
    返回 user_id；0 = 没认出来/被拒绝。
    """
    col = _IDENT_COL.get(str(kind or '').strip())
    if not col:
        logger.error('[ident] 未知 kind=%s，拒绝认人', kind)
        return 0
    ident = _clean(ident)
    if not ident:
        logger.warning('[ident] 空 ident，拒绝认人 kind=%s', kind)
        return 0
    if not (16 <= len(ident) <= 64):
        logger.warning('[ident] ident 长度异常(%d)，拒绝认人 kind=%s', len(ident), kind)
        return 0
    try:
        cursor.execute('SELECT id FROM users WHERE %s = %%s ORDER BY id' % col, (ident,))
        rows = cursor.fetchall() or []
    except Exception as e:
        logger.error('[ident] 查询失败 kind=%s: %s', kind, e)
        return 0
    ids = [int((r['id'] if hasattr(r, 'keys') else r[0]) or 0) for r in rows]
    ids = [i for i in ids if i > 0]
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        logger.warning('[ident] 同一身份命中 %d 行（按规矩取最早 id=%s，请客服人工核）kind=%s ident=%s...',
                       len(ids), ids[0], kind, ident[:10])
        return ids[0]
    if not auto_create:
        return 0
    try:
        cursor.execute(
            "INSERT INTO users (%s, phone) SELECT %%s, '' "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE %s = %%s) RETURNING id" % (col, col),
            (ident, ident))
        r = cursor.fetchone()
        if r:
            uid = int((r['id'] if hasattr(r, 'keys') else r[0]) or 0)
            logger.info('[ident] 新建身份 kind=%s ident=%s... user_id=%s', kind, ident[:10], uid)
            return uid
        # 并发下别人先建了 -> 回查最早那行
        cursor.execute('SELECT id FROM users WHERE %s = %%s ORDER BY id LIMIT 1' % col, (ident,))
        r2 = cursor.fetchone()
        if r2:
            return int((r2['id'] if hasattr(r2, 'keys') else r2[0]) or 0)
    except Exception as e:
        logger.error('[ident] 新建失败 kind=%s: %s', kind, e)
    return 0


'''

src = src.replace(ANCHOR, NEW + ANCHOR, 1)
shutil.copy2(P, P + '.bak_s521')
open(P, 'w', encoding='utf-8', newline='').write(src)
after = hashlib.md5(src.encode('utf-8')).hexdigest()
print('helpers.py %s -> %s' % (before, after))
print('新函数标记: resolve_user_by_ident=%d identity_strict_mode=%d' %
      (src.count('def resolve_user_by_ident('), src.count('def identity_strict_mode(')))
