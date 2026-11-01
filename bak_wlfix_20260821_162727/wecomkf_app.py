# -*- coding: utf-8 -*-
"""
企业微信「微信客服」消息回调示例（Python + Flask）

功能：
  1. GET  /wecom/kf  回调 URL 校验（企微后台保存时自动触发）
  2. POST /wecom/kf  接收 kf_msg_or_event 事件
                    -> 调用 sync_msg 拉取用户消息
                    -> 入库 + 自动回复

运行：python app.py（默认 5000 端口）
详细步骤见同目录 README.md
"""
import hashlib
import json
import logging
import struct
import sys
import time
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode

import psycopg2
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from flask import Flask, request

# ==================== 配置区（2026-08-19 生产部署） ====================
CORP_ID = "wwf6076f4e15de2e3f"                          # 企微企业ID
KF_SECRET = "TjBORfUwDY5uT6xxMvFQODMWEZLVqqrSWOCHCUJWIGk"  # 微信客服 Secret
TOKEN = "kelaiweiKfToken2026"                            # 回调 Token（填到企微后台）
ENCODING_AES_KEY = "LufhjYGdfsGslailjQ51SmrV1RddqFKnRrW3Ewm8aJ5"  # 43位（填到企微后台）
OPEN_KFID = "wkKZA5bwAA_4IEIwMX7CT4pRZU1caWjQ"  # 客服账号ID（open_kfid，wk开头，API实测 2026-08-19）
# =================================================================

# 自动匹配订单开关：企微「微信开发者ID」绑定科莱智小程序后改为 True（可拿 unionid 免输手机号）
AUTO_MATCH_UNIONID = False

# 业务库连接（与 smart-locker 共用）
DB_CFG = {"host": "127.0.0.1", "port": 6432, "user": "locker_admin", "password": "locker_pass_2024", "dbname": "smart_locker"}

# 多轮会话状态（内存）：external_userid -> {"stage": ..., "orders": [...]}
session_ctx = {}
import threading
_ctx_lock = threading.Lock()


