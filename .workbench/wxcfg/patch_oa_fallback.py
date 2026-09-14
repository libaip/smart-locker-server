# -*- coding: utf-8 -*-
"""[功能②] 小程序订阅消息发不出去时，自动改发公众号订阅通知（兜底通道）。

为什么做：小程序订阅消息是"一次性授权"（用户点一次允许只能发一条）。今天实测 1828 个
          用柜子的用户里，34% 至少有一条通知发不出去（43101 没额度 / 只有旧号 openid）。
凭什么做：公众号"订阅通知"接口 /cgi-bin/message/subscribe/bizsend 实测给"未关注、未订阅"
          的普通用户也能发（连发多条 errcode=0，用户手机确实收到），而且公众号这三个模板的
          字段 key 与小程序那三条完全一致 -> 同一份 data 可以直接复用。

改动（只动 helpers.py，三处）：
  A. 在 send_wx_subscribe_message 前面插入"公众号通道"实现：
     get_oa_access_token() / _find_oa_openid() / send_oa_subscribe_notify() + 开关
  B. "跳过公众号openid"分支 -> 改发公众号后再 return False
  C. "mp_openid为空"分支  -> 改发公众号后再 return False
  D. "发送失败(errcode!=0)"分支 -> 改发公众号后再 return False

开关：设置项 oa_notify_fallback_enabled（默认 true）；关掉即回退到改造前的行为。
去重：同手机号+同模板+同内容 90 秒内只发一次（防调用方重试造成重复消息）。

用法：
  python3 patch_oa_fallback.py <项目根>            # 干跑
  python3 patch_oa_fallback.py <项目根> --real     # 真改（带备份）
  python3 patch_oa_fallback.py <项目根> --revert   # 还原
"""
import io, os, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
P = os.path.join(ROOT, 'helpers.py')
BKDIR = os.path.join(ROOT, 'backups')

