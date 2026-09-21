# -*- coding: utf-8 -*-
"""
S507-20260921 小程序客服自动回复（消息推送 webhook）
====================================================
老板要求：「在线客服我只需要自动回复问题就行了，或者进去发几个固定消息让用户选择」。

做法：在 routes/user.py 末尾【纯新增】两个端点，配置到小程序后台的「消息推送」：

  GET  /api/mp/push   微信服务器校验（signature/timestamp/nonce/echostr）
  POST /api/mp/push   接收用户消息/事件 → 自动回复
       · 事件 user_enter_tempsession（用户进入客服会话）→ 用客服消息接口推"编号菜单"
       · 文本消息 → 关键词/编号匹配 → 被动回复（同格式返回，不调接口、不占用额度）
       · 其它类型 → 兜底引导

后台配置（老板做）：
  开发管理 → 消息推送 → 启用
  URL   = https://kelaiwei.top/api/mp/push   （或 https://locker.cqdyxl.com/api/mp/push）
  Token = system_settings.mp_push_token（本次写入 locker_mp_push_2026）
  数据格式 = JSON 或 XML 都支持（本实现两种都兼容）
  加密方式 = **明文模式**（选安全模式需要额外配 EncodingAESKey，本实现未含解密）

Token 放 system_settings，可随时改，不用改代码、不用重启。
"""
import hashlib
import py_compile
import shutil

P = 'routes/user.py'
MARK = 'S507-20260921 小程序客服自动回复'

src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert MARK not in src, '已打过该补丁，中止'

