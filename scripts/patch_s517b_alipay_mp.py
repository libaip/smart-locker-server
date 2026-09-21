# -*- coding: utf-8 -*-
"""
S517b-20260921 支付宝小程序补两处
==================================
1) 订阅授权加安全闸：线上 wx_templates 里【只有微信的 mp/oa 模板，没有任何支付宝模板】，
   后端 /user/subscribe-templates?platform=alipay 现在把微信模板ID原样返回；
   若直接丢给 my.requestSubscribeMessage 必然失败、还可能在用户侧弹系统错误。
   -> 在 platform.requestSubscribe 的支付宝分支加常量开关 ALIPAY_SUBSCRIBE_READY=false：
      未就绪直接 resolve(false)（不弹错、流程继续）。支付宝后台建好模板 + 后端能下发
      支付宝模板ID 后，把它改成 true 即可。
2) 「确认」后自动回 H5：老板实测点完确认停在小程序存包信息页出不来。
   授权/跳过完成后 600ms 自动调 onBackToH5()，原按钮保留作兜底。
用法：python patch_s517b_alipay_mp.py
"""
import hashlib
import os
import shutil
import sys

SRC = r'D:\工具配置迁移包_20260811\小程序_通用版\src'
OUT = r'D:\支付宝小程序'
BAK = r'D:\支付宝小程序\_backups_s517b_20260921'
SRC_PLAT = os.path.join(SRC, 'platform', 'index.js')
SRC_SUB = os.path.join(SRC, 'pages', 'subscribe', 'subscribe.vue')
OUT_PLAT = os.path.join(OUT, 'platform', 'index.js')
OUT_SUB = os.path.join(OUT, 'pages', 'subscribe', 'subscribe.js')
FILES = [(SRC_PLAT, 'src_platform_index.js'), (SRC_SUB, 'src_subscribe.vue'),
         (OUT_PLAT, 'out_platform_index.js'), (OUT_SUB, 'out_subscribe.js')]

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


# ============ 1) 源码 platform/index.js：支付宝订阅安全闸 ============
rep(SRC_PLAT,
    """    // #ifdef MP-ALIPAY
    // 支付宝: my.requestSubscribeMessage，参数为 entityIds""",
    """    // #ifdef MP-ALIPAY
    // [S517b] 安全闸：线上模板表(wx_templates)里只有微信的 mp/oa 模板，没有支付宝模板，
    //   后端 /user/subscribe-templates?platform=alipay 目前会把微信模板ID原样返回，
    //   直接丢给支付宝必然失败、还可能在用户侧弹系统错误 -> 未就绪时不发起，直接返回 false。
    //   等支付宝小程序后台建好订阅消息模板、且后端能下发支付宝模板ID后，把下面改成 true。
    const ALIPAY_SUBSCRIBE_READY = false
    if (!ALIPAY_SUBSCRIBE_READY) {
      console.warn('[subscribe] 支付宝订阅模板未就绪，本次跳过授权（不报错）')
      resolve(false)
      return
    }
    // 支付宝: my.requestSubscribeMessage，参数为 entityIds""",
    'src requestSubscribe 安全闸')

# ============ 2) 源码 subscribe.vue：自动回 H5 ============
rep(SRC_SUB,
    """        if (!ids.length) {
          console.warn('[subscribe] 无可用订阅模板,跳过授权')
          that.subscribed = true
          return
        }
        platform.requestSubscribe(ids).then(() => {
          that.subscribed = true
        })
      }""",
    """        if (!ids.length) {
          console.warn('[subscribe] 无可用订阅模板,跳过授权')
          that.subscribed = true
          that.autoBack()
          return
        }
        platform.requestSubscribe(ids).then(() => {
          that.subscribed = true
          that.autoBack()
        })
      }""",
    'src onConfirmInfo autoBack')
rep(SRC_SUB,
    "  methods: {\n    bindMpOpenid(phone) {",
    """  methods: {
    autoBack() {
      // [S517b] 授权/跳过完成后自动回 H5（老板实测：点完"确认"停在小程序里出不来）。
      //         留 600ms 让"存包信息"先渲染出来，按钮仍保留作兜底。
      const that = this
      setTimeout(() => {
        try {
          that.onBackToH5()
        } catch (e) {
          // ignore
        }
      }, 600)
    },
    bindMpOpenid(phone) {""",
    'src autoBack 方法')

# ============ 3) 编译产物 platform/index.js：安全闸 ============
rep(OUT_PLAT,
    'function requestSubscribe(tmplIds){const ids=tmplIds||[];return new Promise(resolve=>{my.requestSubscribeMessage({',
    'const ALIPAY_SUBSCRIBE_READY=false;'
    'function requestSubscribe(tmplIds){const ids=tmplIds||[];return new Promise(resolve=>{'
    'if(!ALIPAY_SUBSCRIBE_READY){console.warn("[subscribe] 支付宝订阅模板未就绪，本次跳过授权（不报错）");'
    'resolve(false);return}'
    'my.requestSubscribeMessage({',
    'out requestSubscribe 安全闸')

# ============ 4) 编译产物 subscribe.js：自动回 H5 ============
rep(OUT_SUB,
    'if(!ids.length){console.warn("[subscribe] 无可用订阅模板,跳过授权");that.subscribed=true;return}'
    'platform_index.platform.requestSubscribe(ids).then(()=>{that.subscribed=true})};',
    'if(!ids.length){console.warn("[subscribe] 无可用订阅模板,跳过授权");that.subscribed=true;that.autoBack();return}'
    'platform_index.platform.requestSubscribe(ids).then(()=>{that.subscribed=true;that.autoBack()})};',
    'out onConfirmInfo autoBack')
rep(OUT_SUB,
    'methods:{bindMpOpenid(phone){',
    'methods:{autoBack(){const that=this;setTimeout(function(){try{that.onBackToH5()}catch(e){}},600)},'
    'bindMpOpenid(phone){',
    'out autoBack 方法')

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
    print('  %-26s %s -> %s' % (alias, before, after))
print('备份目录: %s' % BAK)
