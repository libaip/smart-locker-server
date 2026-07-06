"""
微信OAuth/网页授权 - Blueprint
"""
import logging
import json
import urllib.request
import urllib.parse
from flask import Blueprint, request, redirect, jsonify
from config import WX_APP_ID as WX_OA_ID, WX_APP_SECRET as WX_OA_SECRET, WX_MP_APP_ID, WX_MP_APP_SECRET
from database import get_db
from helpers import json_response, logger

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
        token_url = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s' % (WX_MP_APP_ID, WX_MP_APP_SECRET)
        resp = urllib.request.urlopen(token_url, timeout=10)
        token_data = json_lib.loads(resp.read().decode())
        token = token_data.get('access_token', '')
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
