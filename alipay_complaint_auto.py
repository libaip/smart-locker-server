#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S825-20261001] 支付宝官方「交易投诉」自动登记 + 自动原路退款。

背景
----
老板 2026-10-01 定稿：**接支付宝官方投诉，并且直接自动原路退款**。
事实核查（本次实测）：
  · `alipay.merchant.tradecomplain.batchquery` 返回 code=10000 Success —— 这个支付宝应用
    **早就有「交易投诉」接口权限**，只是代码里一行都没调过；
  · 近 7 天 / 90 天 / 1 年 total_num 全是 0 —— 目前一条官方投诉都没有；
  · `alipay.merchant.tradecomplain.query/detail/reply/finish` 这几个方法名是错的
    （支付宝回 HTML 错误页），要动作得走 batchquery 这条线。

本脚本职责（三步，全部幂等）
--------------------------
  1) 拉：batchquery 拉近 N 小时的官方投诉（滚动窗口，容忍投诉迟到）；
  2) 登记：按 alipay_complain_id 去重写进 complaints（type='alipay' / platform='alipay'）；
  3) 退款：复用 routes.admin_v2._auto_refund_complaint_order —— 与微信官方投诉**同一套资金逻辑**
     （内部按订单 payment_channel_id 走 alipay.trade.refund），退成功把投诉置 status=2。

为什么不做成应用内调度器
----------------------
现有 `_complaint_scheduler` 的筛选是 `type IN ('wechat')`，支付宝官方投诉走独立 cron，
两条链路互不干扰 —— **微信那条已验证过的链路一行都不动**。

开关（system_settings，读不到一律按默认，绝不报错）
-----------------------------------------------
  alipay_complaint_auto_refund   默认 '1'  总开关（'0' = 只登记不退款）
  alipay_complaint_lookback_hours 默认 '6'  每次回看多少小时
  alipay_complaint_max_retry      默认 '3'  同一投诉最多重试几次退款

安全底线
-------
**只退本地库里能唯一匹配到的订单**（order_no 或 transaction_id）。
接口返回的任何金额字段一律【不采信】—— 响应字段名猜错也绝不会退错钱、退多钱。
匹配不到订单 -> 只登记 + 转人工，一分钱不动。

