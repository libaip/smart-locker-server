# -*- coding: utf-8 -*-
"""[S522] 支付宝小程序侧：填上模板ID + 撤掉未就绪安全闸
1) src/utils/api.js  TEMPLATES.alipay 填上老板给的 2 个模板
2) src/platform/index.js  ALIPAY_SUBSCRIBE_READY: false -> true
用法：python patch_s522_mp.py
"""
import hashlib
import os
import shutil
import sys

SRC = r'D:\工具配置迁移包_20260811\小程序_通用版\src'
BAK = r'D:\工具配置迁移包_20260811\_小程序通用版_备份_20260921_2250'
API = os.path.join(SRC, 'utils', 'api.js')
PLAT = os.path.join(SRC, 'platform', 'index.js')
FILES = [(API, 'src_utils_api.js'), (PLAT, 'src_platform_index.js')]

buf = {}
for p, _a in FILES:
    buf[p] = open(p, encoding='utf-8', newline='').read()
errors = []


def rep(path, old, new, tag):
    s = buf[path]
    if s.count(old) != 1:
        errors.append('%s 锚点命中 %d 次' % (tag, s.count(old)))
        return
    buf[path] = s.replace(old, new, 1)


rep(API,
    """  // 支付宝：待小程序应用领模板后填兜底值（同样优先从后端读）
  alipay: {
    deposit: '',
    general: '',
    refund: ''
  }""",
    """  // [S522-20260921] 支付宝：老板已领模板（账户余额通知 / 寄存押金退还通知），
  //   兜底值填上；正式运行时同样优先后端 /user/subscribe-templates?platform=alipay
  alipay: {
    deposit: 'c142ac2357774daab8994a0f5a91faa4',
    general: 'c142ac2357774daab8994a0f5a91faa4',
    refund: 'de68d98e94c84477b1b9e116fcb8cbfa'
  }""",
    'api.js 支付宝模板')

rep(PLAT,
    "    const ALIPAY_SUBSCRIBE_READY = false",
    "    const ALIPAY_SUBSCRIBE_READY = true   // [S522] 模板已领到，正式开启支付宝订阅授权",
    'platform 安全闸打开')

if errors:
    print('❌ 锚点没命中，未写文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

os.makedirs(BAK, exist_ok=True)
for p, alias in FILES:
    before = hashlib.md5(open(p, 'rb').read()).hexdigest()
    shutil.copy2(p, os.path.join(BAK, alias))
    open(p, 'w', encoding='utf-8', newline='').write(buf[p])
    after = hashlib.md5(open(p, 'rb').read()).hexdigest()
    print('%-24s %s -> %s' % (alias, before, after))
print('备份: %s' % BAK)
