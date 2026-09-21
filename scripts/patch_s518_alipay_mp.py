# -*- coding: utf-8 -*-
"""
S518-20260921 支付宝小程序：认人只认 alipay_uid（id），不拿手机号当钥匙
=====================================================================
老板要求：
  · "支付宝认证的方式不要用手机号"
  · "不要搞乱微信的认人方式"  → 微信分支一行不动

现状（错在哪）：
  subscribe 页 onLoad 调 `bindMpOpenid(phone)` → api.linkMpOpenid → POST /user/link-mp-openid
  这个后端接口是【微信专用】：它拿 code 去 api.weixin.qq.com/sns/jscode2session 换 openid，
  并且按【手机号】匹配/落行。支付宝小程序把 authCode 传进去 → 换不到微信 openid → 直接 500
  "获取小程序openid失败"。也就是说：支付宝这条支路既用了手机号认人、而且根本是失败的。

改法（只动支付宝分支）：
  · 支付宝：platform.silentLogin()(my.getAuthCode) → api.alipayLogin(code, orderId)
           → POST /alipay/login（这是 S272 就定好的"只用 alipay_uid 认人、绝不拿手机号兜底"接口）
    order_id 一并带上（后端目前忽略它；后端加可选 order_id 后即可按 id 把订单绑到该支付宝用户，
    这样这次上传的版本一步到位，不必再传一次）
  · 微信：完全保持原样（bindMpOpenid(phone) 原封不动）
  · 页面上的手机号只用于给用户核对订单，不再参与认人
用法：python patch_s518_alipay_mp.py
"""
import hashlib
import os
import shutil
import sys

SRC = r'D:\工具配置迁移包_20260811\小程序_通用版\src'
BAK = r'D:\工具配置迁移包_20260811\_小程序通用版_备份_20260921_2040'
API = os.path.join(SRC, 'utils', 'api.js')
SUB = os.path.join(SRC, 'pages', 'subscribe', 'subscribe.vue')
FILES = [(API, 'src_utils_api.js'), (SUB, 'src_subscribe.vue')]

buf = {}
for p, _a in FILES:
    assert os.path.exists(p), p
    buf[p] = open(p, encoding='utf-8', newline='').read()
errors = []


def rep(path, old, new, tag):
    s = buf[path]
    n = s.count(old)
    if n != 1:
        errors.append('%s 锚点命中 %d 次（应为1）' % (tag, n))
        return
    buf[path] = s.replace(old, new, 1)


# 1) api.js: alipayLogin 支持可选 orderId（加法改动，微信接口无一字变化）
rep(API,
    "function alipayLogin(code) { return post('/alipay/login', { code: code }) }",
    "// [S518] orderId 可选：后端加了可选 order_id 支持后，可把订单按 id 绑到该支付宝用户\n"
    "function alipayLogin(code, orderId) {\n"
    "  const d = { code: code }\n"
    "  if (orderId) d.order_id = orderId\n"
    "  return post('/alipay/login', d)\n"
    "}",
    'api.alipayLogin 支持 orderId')

# 2) subscribe.vue: 两处调用点改成 bindIdentity，并新增 bindIdentity（支付宝走 id，微信走原路）
rep(SUB, "    this.bindMpOpenid(phone)", "    this.bindIdentity(phone)", 'onLoad 调用点')
rep(SUB, "      this.bindMpOpenid(ph)", "      this.bindIdentity(ph)", 'onShow 调用点')
rep(SUB,
    "    bindMpOpenid(phone) {",
    """    bindIdentity(phone) {
      // [S518] 支付宝：认人只认 alipay_uid —— my.getAuthCode 拿 authCode，
      //        交给后端 /alipay/login（该接口按 S272 规定"只用 alipay_uid 认人，绝不拿手机号兜底"）。
      //        订单关联用 order_id（id），同样不用手机号。
      //        微信：原样走 bindMpOpenid(phone)（H5 会话 ↔ 小程序 openid 的关联方式保持不动）。
      if (platform.getPlatform() === 'alipay') {
        const that = this
        platform.silentLogin().then((code) => {
          if (!code) return
          return api.alipayLogin(code, that.orderId)
        }).then((res) => {
          console.log('[subscribe] alipay 登录/绑定:', res)
        }).catch((err) => {
          console.error('[subscribe] alipay 登录/绑定失败:', err)
        })
        return
      }
      this.bindMpOpenid(phone)
    },

    bindMpOpenid(phone) {""",
    '新增 bindIdentity')

if errors:
    print('❌ 有锚点没命中，未写任何文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

os.makedirs(BAK, exist_ok=True)
print('改前 md5 / 改后 md5：')
for p, alias in FILES:
    before = hashlib.md5(open(p, 'rb').read()).hexdigest()
    shutil.copy2(p, os.path.join(BAK, alias))
    open(p, 'w', encoding='utf-8', newline='').write(buf[p])
    after = hashlib.md5(open(p, 'rb').read()).hexdigest()
    print('  %-24s %s -> %s' % (alias, before, after))
print('备份目录: %s' % BAK)