CODE = '''

# ============================================================
# [S507-20260921] 小程序客服自动回复（消息推送 webhook）
#   老板要求：客服能自动回复 + 进去就给几个固定选项让用户选。
#   纯新增，不改动任何既有接口。Token 存在 system_settings.mp_push_token。
# ============================================================
_MP_PUSH_TOKEN_DEFAULT = 'locker_mp_push_2026'

_MP_MENU = ('您好，智能寄存柜为您服务 👋\\n'
            '请回复数字选择：\\n'
            '1 押金 / 预付款退款\\n'
            '2 取件、取包、取件码\\n'
            '3 柜门打不开 / 设备故障\\n'
            '4 人工客服')

_MP_RULES = [
    (('1', '押金', '预付款', '退款', '没退', '退钱', '没到账', '原路', '退到'),
     '【押金/预付款】您的押金是「原路退回微信零钱」，1~3 分钟内到账，不需要在小程序里提现。\\n'
     '若超过 30 分钟仍未到账，请回复「4」转人工，或直接拨 4006981080，我们马上为您核实补退。'),
    (('2', '取件', '取包', '取件码', '密码', '怎么取', '取不出来'),
     '【取件/取包】两种方式：\\n'
     '① 在小程序首页点「取包」，输入存包时收到的取件码；\\n'
     '② 在柜机屏幕上点「密码取包」，输入取件码。\\n'
     '取件码可在小程序「我的订单」里查看。取不出来请回复「3」。'),
    (('3', '打不开', '开不了', '柜门', '故障', '没弹开', '卡住'),
     '【柜门打不开】请先在柜机上重试一次，等 10 秒再开；\\n'
     '若仍不开：请把「柜机编号 + 柜门号」发给我们，我们远程开门；\\n'
     '急用请直接拨 4006981080（08:30-21:00）。'),
    (('4', '人工', '客服电话', '转人工', '投诉'),
     '【人工客服】电话 4006981080（08:30-21:00）。\\n'
     '也可以在小程序「投诉商家/投诉反馈」里提交，写明手机号和订单号，我们会在 30 分钟内核实处理（押金问题会原路退回）。'),
]


def _mp_push_token():
    """取小程序消息推送 Token（库里有就用库里的，改配置不用重启）"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT setting_value FROM system_settings WHERE setting_key='mp_push_token'")
        r = cur.fetchone()
        conn.close()
        if r:
            v = (r.get('setting_value') if isinstance(r, dict) else r[0]) or ''
            if str(v).strip():
                return str(v).strip()
    except Exception as e:
        logger.warning('[mp_push] 读 token 失败(用默认): %s', e)
    return _MP_PUSH_TOKEN_DEFAULT


def _mp_push_sign_ok(token, timestamp, nonce, signature):
    import hashlib as _hl
    raw = ''.join(sorted([str(token or ''), str(timestamp or ''), str(nonce or '')]))
    return _hl.sha1(raw.encode('utf-8')).hexdigest() == str(signature or '')


def _mp_auto_reply(text):
    """按关键词/编号给出回复；命中不了就给菜单"""
    t = (text or '').strip()
    if not t:
        return _MP_MENU
    low = t.lower()
    for keys, reply in _MP_RULES:
        for k in keys:
            if k in t or k in low:
                return reply
    return '没太明白您的问题，请回复数字选择，或直接拨 4006981080 转人工。\\n\\n' + _MP_MENU


def _mp_kf_send(openid, content):
    """用客服消息接口主动发一条文本（用于"用户进入会话"时推菜单）"""
    try:
        import requests as _rq
        from helpers import get_access_token_for as _gat
        tok = _gat(_wx_mp_id(), _wx_mp_secret())
        if not tok:
            logger.error('[mp_push] 拿不到小程序 token，无法推送菜单')
            return False
        r = _rq.post('https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=%s' % tok,
                     json={'touser': openid, 'msgtype': 'text', 'text': {'content': content}}, timeout=8)
        j = r.json() if r is not None else {}
        if j.get('errcode') in (0, None):
            logger.info('[mp_push] 已向 openid=%s... 推送菜单', str(openid)[:8])
            return True
        logger.warning('[mp_push] 客服消息发送失败: %s', j)
        return False
    except Exception as e:
        logger.error('[mp_push] 客服消息发送异常: %s', e)
        return False


def _mp_parse_push(body, ctype):
    """把微信推来的消息解析成 dict（JSON 与 XML 两种数据格式都兼容）"""
    import json as _json
    data = {}
    ctype = (ctype or '').lower()
    if 'json' in ctype or (body or '').strip().startswith('{'):
        try:
            data = _json.loads(body or '{}')
        except Exception as e:
            logger.warning('[mp_push] JSON 解析失败: %s', e)
            data = {}
    else:
        try:
            import xml.etree.ElementTree as _ET
            root = _ET.fromstring(body or '<xml/>')
            for ch in list(root):
                data[ch.tag] = ch.text or ''
        except Exception as e:
            logger.warning('[mp_push] XML 解析失败: %s', e)
            data = {}
    return data


def _mp_build_reply(data, content, as_json):
    """组装被动回复（同格式返回，不用调接口）"""
    import json as _json
    import time as _t
    payload = {
        'ToUserName': data.get('FromUserName', ''),
        'FromUserName': data.get('ToUserName', ''),
        'CreateTime': int(_t.time()),
        'MsgType': 'text',
        'Content': content,
    }
    if as_json:
        return _json.dumps(payload, ensure_ascii=False), 'application/json'
    xml = ('<xml><ToUserName><![CDATA[%s]]></ToUserName>'
           '<FromUserName><![CDATA[%s]]></FromUserName>'
           '<CreateTime>%s</CreateTime><MsgType><![CDATA[text]]></MsgType>'
           '<Content><![CDATA[%s]]></Content></xml>'
           % (payload['ToUserName'], payload['FromUserName'], payload['CreateTime'], content))
    return xml, 'application/xml'


@bp.route('/mp/push', methods=['GET'])
def mp_push_verify():
    """微信服务器校验（保存消息推送配置时会 GET 一次）"""
    token = _mp_push_token()
    ts = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    sig = request.args.get('signature', '')
    echo = request.args.get('echostr', '')
    if _mp_push_sign_ok(token, ts, nonce, sig):
        logger.info('[mp_push] URL 校验通过')
        return echo or 'ok'
    logger.warning('[mp_push] URL 校验失败 token_len=%s ts=%s nonce=%s', len(token or ''), ts, nonce)
    return 'invalid signature', 403


@bp.route('/mp/push', methods=['POST'])
def mp_push_receive():
    """接收用户消息/事件并自动回复"""
    try:
        body = request.get_data(as_text=True)
        ctype = request.headers.get('Content-Type', '')
        data = _mp_parse_push(body, ctype)
        as_json = ('json' in (ctype or '').lower()) or (body or '').strip().startswith('{')
        msg_type = (data.get('MsgType') or '').lower()
        event = (data.get('Event') or '').lower()
        openid = data.get('FromUserName', '') or ''
        content = data.get('Content', '') or ''
        logger.info('[mp_push] 收到 MsgType=%s Event=%s openid=%s... content=%s',
                    msg_type, event, str(openid)[:8], (content or '')[:60])

        # 1) 用户进入客服会话 → 主动推一条编号菜单（必须调客服消息接口）
        if msg_type == 'event' and event in ('user_enter_tempsession', 'user_enter_session'):
            _mp_kf_send(openid, _MP_MENU)
            return 'success'

        # 2) 文本消息 → 被动回复
        if msg_type == 'text':
            reply = _mp_auto_reply(content)
            out, mime = _mp_build_reply(data, reply, as_json)
            return out, 200, {'Content-Type': mime}

        # 3) 其它类型（图片/小程序卡片等）→ 引导
        out, mime = _mp_build_reply(data, '已收到您的消息，请用文字描述问题，或回复数字选择；急事请拨 4006981080。', as_json)
        return out, 200, {'Content-Type': mime}
    except Exception as e:
        logger.error('[mp_push] 处理异常: %s', e)
        return 'success'
'''

src2 = src.rstrip('\n') + '\n' + CODE
shutil.copy2(P, P + '.bak_s507')
open(P, 'w', encoding='utf-8').write(src2)
after = hashlib.md5(src2.encode('utf-8')).hexdigest()
py_compile.compile(P, doraise=True)

print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('新增端点: /mp/push GET=%d POST=%d' % (src2.count("@bp.route('/mp/push', methods=['GET'])"),
                                             src2.count("@bp.route('/mp/push', methods=['POST'])")))
