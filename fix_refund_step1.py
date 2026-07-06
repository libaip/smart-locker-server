#!/usr/bin/env python3
"""Fix refund logic: make refunds actually call WeChat API"""
import re

# ==========================================
# Step 1: wxpay.py - Add transfer method
# ==========================================
print('=== Step 1: wxpay.py ===')
with open('wxpay.py', 'r') as f:
    content = f.read()

# Add transfer to WxPay (before refund_query)
if 'def transfer(self, partner_trade_no' not in content:
    transfer_code = '''    def transfer(self, partner_trade_no, openid, amount, desc='withdraw', check_name='NO_CHECK', spbill_create_ip='127.0.0.1'):
        """Transfer money to user's WeChat wallet"""
        if not self.cert_path or not self.key_path:
            return {'return_code': 'FAIL', 'return_msg': 'Certificate not configured'}
        url = 'https://api.mch.weixin.qq.com/mmpaymkttransfers/promotion/transfers'
        params = {
            'mch_appid': self.app_id,
            'mchid': self.mch_id,
            'nonce_str': self.generate_nonce_str(),
            'partner_trade_no': partner_trade_no,
            'openid': openid,
            'check_name': check_name,
            'amount': amount,
            'desc': desc,
            'spbill_create_ip': spbill_create_ip,
        }
        params['sign'] = self.make_sign(params)
        xml_data = self.dict_to_xml(params)
        result = self.http_request(url, xml_data, use_cert=True)
        return result

'''
    content = content.replace('    def refund_query(self, out_trade_no', transfer_code + '    def refund_query(self, out_trade_no', 1)
    print('Added transfer() to WxPay')
else:
    print('WxPay.transfer() already exists')

# Add transfer to MockWxPay (before its build_pay_notify_response)
if "def transfer(partner_trade_no='', openid=''" not in content:
    idx = content.find('class ThirdPartyPay')
    if idx > 0:
        idx2 = content.rfind('    def build_pay_notify_response', 0, idx)
        if idx2 > 0:
            mock_transfer = '''    @staticmethod
    def transfer(partner_trade_no='', openid='', amount=0, **kwargs):
        """Mock transfer to WeChat wallet"""
        return {
            'return_code': 'SUCCESS',
            'result_code': 'SUCCESS',
            'payment_no': 'PMOCK' + datetime.now().strftime('%Y%m%d%H%M%S'),
            'partner_trade_no': partner_trade_no,
        }

'''
            content = content[:idx2] + mock_transfer + content[idx2:]
            print('Added transfer() to MockWxPay')

# Add transfer to ThirdPartyPay
tpp_start = content.find('class ThirdPartyPay')
if tpp_start > 0:
    tpp_end = content.find('\nclass ', tpp_start + 10) if '\nclass ' in content[tpp_start + 10:] else len(content)
    tpp_section = content[tpp_start:tpp_end]
    if 'def transfer(' not in tpp_section:
        tpp_transfer = '''    def transfer(self, partner_trade_no='', openid='', amount=0, **kwargs):
        """Third-party transfer (not supported via API)"""
        return {'return_code': 'FAIL', 'return_msg': 'Transfer via platform backend'}

'''
        content = content.replace('    def verify_notify(self, params):', tpp_transfer + '    def verify_notify(self, params):', 1)
        print('Added transfer() to ThirdPartyPay')

with open('wxpay.py', 'w') as f:
    f.write(content)
print('wxpay.py saved')

# ==========================================
# Step 2: helpers.py - Add do_real_refund and do_balance_transfer
# ==========================================
print('\n=== Step 2: helpers.py ===')
with open('helpers.py', 'r') as f:
    content = f.read()

if 'def do_real_refund(' not in content:
    new_funcs = '''

def do_real_refund(order_id=None, order_no=None, amount=0, payment_channel_id=None):
    """Actually call WeChat refund API. Returns (success, refund_id, message)"""
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        if order_id:
            cursor.execute('SELECT order_no, transaction_id, payment_channel_id FROM orders WHERE id=%s', (order_id,))
            row = cursor.fetchone()
            if row:
                order_no = order_no or row['order_no']
                payment_channel_id = payment_channel_id or row['payment_channel_id']
        conn.close()
        if not order_no:
            return False, '', 'Order number is empty'
        payer = None
        if payment_channel_id:
            try:
                conn2 = get_db()
                cursor2 = conn2.cursor()
                cursor2.execute('SELECT * FROM payment_channels WHERE id=%s AND status=1', (payment_channel_id,))
                channel = cursor2.fetchone()
                conn2.close()
                if channel:
                    channel_dict = {}
                    for key in channel.keys():
                        channel_dict[key] = channel[key]
                    payer, _ = get_channel_wxpay(channel_dict)
            except:
                pass
        if not payer:
            payer = get_wxpay()
        total_fee = int(float(amount) * 100)
        result = payer.refund(out_trade_no=order_no, total_fee=total_fee, refund_fee=total_fee)
        if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
            refund_id = result.get('refund_id') or result.get('out_refund_no', '')
            logger.info('[do_real_refund] Success: order=%s, refund_id=%s' % (order_no, refund_id))
            return True, refund_id, 'Refund successful'
        else:
            err_msg = result.get('return_msg') or result.get('err_code_des') or 'Refund failed'
            logger.error('[do_real_refund] Failed: order=%s, msg=%s' % (order_no, err_msg))
            return False, '', err_msg
    except Exception as e:
        logger.error('[do_real_refund] Exception: %s' % e)
        return False, '', str(e)


def do_balance_transfer(phone, amount, openid=None):
    """Transfer balance to user WeChat wallet. Returns (success, payment_no, message)"""
    try:
        from database import get_db
        if not openid:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT openid FROM orders WHERE user_phone=%s AND openid IS NOT NULL AND openid!="" ORDER BY id DESC LIMIT 1', (phone,))
            row = cursor.fetchone()
            conn.close()
            if row:
                openid = row['openid']
            else:
                logger.error('[do_balance_transfer] No openid for %s' % phone)
                return False, '', 'User openid is empty'
        payer = get_wxpay()
        partner_trade_no = 'WD' + datetime.now().strftime('%Y%m%d%H%M%S') + ''.join(random.choices(string.digits, k=6))
        result = payer.transfer(
            partner_trade_no=partner_trade_no,
            openid=openid,
            amount=int(float(amount) * 100),
            desc='Locker balance withdrawal'
        )
        if result.get('return_code') == 'SUCCESS' and result.get('result_code') == 'SUCCESS':
            payment_no = result.get('payment_no', '')
            logger.info('[do_balance_transfer] Success: phone=%s, payment_no=%s' % (phone, payment_no))
            return True, payment_no, 'Transfer successful'
        else:
            err_msg = result.get('return_msg') or result.get('err_code_des') or 'Transfer failed'
            logger.error('[do_balance_transfer] Failed: phone=%s, msg=%s' % (phone, err_msg))
            return False, '', err_msg
    except Exception as e:
        logger.error('[do_balance_transfer] Exception: %s' % e)
        return False, '', str(e)

'''
    content += new_funcs
    print('Added do_real_refund() and do_balance_transfer()')
else:
    print('do_real_refund() already exists')

with open('helpers.py', 'w') as f:
    f.write(content)
print('helpers.py saved')

print('\nSteps 1-2 complete')
