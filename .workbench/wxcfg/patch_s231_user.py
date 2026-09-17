#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S231-20260917] routes/user.py: ① 存包落地页只给"账户余额"一个模板 ② 新增跳转意图接口"""
import io, sys

P = sys.argv[1] if len(sys.argv) > 1 else 'routes/user.py'

OLD = '''@bp.route('/user/subscribe-templates', methods=['GET'])
def get_subscribe_templates():
    """返回订阅消息模板ID列表（动态下发，前端不写死）"""
    _withdraw = _wx_tpl('subscribe_refund', 'mp', 'lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c')   # 退款成功
    _general = _wx_tpl('subscribe_general', 'mp', 'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA')     # 押金退还
    # [2026-09-14] 弹窗不再请求"寄存成功"（老板要求：把用户的一次性授权集中在跟钱相关的两条上）
    # 字段名保留、指向"押金退还"，避免前端按旧字段名取到空值把弹窗搞挂（实验已验证这样弹窗正常显示 2 条）
    _deposit = _general
    return json_response(data={
        'templates': [_withdraw, _general, _deposit],
        'withdraw_notify': _withdraw,
        'general_notify': _general,
        'deposit_notify': _deposit,
        'withdraw': _withdraw,
        'general': _general,
        'deposit': _deposit,
    })
'''

NEW = '''@bp.route('/user/subscribe-templates', methods=['GET'])
def get_subscribe_templates():
    """返回订阅消息模板ID列表（动态下发，前端不写死）"""
    _withdraw = _wx_tpl('subscribe_refund', 'mp', 'lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c')   # 退款成功
    _general = _wx_tpl('subscribe_general', 'mp', 'ax-O5Qa05IWt7bbhRVk9Pb9A_SbXfIMfbhm0Hoh4gYc')     # 账户余额通知（原"押金退还"，2026-09-17 换模板）
    # [S231-20260917] 存包落地页（H5 跳过来的那一页）只请求"账户余额"一个模板；其它位置（提现页 / 小程序存包页）保持两个。
    # 怎么判断：H5 跳转前用 sendBeacon 打过一个"跳转意图"(mp_enter_log.phase='jump_intent')，
    #           这里把 6 秒内最新的一条意图"消费"掉；消费到了 = 这次请求来自存包落地页。
    # 为什么用时间窗：小程序的模板请求不带任何身份信息（线上 2,852 次请求参数完全一样，无法按页面区分）。
    # 万一配错，最坏结果只是某次弹窗少要一个模板，不影响下单/支付/钱。
    _landing = False
    try:
        _tc = get_db()
        _tcu = _tc.cursor()
        _tcu.execute("""
            UPDATE mp_enter_log SET phase = 'jump_intent_used'
            WHERE id = (SELECT id FROM mp_enter_log
                        WHERE phase = 'jump_intent' AND created_at >= NOW() - INTERVAL '6 seconds'
                        ORDER BY id DESC LIMIT 1)
            RETURNING id
        """)
        _landing = _tcu.fetchone() is not None
        _tc.commit()
        _tc.close()
    except Exception as _te:
        logger.warning('[subscribe_templates] 跳转意图判断失败(按非落地页处理): %s', _te)
    # [2026-09-14] 弹窗不再请求"寄存成功"；字段名保留、指向"账户余额"，避免前端按旧字段名取到空值把弹窗搞挂
    _deposit = _general
    _tpls = [_general] if _landing else [_withdraw, _general]
    return json_response(data={
        'templates': _tpls,
        'withdraw_notify': _withdraw,
        'general_notify': _general,
        'deposit_notify': _deposit,
        'withdraw': _withdraw,
        'general': _general,
        'deposit': _deposit,
        'landing': _landing,
    })


@bp.route('/user/mp-jump-intent', methods=['POST'])
def user_mp_jump_intent():
    """[S231-20260917] H5 跳小程序前用 sendBeacon 打个招呼：接下来几秒的模板请求 = 存包落地页。

    永远返回 ok —— 这条路失败绝不能影响用户跳转。
    """
    try:
        data = request.get_json(silent=True) or {}
        _oid = str(data.get('order_id') or '')[:40]
        _ph = str(data.get('phone') or '')[:20]
        if _oid or _ph:
            _jc = get_db()
            _jcu = _jc.cursor()
            _jcu.execute("INSERT INTO mp_enter_log (order_id, phone, phase) VALUES (%s, %s, 'jump_intent')", (_oid, _ph))
            _jc.commit()
            _jc.close()
    except Exception as _je:
        logger.warning('[mp_jump_intent] 记录失败(不影响跳转): %s', _je)
    return json_response(message='ok')
'''


def main():
    s = io.open(P, encoding='utf-8').read()
    n = s.count(OLD)
    if n != 1:
        print('[FAIL] 原文出现 %d 次（要求恰好 1 次），未写文件' % n)
        sys.exit(2)
    s2 = s.replace(OLD, NEW, 1)
    for must in ['/user/mp-jump-intent', "phase = 'jump_intent_used'", 'ax-O5Qa05IWt7bbhRVk9Pb9A_SbXfIMfbhm0Hoh4gYc', '_tpls = [_general] if _landing else [_withdraw, _general]']:
        if must not in s2:
            print('[FAIL] 缺少标记: %s' % must)
            sys.exit(3)
    io.open(P, 'w', encoding='utf-8', newline='').write(s2)
    print('[DONE] user.py: %d -> %d 字符 (%+d)' % (len(s), len(s2), len(s2) - len(s)))


if __name__ == '__main__':
    main()
