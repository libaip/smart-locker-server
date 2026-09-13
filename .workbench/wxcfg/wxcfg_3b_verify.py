# -*- coding: utf-8 -*-
"""
[T-1789258766 / 3B] 175 上线后验证：证明"取值来源换了，取到的值没变"

四件事：
  1) 取值一致性：配置中心读出来的值 vs config.py 现值（必须逐项相同）
  2) 兜底：把数据库连接工厂换成"一用就炸"，取值必须回落原值且不抛异常
     （证明配置中心挂了业务也不受影响）
  3) 模板ID：三个小程序订阅模板取到的必须还是老 ID
  4) 真实链路证明：把 get_config 打桩成假域名后 import routes.device，
     它的 DEFAULT_CONFIG 里的 server_url/websocket_url 必须跟着变成假域名
     —— 这说明 device.py 确实是从配置中心取值的，不是还在用写死值
用法: python3 wxcfg_3b_verify.py
"""
import sys

sys.path.insert(0, '/home/ubuntu/smart-locker')

import wx_config as C
from database import get_db
import config

PASS = []
FAIL = []


def disp(v, secret=False):
    v = '' if v is None else str(v)
    if secret:
        return (v[:6] + '…****') if len(v) > 6 else ('(空)' if not v else '****')
    return v if v != '' else '(空)'


def chk(name, got, want, secret=False):
    if got == want:
        PASS.append(name)
        print('   ✅ %-18s %s' % (name, disp(got, secret)))
    else:
        FAIL.append(name)
        print('   ❌ %-18s 实得=%s  期望=%s' % (name, disp(got, secret), disp(want, secret)))


print('=' * 70)
print('[3B] 175 上线后验证')
print('=' * 70)

C.bind(get_db)

print('\n【1】取值一致性：配置中心 vs config.py 现值')
chk('小程序 appid', C.mp_appid(), getattr(config, 'WX_MP_APP_ID', None))
chk('小程序 secret', C.mp_secret(), getattr(config, 'WX_MP_APP_SECRET', None), secret=True)
chk('公众号 appid', C.oa_appid(), getattr(config, 'WX_APP_ID', None))
chk('公众号 secret', C.oa_secret(), getattr(config, 'WX_APP_SECRET', None), secret=True)
chk('支付回调', C.pay_notify_url(), getattr(config, 'WX_PAY_NOTIFY_URL', None))
chk('退款回调', C.refund_notify_url(), getattr(config, 'WX_REFUND_NOTIFY_URL', None))
chk('H5 主域名', C.h5_base(), 'https://locker.cqdyxl.com')
chk('H5 下单页', C.h5_store(), 'https://locker.cqdyxl.com/store')
chk('网页授权回调', C.oauth_callback(), 'https://locker.cqdyxl.com/api/wx/oauth')
chk('WS 地址', C.ws_base(), 'ws://locker.cqdyxl.com')

print('\n【2】兜底：配置中心读不到时必须回落原值、且不抛异常')
D = C.DEFAULTS['config']


def boom():
    raise RuntimeError('模拟：数据库连不上')


old_factory = get_db
try:
    C.bind(boom)
    C.clear_cache()
    chk('兜底 h5_base', C.h5_base(), D['h5_base'])
    chk('兜底 h5_store', C.h5_store(), D['h5_base'] + '/store')
    chk('兜底 oauth_callback', C.oauth_callback(), D['h5_base'] + D['oauth_path'])
    chk('兜底 ws_base', C.ws_base(), 'ws://locker.cqdyxl.com')
    chk('兜底 pay_notify_url', C.pay_notify_url(), D['pay_notify_url'])
    chk('兜底 小程序 appid', C.mp_appid(), C.DEFAULTS['mp']['appid'])
    chk('兜底 公众号 appid', C.oa_appid(), C.DEFAULTS['oa']['appid'])
    chk('兜底 模板ID', C.template_id('subscribe_general', 'mp', 'FALLBACK_ID'), 'FALLBACK_ID')
    print('   （以上调用全部没抛异常）')
except Exception as e:
    FAIL.append('兜底抛异常')
    print('   ❌ 兜底时抛异常了: %r' % e)
finally:
    C.bind(old_factory)
    C.clear_cache()

print('\n【3】模板ID（第1步会读这些，必须还是老 ID）')
chk('subscribe_deposit', C.template_id('subscribe_deposit', 'mp', 'X'), 'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk')
chk('subscribe_refund', C.template_id('subscribe_refund', 'mp', 'X'), 'lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c')
chk('subscribe_general', C.template_id('subscribe_general', 'mp', 'X'), 'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA')
chk('未知biz回落默认值', C.template_id('not_exist_biz', 'mp', 'MY_DEFAULT'), 'MY_DEFAULT')

print('\n【4】真实链路：把配置中心打桩成假域名，看 device.py 里的值是否跟着变')
print('   （必须在 routes.device 还没被 import 的独立进程里做，所以用子进程）')
import subprocess

code = '''
import sys
sys.path.insert(0, "/home/ubuntu/smart-locker")
import wx_config as C
C.get_config = lambda key=None, default=None: ("https://STUB-TEST.example.com" if key == "h5_base" else default)
import routes.device as D
print("    打桩后 device.py 的 server_url    =", D.DEFAULT_CONFIG["server_url"])
print("    打桩后 device.py 的 websocket_url =", D.DEFAULT_CONFIG["websocket_url"])
assert D.DEFAULT_CONFIG["server_url"] == "https://STUB-TEST.example.com", "device.py 没从配置中心取 server_url"
assert D.DEFAULT_CONFIG["websocket_url"] == "ws://STUB-TEST.example.com/ws/", "device.py 没从配置中心取 websocket_url"
print("   ✅ device.py 确实改成了从配置中心取值（打桩成假域名后立刻跟着变）")
'''
r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
print(r.stdout.rstrip())
if r.returncode != 0:
    FAIL.append('device.py 取值链路')
    print('   ❌ 子进程失败: %s' % r.stderr.strip()[-500:])
else:
    PASS.append('device.py 取值链路')

print('\n' + '=' * 70)
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
if FAIL:
    print('失败项: %s' % ', '.join(FAIL))
print('=' * 70)
sys.exit(1 if FAIL else 0)
