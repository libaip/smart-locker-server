"""
微信OAuth/网页授权 - Blueprint
"""
import logging
import json
import urllib.request
import urllib.parse
from flask import Blueprint, request, redirect, jsonify
from wx_config import (h5_base as _wx_h5b, h5_store as _wx_h5s, oauth_callback as _wx_oauthcb,
                     ws_base as _wx_ws, pay_notify_url as _wx_payurl)   # [CFG-STEP2C] 域名改从配置中心读，读不到自动用 config.py 原值
from wx_config import (mp_appid as _wx_mp_id, mp_secret as _wx_mp_secret,
                     oa_appid as _wx_oa_id, oa_secret as _wx_oa_secret)   # [CFG-STEP2B] 账号凭据改从配置中心读，读不到自动用 config.py 原值
from config import WX_APP_ID as WX_OA_ID, WX_APP_SECRET as WX_OA_SECRET, WX_MP_APP_ID, WX_MP_APP_SECRET, WX_MP_TOKEN
from database import get_db
from helpers import json_response, logger, get_access_token

bp = Blueprint('webhook', __name__)


@bp.route('/wx/oauth', methods=['GET'])
def wx_oauth():
    """H5端微信OAuth2.0授权，获取openid"""
    try:
        redirect_uri = request.args.get('redirect_uri', '')
        code = request.args.get('code', '')

        if code:
            url = f'https://api.weixin.qq.com/sns/oauth2/access_token?appid={_wx_oa_id()}&secret={_wx_oa_secret()}&code={code}&grant_type=authorization_code'
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode('utf-8'))
                openid = result.get('openid')
                access_token = result.get('access_token', '')
                if openid:
                    logger.info(f"[微信OAuth] 获取openid成功: {openid[:8]}***")
                    # 获取微信昵称
                    wechat_name = ''
                    if access_token:
                        try:
                            info_url = f'https://api.weixin.qq.com/sns/userinfo?access_token={access_token}&openid={openid}&lang=zh_CN'
                            req2 = urllib.request.Request(info_url)
                            with urllib.request.urlopen(req2, timeout=10) as resp2:
                                user_info = json.loads(resp2.read().decode('utf-8'))
                            wechat_name = user_info.get('nickname', '')
                            unionid = user_info.get('unionid', '')
                            if wechat_name or unionid:
                                logger.info(f"[微信OAuth] 获取用户信息: name={wechat_name}, unionid={unionid[:8] if unionid else 'N/A'}***")
                                # 保存到user_profiles
                                try:
                                    conn = get_db()
                                    conn.execute('INSERT OR REPLACE INTO user_profiles (openid, wechat_name, unionid, updated_at) VALUES (%s, %s, %s, CURRENT_TIMESTAMP)', (openid, wechat_name, unionid))
                                    conn.commit()
                                    conn.close()
                                except Exception as db_e:
                                    logger.error(f"[微信OAuth] 保存用户信息失败: {db_e}")
                        except Exception as info_e:
                            logger.warning(f"[微信OAuth] 获取用户信息失败(非致命): {info_e}")
                    if redirect_uri:
                        separator = '&' if '?' in redirect_uri else '?'
                        extra_parts = []
                        if wechat_name:
                            extra_parts.append(f'wechat_name={urllib.parse.quote(wechat_name)}')
                        if unionid:
                            extra_parts.append(f'unionid={unionid}')
                        extra = '&' + '&'.join(extra_parts) if extra_parts else ''
                        return redirect(f'{redirect_uri}{separator}openid={openid}{extra}')
                    return json_response({'openid': openid, 'wechat_name': wechat_name, 'unionid': unionid})
                logger.error(f"[微信OAuth] 获取openid失败: {result}")
                return json_response(message='授权失败', code=400)
            except Exception as e:
                logger.error(f"[微信OAuth] 请求微信API失败: {e}")
                return json_response(message='授权失败', code=500)
        else:
            if not redirect_uri:
                return json_response(message='缺少redirect_uri', code=400)
            oauth_callback = _wx_oauthcb()
            oauth_redirect = f'https://open.weixin.qq.com/connect/oauth2/authorize?appid={_wx_oa_id()}&redirect_uri={urllib.parse.quote(oauth_callback + "?redirect_uri=" + urllib.parse.quote(redirect_uri, safe=""))}&response_type=code&scope=snsapi_userinfo&state=locker#wechat_redirect'
            return redirect(oauth_redirect)
    except Exception as e:
        logger.error(f'[wx_oauth] {e}')
        return json_response(message=str(e), code=500)