NEW_BLOCK = '''
# ============================================================
# [2026-09-14] 公众号订阅通知通道：小程序订阅消息发不出去时的兜底
#   小程序订阅消息是"一次性授权"（用户点一次允许只能发一条），实测今天 34% 的用户
#   至少有一条通知发不出去（43101 没额度 / 只有旧号 openid / 没有小程序 openid）。
#   公众号"订阅通知"(/cgi-bin/message/subscribe/bizsend) 实测对未关注、未订阅的普通
#   用户也能发（连发多条 errcode=0，用户手机确实收到）-> 用它兜底。
#   公众号这三个模板的字段 key 与小程序那三条完全一致，data 可直接复用。
#   开关：设置项 oa_notify_fallback_enabled（默认 true），关掉即等于改造前行为。
# ============================================================
_OA_SUB_TPL = {
    'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk': 'oa_sub_deposit',   # 寄存成功
    'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA': 'oa_sub_general',   # 押金退还
    'lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c': 'oa_sub_refund',    # 退款成功
}
_OA_SUB_TPL_DEFAULT = {
    'oa_sub_deposit': 'RyvAHJzC46JDEk_w8nAJHlLppNtNiAgkqP_GdHGWEUg',
    'oa_sub_general': 'pG1ieUsHgBg5y0ZJCfjNiUrF7QuftlSWwDYXSpAp6js',
    'oa_sub_refund': 'nDTX0vT_wODrTnM4A1qSNnSXDUTNgw2Fm0CMF0eZe-o',
}
_oa_token_cache = {'token': '', 'exp': 0.0}
_oa_sent_cache = {}


def _oa_fallback_enabled():
    """兜底通道总开关（设置项 oa_notify_fallback_enabled，默认开）"""
    try:
        return str(get_setting('oa_notify_fallback_enabled', 'true')).strip().lower() in ('true', '1', 'yes', 'on')
    except Exception:
        return True


def get_oa_access_token():
    """公众号 access_token（进程内缓存；取不到返回空串）"""
    import time as _t
    now = _t.time()
    if _oa_token_cache['token'] and now < _oa_token_cache['exp']:
        return _oa_token_cache['token']
    try:
        import requests
        _r = requests.get(
            'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s'
            % (_wx_oa_id(), _wx_oa_secret()), timeout=8).json()
        _tok = _r.get('access_token', '') or ''
        if _tok:
            _oa_token_cache['token'] = _tok
            _oa_token_cache['exp'] = now + int(_r.get('expires_in', 7200) or 7200) - 300
        else:
            logger.warning('[oa_notify] 取公众号access_token失败: %s' % _r)
        return _tok
    except Exception as _e:
        logger.warning('[oa_notify] 取公众号access_token异常: %s' % _e)
        return ''


def find_oa_openid(phone='', unionid=''):
    """找该用户的【公众号】openid（前缀 oLhbm2）：phone_openids.gzh_openid -> users.openid -> phone_openids.openid -> 按 unionid 跨手机号"""
    try:
        from database import get_db
        _c = get_db()
        _cur = _c.cursor()
        _pref = oa_openid_prefix()
        _oid = ''
        if phone:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE phone=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE %s ORDER BY id ASC LIMIT 1", (phone, _pref + '%'))
            _r = _cur.fetchone()
            if _r and _r.get('gzh_openid'):
                _oid = _r['gzh_openid']
            if not _oid:
                _cur.execute("SELECT openid FROM users WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id ASC LIMIT 1", (phone, _pref + '%'))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
            if not _oid:
                _cur.execute("SELECT openid FROM phone_openids WHERE phone=%s AND COALESCE(openid,'')<>'' AND openid LIKE %s ORDER BY id ASC LIMIT 1", (phone, _pref + '%'))
                _r = _cur.fetchone()
                if _r and _r.get('openid'):
                    _oid = _r['openid']
        if not _oid and unionid:
            _cur.execute("SELECT gzh_openid FROM phone_openids WHERE unionid=%s AND COALESCE(gzh_openid,'')<>'' AND gzh_openid LIKE %s ORDER BY id ASC LIMIT 1", (unionid, _pref + '%'))
            _r = _cur.fetchone()
            if _r and _r.get('gzh_openid'):
                _oid = _r['gzh_openid']
        try:
            _c.close()
        except Exception:
            pass
        return _oid or ''
    except Exception as _e:
        logger.warning('[oa_notify] 找公众号openid失败: %s' % _e)
        return ''


def send_oa_subscribe_notify(mp_template_id, data, phone='', unionid='', reason=''):
    """小程序通道发不出去时的兜底：改发公众号订阅通知。返回 True/False。

    只对我们登记过映射的三个模板生效（寄存成功/押金退还/退款成功），其它模板直接返回 False。
    同一个手机号+同一模板+同一内容 90 秒内只发一次（防调用方重试造成重复消息）。
    """
    try:
        if not _oa_fallback_enabled():
            return False
        _biz = _OA_SUB_TPL.get(mp_template_id)
        if not _biz:
            return False
        try:
            from wx_config import template_id as _tpl_id
            _oa_tpl = _tpl_id(_biz, 'oa_sub', _OA_SUB_TPL_DEFAULT.get(_biz, '')) or _OA_SUB_TPL_DEFAULT.get(_biz, '')
        except Exception:
            _oa_tpl = _OA_SUB_TPL_DEFAULT.get(_biz, '')
        if not _oa_tpl:
            return False
        _oid = find_oa_openid(phone=phone, unionid=unionid)
        if not _oid:
            logger.warning('[oa_notify] 跳过(没有公众号openid): phone=%s biz=%s 原因=%s' % (phone, _biz, reason))
            return False
        try:
            import json as _json, time as _t, hashlib as _hl
            _key = (phone or '') + '|' + _biz + '|' + _hl.md5(_json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
            _now = _t.time()
            if _oa_sent_cache.get(_key, 0) > _now - 90:
                logger.info('[oa_notify] 90秒内同内容已发过，跳过重复: phone=%s biz=%s' % (phone, _biz))
                return False
        except Exception:
            _key = None
        _tok = get_oa_access_token()
        if not _tok:
            return False
        import requests
        _resp = requests.post(
            'https://api.weixin.qq.com/cgi-bin/message/subscribe/bizsend?access_token=%s' % _tok,
            json={'touser': _oid, 'template_id': _oa_tpl, 'data': data}, timeout=6).json()
        if _resp.get('errcode') == 0:
            if _key:
                _oa_sent_cache[_key] = time.time()
            logger.info('[oa_notify] 发送成功(公众号兜底): phone=%s openid=%s..., biz=%s, 小程序失败原因=%s'
                        % (phone, _oid[:8], _biz, reason))
            return True
        logger.error('[oa_notify] 发送失败: phone=%s openid=%s..., biz=%s, result=%s, 小程序失败原因=%s'
                     % (phone, _oid[:8], _biz, _resp, reason))
        return False
    except Exception as _e:
        logger.error('[oa_notify] 异常: %s' % _e)
        return False


'''

