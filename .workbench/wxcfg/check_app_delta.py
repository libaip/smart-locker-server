# -*- coding: utf-8 -*-
"""
检查 175 的 app.py 相对 106 的 app.py 只差"第2步要替换掉的写死原值"
用法: python3 check_app_delta.py <175的app.py> <106的app.py>
"""
import sys
import difflib

old = open(sys.argv[1], encoding='utf-8').read().split('\n')
new = open(sys.argv[2], encoding='utf-8').read().split('\n')

WHITELIST = [
    'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA',   # 押金退还模板ID（第1批要换的）
    '_c.WX_APP_ID', '_c.WX_APP_SECRET',                # 公众号凭据（第2批要换的）
    '_cfg.WX_MP_APP_ID',                               # H5 SSR 里的小程序 appid
    '{WX_APP_ID}', '{WX_APP_SECRET}',                  # JSAPI token
    "'appId': WX_APP_ID",                              # JSAPI 签名返回
    'from config import WX_MP_APP_ID as _appid',       # 别名导入（第2批改成运行时取值）
]

sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
deleted = []
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag in ('delete', 'replace'):
        deleted += old[i1:i2]

print('175 独有（106 没有）的行共 %d 行:' % len(deleted))
for l in deleted:
    print('   - %s' % l)

bad = [l for l in deleted if not any(w in l for w in WHITELIST)]
if bad:
    print('\n❌ 下面这些 175 独有的行不属于"第2步要替换的写死值"，整体覆盖会丢东西：')
    for l in bad:
        print('   ! %s' % l)
    sys.exit(1)
print('\n✅ 175 独有的行全部是第2步要替换掉的写死原值，可以整体覆盖')