用法：python3 alipay_complaint_auto.py [--dry-run]
"""
import sys
import os
import json
import logging
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, "/home/ubuntu/smart-locker")

LOG_FILE = "/home/ubuntu/smart-locker/logs/alipay_complaint_auto.log"
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alipay_complaint")

TAG = "[S825]"
DRY_RUN = "--dry-run" in sys.argv


# ----------------------------------------------------------------- 工具
def _pick(rec, *names):
    """防御式取字段：支付宝响应字段名没拿到官方文档（近一年 0 条没有真实样本），
    所以同一含义多写几种拼法，任何一个命中就用；全不命中返回 ''。"""
    if not isinstance(rec, dict):
        return ""
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            v = rec[n]
            if isinstance(v, (dict, list)):
                continue
            return str(v).strip()
    # 再往嵌套里找一层（形如 {"complain_order_info": [{...}]}）
    for v in rec.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            got = _pick(v[0], *names)
            if got:
                return got
        if isinstance(v, dict):
            got = _pick(v, *names)
            if got:
                return got
    return ""


def _setting_int(key, default):
    try:
        from helpers import get_setting
        v = get_setting(key, None)
        if v is None or str(v).strip() == "":
            return default
        return int(str(v).strip())
    except Exception as e:
        log.warning("%s 读设置 %s 失败，按默认 %s: %s", TAG, key, default, e)
        return default


def _auto_refund_on():
    try:
        from helpers import get_setting
        v = get_setting("alipay_complaint_auto_refund", None)
        if v is None or str(v).strip() == "":
            return True                     # 默认开（老板 2026-10-01 定的口径）
        return str(v).strip() not in ("0", "false", "False", "no", "off")
    except Exception as e:
        log.warning("%s 读开关失败，按默认【开】: %s", TAG, e)
        return True


def _alarm(kind, content, level=1):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO alarms (type, content, level) VALUES (%s, %s, %s)", (kind, content, level))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("%s 写 alarms 失败(%s): %s", TAG, kind, e)


def _push(title, content):
    try:
        from helpers import send_pushplus
        send_pushplus(title, content)
    except Exception as e:
        log.warning("%s PushPlus 失败: %s", TAG, e)


# ----------------------------------------------------------------- 拉取
def fetch_complaints(hours):
    """拉近 hours 小时的官方投诉原始记录。返回 (list_of_dict, meta)。"""
    from helpers import get_alipay_mp_client
    cli = get_alipay_mp_client()
    if cli is None:
        raise RuntimeError("拿不到支付宝小程序客户端（app_id/密钥缺失）")
    end = datetime.now()
    begin = end - timedelta(hours=max(1, int(hours)))
    fmt = "%Y-%m-%d %H:%M:%S"
    out, page = [], 1
    while page <= 20:
        biz = {"begin_time": begin.strftime(fmt), "end_time": end.strftime(fmt),
               "page_num": page, "page_size": 20}
        r = cli._post("alipay.merchant.tradecomplain.batchquery", biz)
        code = str(r.get("code") or "")
        if code != "10000":
            raise RuntimeError("batchquery 失败 code=%s sub_code=%s sub_msg=%s"
                               % (code, r.get("sub_code"), r.get("sub_msg")))
        infos = r.get("trade_complain_infos") or []
        if not isinstance(infos, list):
            infos = []
        out.extend([x for x in infos if isinstance(x, dict)])
        total_page = 0
        try:
            total_page = int(r.get("total_page_num") or 0)
        except Exception:
            total_page = 0
        if page >= total_page or not infos:
            break
        page += 1
    return out, {"begin": begin.strftime(fmt), "end": end.strftime(fmt)}


# ----------------------------------------------------------------- 登记
def register_complaint(rec):
    """按 alipay_complain_id 幂等登记。返回 (local_id, is_new, order_row)。"""
    from database import get_db
    complain_id = _pick(rec, "complain_id", "complainId", "complaint_id", "id")
    trade_no = _pick(rec, "trade_no", "tradeNo")
    out_trade_no = _pick(rec, "out_trade_no", "outTradeNo")
    reason = _pick(rec, "complain_reason", "reason", "complain_type", "complainType")
    content = _pick(rec, "complain_content", "content", "detail", "complain_desc")
    cstatus = _pick(rec, "complain_status", "complainStatus", "status")

    if not complain_id:
        # 没有 complain_id 就没法幂等，一律不登记、不退款，只告警（绝不猜）
        log.error("%s 投诉记录里找不到 complain_id，跳过并告警: %s", TAG, json.dumps(rec, ensure_ascii=False)[:600])
        _alarm("alipay_complaint_noid",
               "%s 官方投诉记录缺 complain_id，已跳过（原文见日志）: %s"
               % (TAG, json.dumps(rec, ensure_ascii=False)[:400]))
        return None, False, None

    conn = get_db()
    cur = conn.cursor()
    order = None
    if out_trade_no:
        cur.execute("""SELECT id, order_no, transaction_id, deposit_amount, refund_status, status,
                              payment_channel_id, user_phone, alipay_pay_uid, alipay_mp_uid, slot_id
                       FROM orders WHERE order_no=%s LIMIT 1""", (out_trade_no,))
        order = cur.fetchone()
    if not order and trade_no:
        cur.execute("""SELECT id, order_no, transaction_id, deposit_amount, refund_status, status,
                              payment_channel_id, user_phone, alipay_pay_uid, alipay_mp_uid, slot_id
                       FROM orders WHERE transaction_id=%s LIMIT 1""", (trade_no,))
        order = cur.fetchone()

    o = dict(order) if order else {}
    if not out_trade_no:
        out_trade_no = o.get("order_no") or ""
    phone = o.get("user_phone") or ""
    auid = (o.get("alipay_pay_uid") or o.get("alipay_mp_uid") or "")

    body = []
    if reason:
        body.append("投诉原因: %s" % reason)
    if content:
        body.append("投诉内容: %s" % content)
    if cstatus:
        body.append("支付宝侧状态: %s" % cstatus)
    if complain_id:
        body.append("complain_id: %s" % complain_id)
    if trade_no:
        body.append("trade_no: %s" % trade_no)
    content_text = "\n".join(body) or (json.dumps(rec, ensure_ascii=False)[:500])

    cur.execute("""INSERT INTO complaints
                     (user_phone, type, content, order_no, complaint_type, openid,
                      platform, alipay_uid, alipay_complain_id, transaction_id, source,
                      status, reply, refund_retry)
                   VALUES (%s, 'alipay', %s, %s, 'alipay', '', 'alipay', %s, %s, %s,
                           '支付宝官方投诉', '0', '', 0)
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                (phone, content_text, out_trade_no, auid, complain_id, trade_no))
    row = cur.fetchone()
    if row is None:
        conn.commit()
        conn.close()
        log.info("%s 投诉 %s 已登记过，跳过", TAG, complain_id)
        return None, False, (o if o else None)
    local_id = row["id"] if "id" in row else row[0]
    conn.commit()
    conn.close()
    log.info("%s 新登记官方投诉 local_id=%s complain_id=%s order_no=%s trade_no=%s 匹配到订单=%s",
             TAG, local_id, complain_id, out_trade_no or "(空)", trade_no or "(空)", bool(o))
    return local_id, True, (o if o else None)