def _ensure_kf_table():
    """已处理消息去重表（持久化，防服务重启后重复回复）"""
    try:
        conn = psycopg2.connect(**DB_CFG)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS kf_processed_msg (
            msgid VARCHAR(128) PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning("建去重表失败: %s", e)


def is_processed(msgid):
    """msgid 已处理过返回 True（并登记）；未处理过返回 False。失败时放行（不阻塞业务）"""
    if not msgid:
        return False
    try:
        conn = psycopg2.connect(**DB_CFG)
        c = conn.cursor()
        c.execute("INSERT INTO kf_processed_msg (msgid) VALUES (%s) ON CONFLICT (msgid) DO NOTHING", (msgid,))
        conn.commit()
        inserted = c.rowcount
        conn.close()
        return inserted == 0
    except Exception as e:
        logging.warning("去重检查失败: %s", e)
        return False


_ensure_kf_table()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_token_cache = {"token": None, "expire": 0}


# ---------------- access_token ----------------
def get_access_token():
    """获取企业微信 access_token（使用微信客服的 secret），缓存到过期前"""
    if _token_cache["token"] and _token_cache["expire"] > time.time() + 60:
        return _token_cache["token"]
    r = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": CORP_ID, "corpsecret": KF_SECRET},
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        raise RuntimeError(f"gettoken 失败: {r}")
    _token_cache["token"] = r["access_token"]
    _token_cache["expire"] = time.time() + r["expires_in"]
    logging.info("access_token 刷新成功")
    return r["access_token"]


# ---------------- 回调加解密 ----------------
def verify_signature(signature, timestamp, nonce, encrypt):
    """msg_signature = sha1( sort(token, timestamp, nonce, encrypt) )"""
    s = "".join(sorted([TOKEN, timestamp, nonce, encrypt]))
    return hashlib.sha1(s.encode("utf-8")).hexdigest() == signature


def decrypt_msg(encrypt):
    """AES-256-CBC 解密：random(16) + msg_len(4) + msg + receiveid，PKCS7 补位（cryptography 实现）"""
    key = b64decode(ENCODING_AES_KEY + "=")
    iv = key[:16]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plain = decryptor.update(b64decode(encrypt)) + decryptor.finalize()
    pad = plain[-1]
    content = plain[:-pad]
    # 企微格式：前16字节为随机数，跳过；[16:20] 为消息长度；[20:20+len] 为消息内容
    xml_len = struct.unpack("!i", content[16:20])[0]
    return content[20 : 20 + xml_len].decode("utf-8")


# ---------------- 微信客服消息收发 ----------------
def sync_kf_messages(event_token, open_kfid, cursor=""):
    """用事件里的 Token 拉取用户消息（Token 有效期约 60 秒，要尽快调用）"""
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg",
        params={"access_token": get_access_token()},
        json={"cursor": cursor, "token": event_token, "limit": 1000, "open_kfid": open_kfid},
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        logging.error("sync_msg 失败: %s", r)
        return [], ""
    return r.get("msg_list", []), r.get("next_cursor", "")


def trans_kf_session(external_userid, open_kfid):
    """接入会话：将用户会话状态从「未处理」变更为「待接入」，send_msg 才允许发送（否则报 95018）"""
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/service_state/trans",
        params={"access_token": get_access_token()},
        json={
            "service_state": 2,          # 2=待接入
            "open_kfid": open_kfid,
            "external_userid": external_userid,
        },
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        logging.warning("接入会话失败: %s", r)
    return r


def send_text(external_userid, open_kfid, content):
    """给用户回复一条文本消息"""
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg",
        params={"access_token": get_access_token()},
        json={
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "text",
            "text": {"content": content},
        },
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        logging.error("send_msg 失败: %s", r)
    return r


# ---------------- 业务处理 ----------------
WELCOME_MENU = """你好，感谢使用寄存平台，如需帮助请回复序号（只回复数字或者关键字）

1 存包

2 取包

3 如何收费

4 如何付款

5 打不开柜子

6 退预付款

7 预付款未到账"""

# 小程序卡片配置（科莱智）
MP_APPID = "wx57eaea52dcfff4e8"                       # 科莱智小程序 appid
MP_WALLET_PATH = "pages/wallet/wallet"                # 我的钱包页
MP_THUMB_MEDIA_ID = "3ChSqqm5vOditGAAXEpCIHV15XKyux9X1YXP6hHAWxISTeZYv6bwD62xCYzTKhUM8"  # 缩略图（上传素材获取）
MP_CARD_ENABLED = False                                # 小程序卡片开关：企微绑定小程序成功后改为 True

MENU_ANSWERS = {
    "1": "存包流程：选择网点 -> 小程序/扫码下单并支付 -> 柜门自动打开存入物品。取件时凭取件码或手机号取件。",
    "2": "取包流程：在柜机输入取件码或手机号，或在小程序订单页点「取件」，柜门打开取走物品即可。",
    "3": "不同网点收费标准不同（常见 10~25 元/次；按时长计费的网点按实际时间结算），以下单页面显示金额为准。",
    "4": "微信支付。下单时直接支付，取件后申请退款即可退回。",
    "5": "请拨打客服电话 4006981080，我们会为您远程开柜。",
    "6": "预付款：订单结束后，您进入微信小程序-【科莱智】-点击【我的钱包】-点击【提现】，点击【全部确认】就可以了，会在24小时内到账。",
    "7": "提现未到账：请输入支付使用时的手机号，帮您提交人工处理",
}


def send_miniprogram(external_userid, open_kfid, title, pagepath):
    """发送小程序卡片消息（点击直达小程序页面）"""
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg",
        params={"access_token": get_access_token()},
        json={
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "miniprogram",
            "miniprogram": {
                "appid": MP_APPID,
                "title": title,
                "pagepath": pagepath,
                "thumb_media_id": MP_THUMB_MEDIA_ID,
            },
        },
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        logging.error("send_miniprogram 失败: %s", r)
    return r


# ---------------- 提现未到账自动退款（第7项） ----------------
def query_today_orders(phone):
    """查询某手机号当天的订单（未退款且已支付）"""
    conn = psycopg2.connect(**DB_CFG)
    c = conn.cursor()
    c.execute(
        """SELECT order_no, deposit_amount, created_at FROM orders
           WHERE user_phone=%s AND created_at >= CURRENT_DATE
             AND COALESCE(refund_status,'') NOT IN ('refunded','success')
             AND COALESCE(transaction_id,'') != ''
           ORDER BY created_at LIMIT 20""",
        (phone,),
    )
    rows = c.fetchall()
    conn.close()
    return [(r[0], float(r[1] or 0), r[2].strftime('%H:%M') if r[2] else '') for r in rows]


def record_kf_auto(phone, order_no, content, status, reply):
    """客服自动处理记录 -> 写入 complaints 表（后台「客服自动处理」栏可见）"""
    try:
        conn = psycopg2.connect(**DB_CFG)
        c = conn.cursor()
        c.execute(
            "INSERT INTO complaints (user_phone, type, content, order_no, complaint_type, status, reply, reply_time) VALUES (%s,'kf',%s,%s,'kf_auto',%s,%s,NOW())",
            (phone or '', content or '', order_no or '', status, reply or ''),
        )
        conn.commit()
        conn.close()
        logging.info("客服自动处理已记录 order_no=%s status=%s", order_no, status)
    except Exception as e:
        logging.warning("记录客服自动处理失败: %s", e)


def refund_order(order_no):
    """微信原路退款（复用 smart-locker 的 helpers.do_real_refund），并记录到 complaints"""
    try:
        conn = psycopg2.connect(**DB_CFG)
        c = conn.cursor()
        c.execute(
            "SELECT id, deposit_amount, refund_status, payment_channel_id, status, refund_id, user_phone FROM orders WHERE order_no=%s LIMIT 1",
            (order_no,),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return False, "订单不存在"
        phone = row[6] or ''
        if row[2] in ('refunded', 'success') or (row[4] == 4 and row[5]):
            return False, "订单已退款，无需重复退"
        if not row[3]:
            record_kf_auto(phone, order_no, "客服自动退款", 2, f"退款失败:无支付渠道")
            return False, "订单无支付渠道，无法原路退"
        sys.path.insert(0, "/home/ubuntu/smart-locker")
        from helpers import do_real_refund
        # 数据库连接瞬断（pgbouncer 拥堵/被回收）时自动重试1次，避免退款漏执行
        _retried = False
        while True:
            success, refund_id, msg = do_real_refund(
                order_id=row[0], order_no=order_no, amount=float(row[1] or 0),
                payment_channel_id=row[3],
            )
            if not success and not _retried and msg and ('server closed' in str(msg) or 'connection' in str(msg).lower()):
                _retried = True
                logging.warning("退款连接瞬断，1秒后重试 order_no=%s msg=%s", order_no, msg)
                time.sleep(1)
                continue
            break
        if success:
            record_kf_auto(phone, order_no, f"客服自动退款 金额{float(row[1] or 0):.2f}元", 3, "退款成功")
        else:
            record_kf_auto(phone, order_no, "客服自动退款", 2, f"退款失败:{msg}")
        return success, refund_id or msg
    except Exception as e:
        logging.exception("退款异常 %s: %s", order_no, e)
        return False, f"退款异常:{e}"


def send_confirm_menu(external_userid, open_kfid, head):
    """发送确认/取消按钮菜单（提现退款确认用）"""
    menu = {
        "head_content": head,
        "list": [
            {"type": "click", "click": {"id": "confirm", "content": "确认退款"}},
            {"type": "click", "click": {"id": "cancel", "content": "取消"}},
        ],
        "tail_content": "",
    }
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg",
        params={"access_token": get_access_token()},
        json={"touser": external_userid, "open_kfid": open_kfid, "msgtype": "msgmenu", "msgmenu": menu},
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        logging.error("send_confirm_menu 失败: %s", r)
    return r


def do_refund_batch(order_nos):
    """批量退款，返回结果文本"""
    results = []
    for ono in order_nos:
        ok, msg = refund_order(ono)
        results.append(f"{ono}: {'退款成功✅' if ok else '退款失败❌ ' + str(msg)}")
    return "已为您提交退款，预计30分钟内到账，请注意查收。\n" + "\n".join(results) + "\n如有疑问请拨打 4006981080"


def get_customer_unionid(external_userid):
    """获取微信客户 unionid（前提：企微已绑定小程序/公众号，即微信开发者ID）"""
    try:
        r = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/kf/customer/get",
            params={"access_token": get_access_token(), "external_userid": external_userid},
            timeout=10,
        ).json()
        if r.get("errcode") == 0:
            return r.get("unionid", "")
        logging.warning("获取客户信息失败: %s", r)
    except Exception as e:
        logging.warning("获取客户信息异常: %s", e)
    return ""


def get_phone_by_unionid(unionid):
    """unionid -> 手机号（users / phone_openids 两表都查）"""
    if not unionid:
        return ""
    try:
        conn = psycopg2.connect(**DB_CFG)
        c = conn.cursor()
        c.execute("SELECT phone FROM users WHERE unionid=%s AND phone IS NOT NULL AND phone != '' ORDER BY id DESC LIMIT 1", (unionid,))
        row = c.fetchone()
        if not row:
            c.execute("SELECT phone FROM phone_openids WHERE unionid=%s AND phone IS NOT NULL AND phone != '' ORDER BY id DESC LIMIT 1", (unionid,))
            row = c.fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception as e:
        logging.warning("unionid查手机号异常: %s", e)
        return ""


def process_user_text(external_userid, text):
    """多轮对话主流程，返回回复文本"""
    ctx = session_ctx.get(external_userid) or {}
    stage = ctx.get("stage", "")

    # 阶段1：等待手机号
    if stage == "await_phone":
        if len(text) == 11 and text.isdigit():
            orders = query_today_orders(text)
            if orders:
                lines = [f"{i+1}. 订单号 {o[0]}  金额 {o[1]:.2f} 元  时间 {o[2]}" for i, o in enumerate(orders)]
                head = "为您查询到以下订单：\n" + "\n".join(lines) + "\n\n请选择："
                session_ctx[external_userid] = {"stage": "await_confirm", "orders": [o[0] for o in orders]}
                return ("__CONFIRM_MENU__", head)   # 上层发确认/取消按钮菜单
            session_ctx.pop(external_userid, None)
            return "未查询到您当天的订单。请提供订单号，或拨打人工客服 4006981080"
        return "请输入 11 位手机号（支付时使用的手机号）"

    # 阶段2：等待确认退款
    if stage == "await_confirm":
        if "确认" in text:
            order_nos = ctx.get("orders", [])
            session_ctx.pop(external_userid, None)
            return do_refund_batch(order_nos)
        session_ctx.pop(external_userid, None)
        return "已取消本次操作。如需帮助请回复菜单序号。"

    # 提现未到账入口（优先自动匹配：免输手机号）
    if text == "7" or any(k in text for k in ("提现", "未到账", "没到账", "预付款未到账")):
        if AUTO_MATCH_UNIONID:
            unionid = get_customer_unionid(external_userid)
            phone = get_phone_by_unionid(unionid)
            if phone:
                orders = query_today_orders(phone)
                if orders:
                    lines = [f"{i+1}. 订单号 {o[0]}  金额 {o[1]:.2f} 元  时间 {o[2]}" for i, o in enumerate(orders)]
                    head = "已为您自动匹配当天订单：\n" + "\n".join(lines) + "\n\n请选择："
                    session_ctx[external_userid] = {"stage": "await_confirm", "orders": [o[0] for o in orders]}
                    return ("__CONFIRM_MENU__", head)
        session_ctx[external_userid] = {"stage": "await_phone"}
        return "请输入支付使用时的手机号，帮您提交人工处理"

    # 直接发 20 位订单号：按订单号退款
    if len(text) >= 15 and text.isdigit():
        ok, msg = refund_order(text)
        if ok:
            return f"订单 {text} 退款成功，将原路退回微信，请注意查收。"
        return f"订单 {text} 处理失败：{msg}。请拨打人工客服 4006981080。"
    return None


def auto_reply(msg):
    """自动回复规则：先匹配菜单序号/关键字，再匹配常见问题，最后兜底话术"""
    content = (msg.get("text") or {}).get("content", "").strip()
    if not content:
        return WELCOME_MENU
    # 1) 菜单按钮自动输入的格式（如"6 退预付款"、"7 预付款未到账"）：按首数字精确命中，避免关键字误伤
    first = content[0]
    if first in MENU_ANSWERS and (len(content) == 1 or content[1] in (' ', '\u3000', '\n')):
        return MENU_ANSWERS[first]
    # 2) 菜单序号
    if content in MENU_ANSWERS:
        return MENU_ANSWERS[content]
    # 3) 关键字匹配（数字或关键词都能触发）
    for key, answer in MENU_ANSWERS.items():
        kw_map = {
            "1": ["存包", "存东西", "怎么存", "我要存"],
            "2": ["取包", "取件", "怎么取", "我要取"],
            "3": ["收费", "价格", "多少钱", "费用"],
            "4": ["付款", "支付", "怎么付"],
            "5": ["打不开", "开不了", "柜门", "开柜", "卡住了"],
            "6": ["退款", "退钱", "退预付款", "押金"],
            "7": ["提现", "未到账", "没到账"],
        }
        if any(k in content for k in kw_map[key]):
            return answer
    # 3) 人工
    if "人工" in content or "客服" in content:
        return "人工客服请拨打4006981080"
    # 4) 兜底：返回 None，由上层发送按钮菜单
    return None


# 菜单消息（可点击按钮，msgtype=msgmenu，官方格式）
MENU_MSG = {
    "head_content": "你好，感谢使用寄存平台，请点击选择或回复序号：\n",
    "list": [
        {"type": "click", "click": {"id": "1", "content": "1 存包"}},
        {"type": "click", "click": {"id": "2", "content": "2 取包"}},
        {"type": "click", "click": {"id": "3", "content": "3 如何收费"}},
        {"type": "click", "click": {"id": "4", "content": "4 如何付款"}},
        {"type": "click", "click": {"id": "5", "content": "5 打不开柜子"}},
        {"type": "click", "click": {"id": "6", "content": "6 退预付款"}},
        {"type": "click", "click": {"id": "7", "content": "7 预付款未到账"}},
    ],
    "tail_content": "\n或直接输入数字/描述您的问题",
}


def send_menu(external_userid, open_kfid):
    """发送菜单消息（按钮可点击）"""
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg",
        params={"access_token": get_access_token()},
        json={
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "msgmenu",
            "msgmenu": MENU_MSG,
        },
        timeout=10,
    ).json()
    if r.get("errcode") != 0:
        logging.error("send_menu 失败: %s", r)
    return r