@bp.route('/wx/generate-scheme', methods=['POST'])
def generate_wx_scheme():
    try:
        import urllib.request
        import json as json_lib
        req_data = request.get_json() or {}
        path = req_data.get('path', 'pages/deposit/deposit')
        body = json_lib.dumps({
            'jump_wxa': {'path': path, 'query': req_data.get('query', '')},
            'expire_type': 1,
            'expire_interval': 365
        }).encode()

        def _do_scheme(_tk):
            _url = 'https://api.weixin.qq.com/wxa/generatescheme?access_token=' + (_tk or '')
            _rq = urllib.request.Request(_url, data=body, headers={'Content-Type': 'application/json'})
            _rs = urllib.request.urlopen(_rq, timeout=10)
            return json_lib.loads(_rs.read().decode())

        token = get_access_token()
        if not token:
            return jsonify({'code': 500, 'message': 'token failed'})
        result = _do_scheme(token)
        # [S240-20260918] 令牌被挤失效(40001/42001)时强制刷新令牌重试一次：
        #   以前失效后会一直失败到缓存过期(最长2小时)，H5 拿不到跳转串只能走
        #   appid 兜底跳转，用户表现为"点多少次都跳不过小程序"。
        if result.get('errcode') in (40001, 42001):
            try:
                logger.warning('[generate-scheme] token 失效(%s)，刷新后重试' % result.get('errcode'))
            except Exception:
                pass
            _tk2 = get_access_token(force_refresh=True)
            if _tk2:
                result = _do_scheme(_tk2)
        if result.get('errcode') == 0:
            return jsonify({'code': 200, 'data': {'scheme': result.get('openlink', '')}})
        else:
            return jsonify({'code': 500, 'message': str(result)})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e)})

@bp.route('/cabinets/by-group/<group_code>', methods=['GET'])
def get_cabinets_by_group_code(group_code):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cg.*, l.name as location_name, l.address as location_address, 20.0 as deposit_amount FROM cabinet_groups cg JOIN locations l ON cg.location_id = l.id WHERE cg.group_code = %s", (group_code,))
        group = cursor.fetchone()
        if not group:
            conn.close()
            return json_response({"code": 404, "message": "group not found"})
        conn.close()
        return json_response({"name": group["location_name"] or "", "location": group["location_address"] or "", "deposit": 20.0}, code=0)
    except Exception as e:
        logger.error("[get_cabinets_by_group_code] %s" % str(e))
        return json_response({"code": 500, "message": str(e)})
import hashlib
import time
import xml.etree.ElementTree as ET