# ----------------------------------------------------------------- 退款
def refund_one(local_id, out_trade_no, trade_no, order):
    """调用与微信官方投诉完全相同的资金逻辑。"""
    from routes.admin_v2 import _auto_refund_complaint_order
    if not order:
        _mark(local_id, "0", "支付宝官方投诉：未找到对应订单，转人工核实", retry_inc=True)
        log.warning("%s local_id=%s 匹配不到订单，只登记不退款 order_no=%s trade_no=%s",
                    TAG, local_id, out_trade_no or "(空)", trade_no or "(空)")
        return False, "订单不存在"
    ok, msg = _auto_refund_complaint_order(
        out_trade_no or order.get("order_no") or "",
        transaction_id=trade_no or order.get("transaction_id") or "",
        complaint_id=local_id,
        payer_phone="",          # 有订单号/交易号就不走手机号兜底，少一条猜的路
        mch_id="")
    log.info("%s local_id=%s order_no=%s 退款结果 ok=%s msg=%s",
             TAG, local_id, out_trade_no or order.get("order_no"), ok, msg)
    if ok:
        if str(msg) == "订单已退款，无需重复退款":
            _mark(local_id, "2", "订单已退款，无需重复退款")
        else:
            _mark(local_id, "2", "已自动原路退款")
            _alarm("alipay_complaint_refunded",
                   "%s 支付宝官方投诉已自动原路退款: complaint_local_id=%s order_no=%s amount=%s refund=%s"
                   % (TAG, local_id, out_trade_no or order.get("order_no"),
                      order.get("deposit_amount"), msg), level=0)
            _push("【寄存柜】支付宝官方投诉已自动退款",
                  "投诉已自动原路退款。\n订单号: %s\n金额: %s 元\n退款结果: %s\n"
                  % (out_trade_no or order.get("order_no"), order.get("deposit_amount"), msg))
    else:
        _mark(local_id, "0", "自动退款失败，稍后自动重试：%s" % str(msg)[:120], retry_inc=True)
        _alarm("alipay_complaint_refund_failed",
               "%s 支付宝官方投诉自动退款失败: complaint_local_id=%s order_no=%s msg=%s"
               % (TAG, local_id, out_trade_no or order.get("order_no"), msg))
    return ok, msg