def send_welcome_on_event(code, open_kfid):
    """进入会话事件响应：发送按钮菜单（msgmenu 官方格式；若事件响应不支持则回退文本）"""
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg_on_event",
        params={"access_token": get_access_token()},
        json={
            "code": code,
            "open_kfid": open_kfid,
            "msgtype": "msgmenu",
            "msgmenu": MENU_MSG,
        },
        timeout=10,
    ).json()
    if r.get("errcode") == 40008:   # 事件响应不支持菜单 → 回退文本欢迎语
        logging.warning("事件响应不支持msgmenu，回退文本: %s", r)
        r = requests.post(
            "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg_on_event",
            params={"access_token": get_access_token()},
            json={
                "code": code,
                "open_kfid": open_kfid,
                "msgtype": "text",
                "text": {"content": WELCOME_MENU},
            },
            timeout=10,
        ).json()
    if r.get("errcode") != 0:
        logging.warning("send_msg_on_event 失败: %s", r)
    else:
        logging.info("进入会话欢迎菜单已发送")
    return r


def handle_kf_event(token, open_kfid):
    """事件回调主流程：拉消息 -> 去重 -> 入库 -> 回复"""
    # token 模式：cursor 必须传空（token 与 cursor 二选一），否则会拉回历史消息导致重复回复
    msg_list, next_cursor = sync_kf_messages(token, open_kfid, "")
    for msg in msg_list:
        # 数据库持久化去重：同一 msgid 只处理一次（防重启后历史消息重复回复）
        msgid = msg.get("msgid", "")
        if msgid and is_processed(msgid):
            logging.info("已处理过跳过 msgid=%s", msgid)
            continue
        # 事件类消息：进入会话（欢迎菜单）/ 菜单按钮点击
        if msg.get("msgtype") == "event":
            ev = msg.get("event") or {}
            logging.info("收到事件消息: %s", json.dumps(ev, ensure_ascii=False)[:300])
            ev_type = ev.get("event_type", "")
            ext = msg.get("external_userid", "")
            if ev_type == "enter_session":
                code = ev.get("welcome_code") or ev.get("code")
                if code:
                    # welcome_code 也要去重（同一事件可能被多次拉到）
                    if is_processed("wc:" + code):
                        logging.info("welcome_code 已用过，跳过")
                    else:
                        try:
                            send_welcome_on_event(code, msg.get("open_kfid") or open_kfid)
                        except Exception as e:
                            logging.exception("欢迎菜单发送异常: %s", e)
            elif ev_type == "menu":
                # 用户点击菜单按钮：按 id 回复对应答案（7 进入提现流程；confirm/cancel 处理退款确认）
                key = str(ev.get("menu_id", "") or ev.get("id", "") or "")
                if ext:
                    ctx2 = session_ctx.get(ext) or {}
                    if key == "7":
                        session_ctx[ext] = {"stage": "await_phone"}
                        send_text(ext, open_kfid, "请输入支付使用时的手机号，帮您提交人工处理")
                    elif key == "confirm":
                        order_nos = ctx2.get("orders", [])
                        session_ctx.pop(ext, None)
                        send_text(ext, open_kfid, do_refund_batch(order_nos))
                    elif key == "cancel":
                        session_ctx.pop(ext, None)
                        send_text(ext, open_kfid, "已取消本次操作。如需帮助请回复菜单序号。")
                    elif key in MENU_ANSWERS:
                        send_text(ext, open_kfid, MENU_ANSWERS[key])
            continue
        if msg.get("origin") != 3:      # 只处理用户发来的消息（3=用户）
            continue
        external_userid = msg.get("external_userid")
        msgtype = msg.get("msgtype")
        if msgtype == "text":
            text = (msg.get("text") or {}).get("content", "")
        elif msgtype == "image":
            text = "[图片]"
        else:
            text = f"[{msgtype}]"
        # 1) 入库你们的工单/客服系统（这里打印演示，接你们系统时替换成写库逻辑）
        logging.info("收到用户消息 user=%s msgid=%s: %s", external_userid, msgid, text)
        # 2) 多轮流程（提现未到账等）优先；未命中走菜单/关键字自动回复
        reply = process_user_text(external_userid, text)
        if isinstance(reply, tuple) and reply[0] == "__CONFIRM_MENU__":
            # 列单完成：发确认/取消按钮菜单
            send_confirm_menu(external_userid, open_kfid, reply[1])
        elif reply is None:
            reply = auto_reply(msg)
            if reply is None:
                # 兜底：发按钮菜单（可点击链接）
                send_menu(external_userid, open_kfid)
            else:
                send_text(external_userid, open_kfid, reply)
        else:
            send_text(external_userid, open_kfid, reply)
        # 3) 回复 6（退预付款）时，附带小程序卡片直达钱包（需企微已绑定小程序）
        if MP_CARD_ENABLED and (msg.get("text") or {}).get("content", "").strip() == "6":
            send_miniprogram(external_userid, open_kfid, "我的钱包", MP_WALLET_PATH)