@bp.route('/wx/message', methods=['GET', 'POST'])
def wechat_message():
    """????????? - ????"""
    try:
        WX_TOKEN = 'smartlocker2024'

        if request.method == 'GET':
            signature = request.args.get('signature', '')
            timestamp = request.args.get('timestamp', '')
            nonce = request.args.get('nonce', '')
            echostr = request.args.get('echostr', '')
            tmp_list = sorted([WX_TOKEN, timestamp, nonce])
            tmp_str = hashlib.sha1(''.join(tmp_list).encode()).hexdigest()
            if tmp_str == signature:
                return echostr
            return 'verify failed', 403

        xml_data = request.data.decode('utf-8')
        root = ET.fromstring(xml_data)
        msg = {child.tag: child.text for child in root}

        from_user = msg.get('FromUserName', '')
        to_user = msg.get('ToUserName', '')
        msg_type = msg.get('MsgType', '')
        event = msg.get('Event', '')
        content_raw = msg.get('Content', msg.get('EventKey', ''))
        ts = str(int(time.time()))

        # ??????????????
        try:
            _conn_msg = get_db()
            _cur_msg = _conn_msg.cursor()
            _phone = ''
            _cur_msg.execute("SELECT phone FROM users WHERE (openid = %s OR mp_openid = %s) AND phone IS NOT NULL AND phone != '' LIMIT 1", (from_user, from_user,))
            _r_msg = _cur_msg.fetchone()
            if _r_msg:
                _phone = _r_msg[0]
            # 如果通过openid没找到手机号，尝试通过unionid查找
            if not _phone:
                try:
                    import urllib.request as _urllib_req, json as _json, logging as _logging
                    _token_url = "https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s" % (_wx_oa_id(), _wx_oa_secret())
                    _token_resp = _urllib_req.urlopen(_token_url, timeout=5)
                    _token_data = _json.loads(_token_resp.read().decode())
                    _oa_token = _token_data.get("access_token", "")
                    if _oa_token:
                        _userinfo_url = "https://api.weixin.qq.com/cgi-bin/user/info?access_token=%s&openid=%s&lang=zh_CN" % (_oa_token, from_user)
                        _info_resp = _urllib_req.urlopen(_userinfo_url, timeout=5)
                        _info_data = _json.loads(_info_resp.read().decode())
                        _unionid = _info_data.get("unionid", "")
                        if _unionid:
                            _cur_msg.execute("SELECT phone FROM users WHERE unionid = %s AND phone IS NOT NULL AND phone != '' LIMIT 1", (_unionid,))
                            _r2 = _cur_msg.fetchone()
                            if _r2:
                                _phone = _r2[0]
                except Exception as _union_err:
                    pass
            _cur_msg.execute("INSERT INTO wx_oa_messages (openid, phone, msg_type, content, event, raw_msg) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                           (from_user, _phone, msg_type, content_raw[:500], event, xml_data[:1000]))
            _msg_id_row = _cur_msg.fetchone()
            _msg_id_val = _msg_id_row[0] if _msg_id_row else None
            # [FIX-20260913] 只有"用户主动发的消息"(文字/图片/语音/视频/位置/链接)才算投诉/留言。
            #   以前 MsgType=event 也当投诉, 实测事故: 用户在存包页点"订阅"并允许 ->
            #   生成一条假投诉 -> 触发"投诉自动原路退款" + 被加进"当天投诉白名单"
            #   -> 当天结束订单被直接退款(不走余额提现)。点公众号菜单(view_miniprogram)
            #   同理, 也是假投诉, 会让投诉数虚高。
            _is_user_msg = (msg_type or '').strip().lower() in (
                'text', 'image', 'voice', 'video', 'shortvideo', 'location', 'link')
            if _phone and _msg_id_val and _is_user_msg:
                _cur_msg.execute("SELECT id FROM complaints WHERE user_phone = %s AND status = '0' ORDER BY id DESC LIMIT 1", (_phone,))
                _exist_cr = _cur_msg.fetchone()
                if _exist_cr:
                    _cid = _exist_cr[0]
                    _cur_msg.execute("UPDATE complaints SET content = content || chr(10) || %s, reply_time = NOW(), source = COALESCE(NULLIF(source, ''), 'wechat_mp') WHERE id = %s", (content_raw[:500], _cid))
                else:
                    _cur_msg.execute("INSERT INTO complaints (user_phone, content, openid, type, status, source) VALUES (%s, %s, %s, 'self', '0', 'wechat_mp') RETURNING id", (_phone, content_raw[:500], from_user))
                    _cid_row = _cur_msg.fetchone()
                    _cid = _cid_row[0] if _cid_row else 0
                if _cid:
                    _cur_msg.execute("UPDATE wx_oa_messages SET complaint_id = %s WHERE id = %s", (_cid, _msg_id_val))
            _conn_msg.commit()
            _conn_msg.close()
        except:
            pass

        def _reply(text):
            return '<xml><ToUserName><![CDATA[' + from_user + ']]></ToUserName><FromUserName><![CDATA[' + to_user + ']]></FromUserName><CreateTime>' + ts + '</CreateTime><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[' + text + ']]></Content></xml>'

        # [S371] 关注/扫码 -> 回一张【图文卡片】，标题就是「存包」，点它进存包页。
        #   链接带 10 分钟时效签名：过期后 /go 会展示"请重新扫柜机码"的页面。
        def _reply_news(title, desc, pic, url):
            return ('<xml><ToUserName><![CDATA[' + from_user + ']]></ToUserName>'
                    '<FromUserName><![CDATA[' + to_user + ']]></FromUserName>'
                    '<CreateTime>' + ts + '</CreateTime>'
                    '<MsgType><![CDATA[news]]></MsgType><ArticleCount>1</ArticleCount>'
                    '<Articles><item>'
                    '<Title><![CDATA[' + title + ']]></Title>'
                    '<Description><![CDATA[' + desc + ']]></Description>'
                    '<PicUrl><![CDATA[' + pic + ']]></PicUrl>'
                    '<Url><![CDATA[' + url + ']]></Url>'
                    '</item></Articles></xml>')

        def _store_card_url(scene):
            """把带参二维码的 scene 拼成"带 10 分钟时效"的存包链接"""
            import hmac as _hm, hashlib as _h, time as _t
            try:
                from config import SECRET_KEY as _SK
            except Exception:
                _SK = 'smart-locker-secret-key-2024'
            _dev = ''
            _sc = str(scene or '')
            if _sc.startswith('c') and _sc[1:].isdigit():
                _dev = 'c' + _sc[1:]          # 柜机 id 形式，/go 会透传
            elif _sc.startswith('d') and _sc[1:]:
                _dev = _sc[1:]
            _now = int(_t.time())
            _sig = _hm.new(_SK.encode(), ('%s|%d' % (_dev, _now)).encode(), _h.sha256).hexdigest()[:16]
            _base = _wx_h5b() or 'https://locker.cqdyxl.com'
            return '%s/go?d=%s&t=%d&s=%s' % (_base, _dev, _now, _sig)

        def _multi_news(arts):
            """多条图文；arts=[(标题, 描述, 图片URL, 链接URL), ...]"""
            _x = ('<xml><ToUserName><![CDATA[' + from_user + ']]></ToUserName>'
                  '<FromUserName><![CDATA[' + to_user + ']]></FromUserName>'
                  '<CreateTime>' + ts + '</CreateTime>'
                  '<MsgType><![CDATA[news]]></MsgType>'
                  '<ArticleCount>' + str(len(arts)) + '</ArticleCount><Articles>')
            for _t, _d, _p, _u in arts:
                _x += ('<item><Title><![CDATA[' + _t + ']]></Title>'
                       '<Description><![CDATA[' + _d + ']]></Description>'
                       '<PicUrl><![CDATA[' + _p + ']]></PicUrl>'
                       '<Url><![CDATA[' + _u + ']]></Url></item>')
            return _x + '</Articles></xml>'

        def _site_name(scene):
            """根据带参二维码的 scene 查出网点名（图里要显示）"""
            try:
                _s = str(scene or '')
                _conn = get_db()
                _cur = _conn.cursor()
                if _s.startswith('c') and _s[1:].isdigit():
                    _cur.execute('SELECT l.name AS n FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.id=%s', (_s[1:],))
                elif _s.startswith('d') and _s[1:]:
                    _cur.execute('SELECT l.name AS n FROM cabinets c LEFT JOIN locations l ON c.location_id=l.id WHERE c.mainboard_device_id=%s', (_s[1:],))
                else:
                    return ''
                _r = _cur.fetchone()
                return (_r['n'] if _r and _r.get('n') else '') or ''
            except Exception as _e:
                logger.warning('[S373] 查网点名失败: %s', _e)
                return ''

        def _news_for_scene(scene):
            import urllib.parse as _up
            _base = _wx_h5b() or 'https://locker.cqdyxl.com'
            _store = _store_card_url(scene)
            _pic = _base + '/img/card-banner.png?s=' + _up.quote(_site_name(scene))
            _i_store = _base + '/img/card-icon.png?c=' + _up.quote('存')
            _i_fetch = _base + '/img/card-icon.png?c=' + _up.quote('取')
            return _multi_news([
                ('自助存取包', '点击下方「存包」开始（10分钟内有效）', _pic, _store),
                ('STORE | 点击->存包', '', _i_store, _store),
                ('FETCH | 点击->取包', '', _i_fetch, _base + '/retrieve'),
            ])

        if msg_type == 'event':
            if event == 'subscribe':
                _ek = (msg.get('EventKey') or '')
                _scene = _ek.split('qrscene_', 1)[-1] if 'qrscene_' in _ek else ''
                return _news_for_scene(_scene)
            if event == 'SCAN':
                # [S371] 已关注的用户再扫带参二维码：微信推的是 SCAN(不是 subscribe)。
                #   以前这里没处理 -> 老用户扫码一条回复都收不到。
                _scene = (msg.get('EventKey') or '')
                return _news_for_scene(_scene)
            elif event == 'unsubscribe':
                return '', 200

        if msg_type == 'text':
            return _reply('''\u60a8\u597d\uff0c\u5df2\u8bb0\u5f55\u60a8\u7684\u7559\u8a00\uff0c\u5ba2\u670d\u4eba\u5458\u5c06\u5c3d\u5feb\u5904\u7406\u3002\u5ba2\u670d\u7535\u8bdd\uff1a4006981080''')

        return '', 200
    except Exception as e:
        logger.error(f'[\u5fae\u4fe1\u6d88\u606f] \u5904\u7406\u5931\u8d25: {e}')
        return '', 200
