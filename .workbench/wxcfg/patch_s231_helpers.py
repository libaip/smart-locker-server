#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S231-20260917] helpers.py: 发送失败时"新账户余额模板 -> 旧押金退还模板"回退（过渡期不丢通知）"""
import io, sys

P = sys.argv[1] if len(sys.argv) > 1 else 'helpers.py'

ANCHOR = "def send_wx_subscribe_message(openid, template_id, data, page='', phone=None, unionid=None):"

CONSTS = '''# ============================================
# [S231-20260917] "押金退还通知" -> "账户余额通知" 换模板过渡用的两个 ID
# 换模板后老用户手里只有旧模板的授权（微信按模板 ID 记账），新模板会被拒(43101 无额度)，
# 所以发送失败时用旧模板再发一次，过渡期一条通知都不丢。
# ============================================
_TPL_ACCOUNT_NEW = 'ax-O5Qa05IWt7bbhRVk9Pb9A_SbXfIMfbhm0Hoh4gYc'   # 账户余额通知（新）
_TPL_DEPOSIT_OLD = 'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA'   # 押金退还通知（旧，仅作过渡回退）


'''

OLD_FAIL = """        else:
            logger.error(f'[subscribe_msg] 发送失败: openid={openid[:8]}..., phone={phone}, template={template_id}, result={result}')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='errcode=%s' % result.get('errcode'))
            return False
"""

NEW_FAIL = """        else:
            logger.error(f'[subscribe_msg] 发送失败: openid={openid[:8]}..., phone={phone}, template={template_id}, result={result}')
            # [S231] 过渡期回退：换成新"账户余额"模板后，老用户只有旧"押金退还"模板的授权 ->
            #        新模板必然被拒(43101)，这里用旧模板再发一次；过渡期结束(大家都重新授权过)可去掉这段。
            if template_id == _TPL_ACCOUNT_NEW:
                try:
                    _p2 = {'touser': openid, 'template_id': _TPL_DEPOSIT_OLD, 'data': data}
                    if page:
                        _p2['page'] = page
                    _r2 = requests.post(send_url, json=_p2, timeout=5).json()
                    if _r2.get('errcode') == 0:
                        logger.info(f'[subscribe_msg] 新模板失败->旧模板发送成功: openid={openid[:8]}...')
                        return True
                    logger.error(f'[subscribe_msg] 旧模板也失败: openid={openid[:8]}..., result={_r2}')
                except Exception as _e2:
                    logger.error(f'[subscribe_msg] 旧模板回退异常: {_e2}')
            send_oa_subscribe_notify(template_id, data, phone=phone or '', unionid=unionid, reason='errcode=%s' % result.get('errcode'))
            return False
"""


def main():
    s = io.open(P, encoding='utf-8').read()
    before = len(s)
    if s.count(ANCHOR) != 1:
        print('[FAIL] 找不到唯一的 send_wx_subscribe_message 定义')
        sys.exit(2)
    if s.count(OLD_FAIL) != 1:
        print('[FAIL] 失败分支原文出现 %d 次（要求 1 次）' % s.count(OLD_FAIL))
        sys.exit(3)
    s = s.replace(ANCHOR, CONSTS + ANCHOR, 1)
    s = s.replace(OLD_FAIL, NEW_FAIL, 1)
    for must in ['_TPL_ACCOUNT_NEW = ', '_TPL_DEPOSIT_OLD = ', '新模板失败->旧模板发送成功']:
        if must not in s:
            print('[FAIL] 缺少标记: %s' % must)
            sys.exit(4)
    io.open(P, 'w', encoding='utf-8', newline='').write(s)
    print('[DONE] helpers.py: %d -> %d 字符 (%+d)' % (before, len(s), len(s) - before))
    print('       （已插入两个常量 + 失败回退分支）')


if __name__ == '__main__':
    main()