# ---------------- 路由 ----------------
@app.route("/wecom/kf", methods=["GET", "POST"])
def kf_callback():
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    msg_signature = request.args.get("msg_signature", "")

    if request.method == "GET":
        # 企微后台保存回调 URL 时发来的 URL 验证请求
        echostr = request.args.get("echostr", "")
        if not verify_signature(msg_signature, timestamp, nonce, echostr):
            return "verify fail", 403
        return decrypt_msg(echostr)     # 返回解密后的明文即通过验证

    # POST：消息/事件推送
    body = request.get_data(as_text=True)
    root = ET.fromstring(body)
    encrypt = root.findtext("Encrypt") or ""
    if not verify_signature(msg_signature, timestamp, nonce, encrypt):
        return "signature fail", 403

    inner = ET.fromstring(decrypt_msg(encrypt))
    event = inner.findtext("Event")
    event_type = inner.findtext("EventType") or ""
    open_kfid = inner.findtext("OpenKfId") or ""
    logging.info("收到事件: %s event_type=%s", event, event_type)
    # 调试：打印完整解密 XML（确认企微事件结构）
    logging.info("解密XML: %s", ET.tostring(inner, encoding="unicode")[:800])

    if event == "kf_msg_or_event":
        if event_type == "enter_session":
            # 用户进入会话：发送欢迎菜单（用事件下发的响应 code）
            code = inner.findtext("WelcomeCode") or inner.findtext("Code")
            if code:
                try:
                    send_welcome_on_event(code, open_kfid)
                except Exception as e:
                    logging.exception("欢迎语发送异常: %s", e)
        else:
            # 消息/其他事件：拉取消息并回复
            event_token = inner.findtext("Token")
            try:
                handle_kf_event(event_token, open_kfid)
            except Exception as e:          # 失败也要应答，避免企微重试风暴
                logging.exception("处理事件失败: %s", e)
    return "success"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