def _mark(local_id, status, reply, retry_inc=False):
    try:
        from database import get_db
        conn = get_db()
        cur = conn.cursor()
        if retry_inc:
            cur.execute("""UPDATE complaints SET status=%s, reply=%s, reply_time=CURRENT_TIMESTAMP,
                                  refund_retry=COALESCE(refund_retry,0)+1 WHERE id=%s""",
                        (status, reply, local_id))
        else:
            cur.execute("""UPDATE complaints SET status=%s, reply=%s, reply_time=CURRENT_TIMESTAMP
                           WHERE id=%s""", (status, reply, local_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("%s 更新投诉 %s 失败: %s", TAG, local_id, e)


def retry_local(max_retry):
    """重试库里还挂着（status 0/1）的支付宝官方投诉。"""
    from database import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, order_no, transaction_id FROM complaints
                   WHERE type='alipay' AND platform='alipay' AND alipay_complain_id IS NOT NULL
                     AND status IN ('0','1') AND COALESCE(refund_retry,0) < %s
                     AND created_at > NOW() - INTERVAL '30 days'
                   ORDER BY id LIMIT 20""", (max_retry,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        o = None
        c2 = get_db()
        cur2 = c2.cursor()
        if r.get("order_no"):
            cur2.execute("""SELECT id, order_no, transaction_id, deposit_amount, refund_status, status,
                                   payment_channel_id, user_phone, alipay_pay_uid, alipay_mp_uid, slot_id
                            FROM orders WHERE order_no=%s LIMIT 1""", (r["order_no"],))
            o = cur2.fetchone()
        c2.close()
        o = dict(o) if o else None
        log.info("%s 重试 local_id=%s order_no=%s", TAG, r["id"], r.get("order_no"))
        refund_one(r["id"], r.get("order_no") or "", r.get("transaction_id") or "", o)


# ----------------------------------------------------------------- 主流程
def main():
    log.info("%s ===== 开始 dry_run=%s =====", TAG, DRY_RUN)
    do_refund = _auto_refund_on()
    hours = _setting_int("alipay_complaint_lookback_hours", 6)
    max_retry = _setting_int("alipay_complaint_max_retry", 3)
    log.info("%s 开关: 自动退款=%s 回看=%s小时 最大重试=%s", TAG, do_refund, hours, max_retry)

    try:
        recs, meta = fetch_complaints(hours)
    except Exception as e:
        log.error("%s 拉取官方投诉失败: %s\n%s", TAG, e, traceback.format_exc()[:1200])
        _alarm("alipay_complaint_fetch_failed", "%s 拉取支付宝官方投诉失败: %s" % (TAG, e))
        return 2

    log.info("%s 拉取窗口 %s ~ %s，共 %s 条", TAG, meta["begin"], meta["end"], len(recs))
    if DRY_RUN:
        for rec in recs:
            log.info("%s [dry-run] 原文: %s", TAG, json.dumps(rec, ensure_ascii=False)[:1500])
        print("[dry-run] 共 %d 条，原文见日志" % len(recs))
        return 0

    new_cnt = refund_ok = refund_fail = 0
    for rec in recs:
        # 每条都留全量原文，便于第一条真投诉到来时校准字段名
        log.info("%s 原文: %s", TAG, json.dumps(rec, ensure_ascii=False)[:1500])
        try:
            local_id, is_new, order = register_complaint(rec)
        except Exception as e:
            log.error("%s 登记异常: %s\n%s", TAG, e, traceback.format_exc()[:1200])
            continue
        if not is_new:
            continue
        new_cnt += 1
        if not do_refund:
            log.info("%s 开关关闭，只登记不退款 local_id=%s", TAG, local_id)
            continue
        try:
            ok, _ = refund_one(local_id, _pick(rec, "out_trade_no", "outTradeNo"),
                               _pick(rec, "trade_no", "tradeNo"), order)
            refund_ok += 1 if ok else 0
            refund_fail += 0 if ok else 1
        except Exception as e:
            refund_fail += 1
            log.error("%s 退款异常 local_id=%s: %s\n%s", TAG, local_id, e, traceback.format_exc()[:1200])
            _mark(local_id, "0", "自动退款异常，稍后自动重试", retry_inc=True)

    if do_refund:
        try:
            retry_local(max_retry)
        except Exception as e:
            log.error("%s 重试阶段异常: %s", TAG, e)

    log.info("%s ===== 结束: 新登记=%s 退款成功=%s 退款失败=%s =====", TAG, new_cnt, refund_ok, refund_fail)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.error("%s 顶层异常: %s\n%s", TAG, e, traceback.format_exc()[:1500])
        sys.exit(1)
