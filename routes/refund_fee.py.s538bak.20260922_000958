from flask import Blueprint, request
from database import get_db
from helpers import json_response, require_auth
import logging

logger = logging.getLogger(__name__)
bp = Blueprint("refund_fee", __name__)

@bp.route("/admin/order/refund-fee", methods=["POST"])
@require_auth
def admin_order_refund_fee():
    try:
        data = request.get_json()
        order_id = data.get("order_id")
        if not order_id:
            return json_response(message="order_id 不能为空", code=400)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE id=%s AND status IN (2,3,4) AND per_use_price>0 AND COALESCE(refund_status,'')!='fee_refunded'", (order_id,))
        order = c.fetchone()
        if not order:
            conn.close()
            return json_response(message="订单不存在、状态不允许或已退过使用费", code=400)
        od = dict(order)
        fee = float(od.get("per_use_price") or 0)
        if fee <= 0:
            conn.close()
            return json_response(message="该订单无使用费", code=400)
        order_no = od.get("order_no", "")
        payment_channel_id = od.get("payment_channel_id")
        conn.close()
        from helpers import do_real_refund
        ok, rid, msg = do_real_refund(order_id=order_id, order_no=order_no, amount=fee, payment_channel_id=payment_channel_id)
        if ok:
            conn2 = get_db()
            c2 = conn2.cursor()
            c2.execute("UPDATE orders SET refund_status='fee_refunded' WHERE id=%s", (order_id,))
            conn2.commit()
            conn2.close()
            logger.info("[refund_fee] 使用费已退 order_id=%s amount=%s" % (order_id, fee))
            return json_response(message="使用费 %s 元已原路退款" % fee, data={"amount": fee, "refund_id": rid})
        else:
            return json_response(message="退款失败: %s" % msg, code=500)
    except Exception as e:
        logger.error("[refund_fee] %s" % e)
        return json_response(message=str(e), code=500)
