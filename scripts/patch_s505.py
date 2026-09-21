# -*- coding: utf-8 -*-
"""
S505-20260921 用户资料完善接口（新小程序"头像昵称填写能力"后端）
============================================================
背景：微信 2022-11-08 起 wx.getUserProfile 只返回匿名昵称/头像（"微信用户"+灰头像），
      唯一合规途径是让用户自己填（<button open-type="chooseAvatar"> + <input type="nickname">）。
      本补丁在 routes/user.py 末尾【纯新增】两个接口，不改动任何既有逻辑：

  GET  /api/user/profile?phone=&openid=&mp_openid=   读取昵称/头像（供"完善资料"页预填）
  POST /api/user/save-profile                        保存昵称 + 头像(base64 → static/avatars/<hash>.png)

昵称写入 3 处（后台投诉/订单列表用的就是这三处的 COALESCE）：
  users.wechat_name / users.nickname / phone_openids.wechat_name / user_balances.wechat_name
头像写入 users.avatar_url（列由本次部署同时 ALTER TABLE 添加）
"""
import hashlib
import py_compile
import shutil

P = 'routes/user.py'
MARK = 'S505-20260921 用户资料完善'

src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert MARK not in src, '已经打过该补丁（文件中已存在标记），中止'

CODE = '''

# ============================================================
# [S505-20260921] 用户资料完善（新小程序"头像昵称填写能力"）
#   微信 2022-11-08 起 wx.getUserProfile 只返回匿名昵称/头像，
#   合规途径是让用户自己填：<button open-type="chooseAvatar"> + <input type="nickname">。
#   本段为【纯新增】，不改动任何既有接口逻辑。
# ============================================================
_AVATAR_DIR = 'static/avatars'
_AVATAR_MAX_BYTES = 300 * 1024


def _profile_ident(cur, phone='', openid='', mp_openid=''):
    """尽力把身份补全成 (phone, openid, mp_openid)；找不到的留空，不报错。"""
    phone = (phone or '').strip()
    openid = (openid or '').strip()
    mp_openid = (mp_openid or '').strip()
    try:
        if not phone and (openid or mp_openid):
            cur.execute("""SELECT phone FROM phone_openids
                           WHERE (openid = %s AND %s <> '') OR (mp_openid = %s AND %s <> '')
                           ORDER BY updated_at DESC NULLS LAST LIMIT 1""",
                        (openid, openid, mp_openid, mp_openid))
            r = cur.fetchone()
            if r:
                phone = (r.get('phone') if isinstance(r, dict) else r[0]) or ''
        if not phone and openid:
            cur.execute("SELECT phone FROM user_balances WHERE openid = %s LIMIT 1", (openid,))
            r = cur.fetchone()
            if r:
                phone = (r.get('phone') if isinstance(r, dict) else r[0]) or ''
        if phone and not mp_openid:
            cur.execute("SELECT mp_openid FROM phone_openids WHERE phone = %s LIMIT 1", (phone,))
            r = cur.fetchone()
            if r:
                mp_openid = (r.get('mp_openid') if isinstance(r, dict) else r[0]) or ''
    except Exception as _e:
        logger.warning('[profile] 身份补全失败: %s', _e)
    return phone, openid, mp_openid


def _save_avatar_b64(identity_key, avatar_b64):
    """把 base64 头像落盘，返回可访问 URL；失败返回 ''。"""
    import base64 as _b64
    import hashlib as _hashlib
    import os as _os
    try:
        data = (avatar_b64 or '').strip()
        if not data:
            return ''
        if ',' in data and data.lower().startswith('data:'):
            data = data.split(',', 1)[1]
        if len(data) > _AVATAR_MAX_BYTES * 2:
            logger.warning('[profile] 头像过大，拒绝保存 key=%s len=%s', identity_key, len(data))
            return ''
        raw = _b64.b64decode(data)
        if len(raw) > _AVATAR_MAX_BYTES or len(raw) < 100:
            logger.warning('[profile] 头像大小异常 key=%s bytes=%s', identity_key, len(raw))
            return ''
        if raw[:3] != b'\\xff\\xd8\\xff' and raw[:8] != b'\\x89PNG\\r\\n\\x1a\\n':
            logger.warning('[profile] 头像不是 jpg/png，拒绝 key=%s', identity_key)
            return ''
        name = _hashlib.sha1(str(identity_key).encode('utf-8')).hexdigest()[:20] + '.png'
        d = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), _AVATAR_DIR)
        _os.makedirs(d, exist_ok=True)
        with open(_os.path.join(d, name), 'wb') as f:
            f.write(raw)
        return '/' + _AVATAR_DIR + '/' + name
    except Exception as _e:
        logger.error('[profile] 头像保存失败 key=%s: %s', identity_key, _e)
        return ''


@bp.route('/user/profile', methods=['GET'])
def get_user_profile():
    """读取当前用户的昵称/头像（"完善资料"页预填用）"""
    try:
        phone = request.args.get('phone', '')
        openid = request.args.get('openid', '')
        mp_openid = request.args.get('mp_openid', '')
        if not phone and not openid and not mp_openid:
            return json_response(message='请先登录', code=400)
        conn = get_db()
        cur = conn.cursor()
        phone, openid, mp_openid = _profile_ident(cur, phone, openid, mp_openid)
        nickname, avatar = '', ''
        if phone or openid or mp_openid:
            cur.execute("""SELECT COALESCE(NULLIF(wechat_name,''), '') AS wechat_name,
                                  COALESCE(NULLIF(nickname,''), '') AS nickname,
                                  COALESCE(NULLIF(avatar_url,''), '') AS avatar_url
                           FROM users
                           WHERE (%s <> '' AND phone = %s) OR (%s <> '' AND openid = %s)
                              OR (%s <> '' AND mp_openid = %s)
                           LIMIT 1""",
                        (phone, phone, openid, openid, mp_openid, mp_openid))
            r = cur.fetchone()
            if r:
                nickname = (r.get('wechat_name') or r.get('nickname') or '').strip()
                avatar = (r.get('avatar_url') or '').strip()
        if not nickname and phone:
            cur.execute("SELECT wechat_name FROM phone_openids WHERE phone = %s LIMIT 1", (phone,))
            r = cur.fetchone()
            if r:
                nickname = ((r.get('wechat_name') if isinstance(r, dict) else r[0]) or '').strip()
        conn.close()
        return json_response(data={'phone': phone, 'nickname': nickname, 'avatar_url': avatar,
                                   'has_profile': bool(nickname or avatar)})
    except Exception as e:
        logger.error(f'[user_profile] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/user/save-profile', methods=['POST'])
def save_user_profile():
    """保存用户自己填写的昵称/头像（合规来源：微信"头像昵称填写能力"）"""
    try:
        data = request.get_json(silent=True) or {}
        phone = str(data.get('phone') or '').strip()
        openid = str(data.get('openid') or '').strip()
        mp_openid = str(data.get('mp_openid') or '').strip()
        nickname = str(data.get('nickname') or '').strip()[:60]
        avatar_b64 = str(data.get('avatar') or '')
        if not phone and not openid and not mp_openid:
            return json_response(message='请先登录', code=400)
        if not nickname and not avatar_b64:
            return json_response(message='昵称和头像都是空的', code=400)
        conn = get_db()
        cur = conn.cursor()
        phone, openid, mp_openid = _profile_ident(cur, phone, openid, mp_openid)
        avatar_url = ''
        if avatar_b64:
            avatar_url = _save_avatar_b64(phone or openid or mp_openid, avatar_b64)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 1) users（后台/个人中心通用）
        try:
            cur.execute("""UPDATE users SET wechat_name = COALESCE(NULLIF(%s,''), wechat_name),
                                  nickname    = COALESCE(NULLIF(%s,''), nickname),
                                  avatar_url  = COALESCE(NULLIF(%s,''), avatar_url)
                           WHERE (%s <> '' AND phone = %s) OR (%s <> '' AND openid = %s)
                              OR (%s <> '' AND mp_openid = %s)""",
                        (nickname, nickname, avatar_url,
                         phone, phone, openid, openid, mp_openid, mp_openid))
        except Exception as _e1:
            logger.warning('[save_profile] 更新 users 失败(继续): %s', _e1)
        # 2) phone_openids（后台投诉/订单列表优先取这里）
        if phone and nickname:
            try:
                cur.execute("UPDATE phone_openids SET wechat_name = %s WHERE phone = %s", (nickname, phone))
            except Exception as _e2:
                logger.warning('[save_profile] 更新 phone_openids 失败: %s', _e2)
        # 3) user_balances（钱包页显示用）
        if nickname:
            try:
                cur.execute("""UPDATE user_balances SET wechat_name = %s
                               WHERE (%s <> '' AND phone = %s) OR (%s <> '' AND openid = %s)""",
                            (nickname, phone, phone, openid, openid))
            except Exception as _e3:
                logger.warning('[save_profile] 更新 user_balances 失败: %s', _e3)
        conn.commit()
        conn.close()
        logger.info('[save_profile] 保存成功 phone=%s nickname=%s avatar=%s',
                    phone or (openid or mp_openid)[:8], nickname, bool(avatar_url))
        return json_response(data={'nickname': nickname, 'avatar_url': avatar_url}, message='已保存')
    except Exception as e:
        logger.error(f'[save_profile] 错误: {e}')
        return json_response(message=str(e), code=500)
'''

src2 = src.rstrip('\n') + '\n' + CODE
shutil.copy2(P, P + '.bak_s505')
open(P, 'w', encoding='utf-8').write(src2)
after = hashlib.md5(src2.encode('utf-8')).hexdigest()
py_compile.compile(P, doraise=True)

print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('新增接口出现次数 save-profile=%d  user/profile=%d' % (src2.count('/user/save-profile'), src2.count("'/user/profile'")))