SKIP_OLD = """        if openid and not openid.startswith(mp_openid_prefix()):
            logger.warning(f'[subscribe_msg] 跳过公众号openid: openid={openid[:8]}..., phone={phone}')
            return False
        if not openid:
            logger.warning(f'[subscribe_msg] mp_openid为空，跳过发送（phone={phone}）')
            return False"""

SKIP_NEW = """        if openid and not openid.startswith(mp_openid_prefix()):
            logger.warning(f'[subscribe_msg] 跳过公众号openid: openid={openid[:8]}..., phone={phone}')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='只有旧号/公众号openid')
            return False
        if not openid:
            logger.warning(f'[subscribe_msg] mp_openid为空，跳过发送（phone={phone}）')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='没有小程序openid')
            return False"""

FAIL_OLD = """            logger.error(f'[subscribe_msg] 发送失败: openid={openid[:8]}..., phone={phone}, template={template_id}, result={result}')
            return False"""

FAIL_NEW = """            logger.error(f'[subscribe_msg] 发送失败: openid={openid[:8]}..., phone={phone}, template={template_id}, result={result}')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='errcode=%s' % result.get('errcode'))
            return False"""

ANCHOR_DEF = "def send_wx_subscribe_message(openid, template_id, data, page='', phone=None, unionid=None):"


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'helpers.py'))
         for d in os.listdir(BKDIR) if d.startswith('oafb_') and os.path.isfile(os.path.join(BKDIR, d, 'helpers.py'))]
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    shutil.copy2(src, P)
    subprocess.run(['python3', '-m', 'py_compile', P], check=True)
    print('  已还原自 %s' % src)
    print('  md5=%s' % subprocess.run(['md5sum', P], capture_output=True, text=True).stdout.split()[0])
    raise SystemExit(0)

txt = io.open(P, encoding='utf-8').read()
checks = [(ANCHOR_DEF, 1, '插入点: send_wx_subscribe_message 定义行'),
          (SKIP_OLD, 1, '跳过分支'),
          (FAIL_OLD, 1, '发送失败分支')]
ok = True
for a, want, label in checks:
    n = txt.count(a)
    print('  %s %-28s 命中 %d/%d' % ('✓' if n == want else '✗', label, n, want))
    if n != want:
        ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，一个文件都不写')

new = txt.replace(ANCHOR_DEF, NEW_BLOCK.lstrip('\n') + ANCHOR_DEF, 1)
new = new.replace(SKIP_OLD, SKIP_NEW, 1)
new = new.replace(FAIL_OLD, FAIL_NEW, 1)
try:
    compile(new, P, 'exec')
except SyntaxError as e:
    raise SystemExit('[中止] 语法不过: %s' % e)
print('  语法检查通过; 新增 %d 字节' % (len(new.encode('utf-8')) - len(txt.encode('utf-8'))))

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'oafb_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(P, os.path.join(d, 'helpers.py'))
io.open(P, 'w', encoding='utf-8').write(new)
compile(io.open(P, encoding='utf-8').read(), P, 'exec')
print('  已写入 helpers.py md5=%s' % subprocess.run(['md5sum', P], capture_output=True, text=True).stdout.split()[0])
print('  备份: %s' % os.path.join(d, 'helpers.py'))
