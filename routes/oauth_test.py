# -*- coding: utf-8 -*-
# 澶囩敤鍏紬鍙锋巿鏉冮獙璇佹祴璇?涓存椂鎺ュ彛,楠岃瘉鍚庡垹闄?
# 鐢ㄦ硶:
#   1. 鎵撳紑: https://locker.cqdyxl.com/api/wx/oauth-test   -> 璺宠浆澶囩敤鍏紬鍙锋巿鏉?#   2. 鎺堟潈鍚庡洖鍒? https://locker.cqdyxl.com/api/wx/oauth-test/cb?code=xxx
#   3. 椤甸潰鏄剧ず澶囩敤鍏紬鍙风殑 openid + unionid
import json
import urllib.request
import urllib.parse
from flask import Blueprint, request, redirect, Response

BACKUP_APP_ID = 'wx4f65dc701e9111fa'
BACKUP_APP_SECRET = '821d8c0572c235ebeec9dbba0ff62e7a'
CB_BASE = 'https://locker.cqdyxl.com'

bp = Blueprint('oauth_test', __name__)

def page(title, body):
    html = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>%s</title></head>'
            '<body style="font-family:monospace;padding:20px;font-size:16px">'
            '<h3>%s</h3>%s</body></html>') % (title, title, body)
    return Response(html, mimetype='text/html; charset=utf-8')

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
        return page('Error', '<p>missing code</p>')
    try:
        url = ('https://api.weixin.qq.com/sns/oauth2/access_token?appid=' + BACKUP_APP_ID +
               '&secret=' + BACKUP_APP_SECRET + '&code=' + code + '&grant_type=authorization_code')
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        openid = result.get('openid', '')
        at = result.get('access_token', '')
        unionid = result.get('unionid', '')
        if 'errcode' in result:
            return page('OAuth Error', '<p>%s</p>' % json.dumps(result, ensure_ascii=True))
        wechat_name = ''
        if at and openid:
            try:
                info_url = ('https://api.weixin.qq.com/sns/userinfo?access_token=' + at +
                            '&openid=' + openid + '&lang=zh_CN')
                req2 = urllib.request.Request(info_url)
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    ui = json.loads(resp2.read().decode('utf-8'))
                unionid = unionid or ui.get('unionid', '')
                wechat_name = ui.get('nickname', '')
            except Exception as ie:
                pass
        body = ('<p>openid : <b>%s</b></p>'
                '<p>unionid: <b>%s</b></p>'
                '<p>name   : %s</p>'
                '<p style="color:#888;font-size:13px">copy unionid and send to admin to compare with main gzh database</p>') % (
                    openid, unionid, urllib.parse.quote(wechat_name))
        return page('OAuth Result', body)
    except Exception as e:
        return page('Error', '<p>%s</p>' % str(e))
