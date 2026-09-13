# -*- coding: utf-8 -*-
# 澶囩敤鍏紬鍙锋巿鏉冮獙璇佹祴璇?涓存椂鎺ュ彛,楠岃瘉鍚庡垹闄?
# 鐢ㄦ硶:
#   1. 鎵撳紑: https://locker.cqdyxl.com/api/wx/oauth-test   -> 璺宠浆澶囩敤鍏紬鍙锋巿鏉?#   2. 鎺堟潈鍚庡洖鍒? https://locker.cqdyxl.com/api/wx/oauth-test/cb?code=xxx
#   3. 椤甸潰鏄剧ず澶囩敤鍏紬鍙风殑 openid + unionid, 涓庝富鍏紬鍙峰簱閲岀殑 unionid 瀵规瘮
import logging
import json
import urllib.request
import urllib.parse
from flask import Blueprint, request, redirect, jsonify
from wx_config import (h5_base as _wx_h5b, h5_store as _wx_h5s, oauth_callback as _wx_oauthcb,
                     ws_base as _wx_ws, pay_notify_url as _wx_payurl)   # [CFG-STEP2C] 域名改从配置中心读，读不到自动用 config.py 原值

BACKUP_APP_ID = 'wx4f65dc701e9111fa'
BACKUP_APP_SECRET = '821d8c0572c235ebeec9dbba0ff62e7a'
CB_BASE = _wx_h5b()

bp = Blueprint('oauth_test', __name__)

@bp.route('/wx/oauth-test', methods=['GET'])
def oauth_test_start():
    redirect_uri = CB_BASE + '/api/wx/oauth-test/cb'
    url = ('https://open.weixin.qq.com/connect/oauth2/authorize?appid=' + BACKUP_APP_ID +
           '&redirect_uri=' + urllib.parse.quote(redirect_uri, safe='') +
           '&response_type=code&scope=snsapi_userinfo&state=backup#wechat_redirect')
    return redirect(url)

@bp.route('/wx/oauth-test/cb', methods=['GET'])
def oauth_test_cb():
    code = request.args.get('code', '')
    if not code:
        return '<h3>缂哄皯code</h3>'
    try:
        url = ('https://api.weixin.qq.com/sns/oauth2/access_token?appid=' + BACKUP_APP_ID +
               '&secret=' + BACKUP_APP_SECRET + '&code=' + code + '&grant_type=authorization_code')
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        openid = result.get('openid', '')
        at = result.get('access_token', '')
        unionid = result.get('unionid', '')
        wechat_name = ''
        if at and openid:
            info_url = ('https://api.weixin.qq.com/sns/userinfo?access_token=' + at +
                        '&openid=' + openid + '&lang=zh_CN')
            req2 = urllib.request.Request(info_url)
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                ui = json.loads(resp2.read().decode('utf-8'))
            unionid = unionid or ui.get('unionid', '')
            wechat_name = ui.get('nickname', '')
        html = ('<meta charset="utf-8"><h3>澶囩敤鍏紬鍙锋巿鏉冪粨鏋?/h3>'
                '<p>openid: <b>%s</b></p>'
                '<p>unionid: <b>%s</b></p>'
                '<p>鏄电О: %s</p>'
                '<p>鎺堟潈鍝嶅簲鍘熷: %s</p>'
                '<p>璇存槑: 鎷胯繖涓猽nionid鍘诲簱閲屾煡 users琛?orders琛?鏈夋病鏈夊悓鏍?unionid 鐨勭敤鎴? 鏈?缁戝畾寮€鏀惧钩鍙版垚鍔? 鏃?娌＄粦涓?/p>') % (
                    openid, unionid, wechat_name, json.dumps(result, ensure_ascii=False))
        return html
    except Exception as e:
        return '<h3>閿欒: %s</h3>' % str(e)
