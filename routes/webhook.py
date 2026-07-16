"""
微信OAuth/网页授权 - Blueprint
"""
import logging
import json
import urllib.request
import urllib.parse
from flask import Blueprint, request, redirect, jsonify
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
            url = f'https://api.weixin.qq.com/sns/oauth2/access_token?appid={WX_OA_ID}&secret={WX_OA_SECRET}&code={code}&grant_type=authorization_code'
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
            oauth_callback = 'https://locker.cqdyxl.com/api/wx/oauth'
            oauth_redirect = f'https://open.weixin.qq.com/connect/oauth2/authorize?appid={WX_OA_ID}&redirect_uri={urllib.parse.quote(oauth_callback + "?redirect_uri=" + urllib.parse.quote(redirect_uri, safe=""))}&response_type=code&scope=snsapi_userinfo&state=locker#wechat_redirect'
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
        token = get_access_token()
        if not token:
            return jsonify({'code': 500, 'message': 'token failed'})
        scheme_url = 'https://api.weixin.qq.com/wxa/generatescheme?access_token=' + token
        body = json_lib.dumps({
            'jump_wxa': {'path': path, 'query': req_data.get('query', '')},
            'expire_type': 1,
            'expire_interval': 365
        }).encode()
        req = urllib.request.Request(scheme_url, data=body, headers={'Content-Type': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        result = json_lib.loads(resp.read().decode())
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
            _cur_msg.execute("SELECT phone FROM phone_openids WHERE (openid = %s OR mp_openid = %s) AND phone IS NOT NULL AND phone != '' LIMIT 1", (from_user, from_user,))
            _r_msg = _cur_msg.fetchone()
            if _r_msg:
                _phone = _r_msg[0]
            # 如果通过openid没找到手机号，尝试通过unionid查找
            if not _phone:
                try:
                    import urllib.request as _urllib_req, json as _json, logging as _logging
                    _token_url = "https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s" % (WX_OA_ID, WX_OA_SECRET)
                    _token_resp = _urllib_req.urlopen(_token_url, timeout=5)
                    _token_data = _json.loads(_token_resp.read().decode())
                    _oa_token = _token_data.get("access_token", "")
                    if _oa_token:
                        _userinfo_url = "https://api.weixin.qq.com/cgi-bin/user/info?access_token=%s&openid=%s&lang=zh_CN" % (_oa_token, from_user)
                        _info_resp = _urllib_req.urlopen(_userinfo_url, timeout=5)
                        _info_data = _json.loads(_info_resp.read().decode())
                        _unionid = _info_data.get("unionid", "")
                        if _unionid:
                            _cur_msg.execute("SELECT phone FROM phone_openids WHERE unionid = %s AND phone IS NOT NULL AND phone != '' LIMIT 1", (_unionid,))
                            _r2 = _cur_msg.fetchone()
                            if _r2:
                                _phone = _r2[0]
                except Exception as _union_err:
                    pass
            _cur_msg.execute("INSERT INTO wx_oa_messages (openid, phone, msg_type, content, event, raw_msg) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                           (from_user, _phone, msg_type, content_raw[:500], event, xml_data[:1000]))
            _msg_id_row = _cur_msg.fetchone()
            _msg_id_val = _msg_id_row[0] if _msg_id_row else None
            if _phone and _msg_id_val:
                _cur_msg.execute("SELECT id FROM complaints WHERE user_phone = %s AND status = '0' ORDER BY id DESC LIMIT 1", (_phone,))
                _exist_cr = _cur_msg.fetchone()
                if _exist_cr:
                    _cid = _exist_cr[0]
                    _cur_msg.execute("UPDATE complaints SET content = content || chr(10) || %s, reply_time = NOW() WHERE id = %s", (content_raw[:500], _cid))
                else:
                    _cur_msg.execute("INSERT INTO complaints (user_phone, content, openid, type, status) VALUES (%s, %s, %s, 'self', '0') RETURNING id", (_phone, content_raw[:500], from_user))
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

        if msg_type == 'event':
            if event == 'subscribe':
                return _reply('''\u6b22\u8fce\u5173\u6ce8\u667a\u80fd\u5bc4\u5b58\u67dc\uff01\u70b9\u51fb\u83dc\u5355\u5373\u53ef\u4f7f\u7528\u5bc4\u5b58\u670d\u52a1\u3002\u5ba2\u670d\u7535\u8bdd\uff1a4006981080''')
            elif event == 'unsubscribe':
                return '', 200

        if msg_type == 'text':
            return _reply('''\u60a8\u597d\uff0c\u5df2\u8bb0\u5f55\u60a8\u7684\u7559\u8a00\uff0c\u5ba2\u670d\u4eba\u5458\u5c06\u5c3d\u5feb\u5904\u7406\u3002\u5ba2\u670d\u7535\u8bdd\uff1a4006981080''')

        return '', 200
    except Exception as e:
        logger.error(f'[\u5fae\u4fe1\u6d88\u606f] \u5904\u7406\u5931\u8d25: {e}')
        return '', 200
