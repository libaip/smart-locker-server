# -*- coding: utf-8 -*-
"""
S517-20260921 支付宝小程序：①能跳回 H5 ②订阅模板改用运行时 ③参数兜底
=====================================================================
背景（老板实测两个问题）：
  A. H5 跳进小程序没收到手机号  -> H5 侧已修（S516：scheme 的 page 要用原样路径）
  B. 点完"确认"没跳回 H5，还停在小程序的存包信息页
     - 编译产物 platform/index.js 的 exitToH5() 忽略入参、只调 my.exitMiniProgram()，没有 fail 兜底
  C. 附带发现：subscribe 页 onConfirmInfo() 读的是静态 TEMPLATES.alipay（全空）
     -> ids=[] -> 直接 console.warn("无可用订阅模板,跳过授权")，订阅授权【根本没申请】
     而后端 /user/subscribe-templates 是支持的、api.js 里也有 fetchSubscribeTemplates()

本次改动（源码 + 编译产物同时改，避免下次构建被覆盖）：
  1) platform/index.js exitToH5(options)：
     优先 my.ap.openURL(return_url) 跳回 H5 -> 失败再 my.exitMiniProgram({fail})
     -> 都失败弹"请点右上角×/左上角←关闭小程序"
  2) pages/subscribe：onLoad 存 h5Url（来自 return_url），onShow 兜底再取一次启动参数
     （支付宝 5 分钟内复用实例时 onLoad 不会重跑），onBackToH5 把 url 传给 exitToH5
  3) onConfirmInfo 改用 fetchSubscribeTemplates()/getSubscribeIds()（后端模板优先，静态兜底）
  4) 第二个按钮文案「确认」-> 「返回继续支付」，提示文案同步
用法：python patch_s517_alipay_mp.py
"""
import hashlib
import os
import shutil
import sys

SRC = r'D:\工具配置迁移包_20260811\小程序_通用版\src'
OUT = r'D:\支付宝小程序'
BAK = r'D:\支付宝小程序\_backups_s517_20260921'
FILES = [
    (os.path.join(SRC, 'platform', 'index.js'), 'src_platform_index.js'),
    (os.path.join(SRC, 'pages', 'subscribe', 'subscribe.vue'), 'src_subscribe.vue'),
    (os.path.join(OUT, 'platform', 'index.js'), 'out_platform_index.js'),
    (os.path.join(OUT, 'pages', 'subscribe', 'subscribe.js'), 'out_subscribe.js'),
    (os.path.join(OUT, 'pages', 'subscribe', 'subscribe.axml'), 'out_subscribe.axml'),
]

# ---------- 先全部读进来 + 备份 + 断言锚点，全部通过才写盘 ----------
buf = {}
for p, _alias in FILES:
    assert os.path.exists(p), '文件不存在: %s' % p
    buf[p] = open(p, encoding='utf-8', newline='').read()

errors = []


def rep(path, old, new, tag):
    s = buf[path]
    n = s.count(old)
    if n != 1:
        errors.append('%s 锚点命中 %d 次（应为1）: %s' % (tag, n, old[:80]))
        return
    buf[path] = s.replace(old, new, 1)


# ============ 1) 源码 platform/index.js：exitToH5 支付宝分支 ============
SRC_PLAT = os.path.join(SRC, 'platform', 'index.js')
OLD = """  // #ifdef MP-ALIPAY
  // 支付宝无 exitMiniProgram；提示用户关闭小程序返回支付宝内网页继续
  uni.showModal({
    title: '订阅成功',
    content: '请关闭本小程序，返回支付宝网页继续完成支付',
    showCancel: false,
    success: () => {
      // 支付宝小程序无法编程退出，引导用户手动关闭；
      // 若后续确认支付宝支持 my.exitMiniProgram 可在此调用
      try {
        // #ifdef MP-ALIPAY
        // 部分支付宝基础库支持 my.exitMiniProgram（以官方文档为准，先用条件探测）
        if (typeof my !== 'undefined' && my.exitMiniProgram) {
          my.exitMiniProgram()
        }
        // #endif
      } catch (e) {
        // ignore
      }
    }
  })
  // #endif"""
NEW = """  // #ifdef MP-ALIPAY
  // [S517] 支付宝：优先用 my.ap.openURL 直接跳回 H5(return_url)；失败再试 my.exitMiniProgram；
  //        两者都不行才弹提示让用户手动关闭（原实现只调 exitMiniProgram 且无 fail 兜底 -> 老板实测卡住）
  const h5url = opts.url || opts.h5Url || ''
  const tipManual = () => {
    uni.showModal({
      title: '请返回支付宝网页',
      content: '点右上角「×」或左上角「←」关闭本小程序，即可回到支付宝网页继续完成支付',
      showCancel: false,
      confirmText: '知道了'
    })
  }
  const tryExit = () => {
    try {
      if (typeof my !== 'undefined' && typeof my.exitMiniProgram === 'function') {
        my.exitMiniProgram({ fail: () => tipManual() })
        return
      }
    } catch (e) {
      // ignore
    }
    tipManual()
  }
  uni.showModal({
    title: '订阅成功',
    content: '点「确定」返回支付宝网页继续完成支付',
    showCancel: false,
    confirmText: '返回继续支付',
    success: () => {
      let opened = false
      try {
        if (h5url && typeof my !== 'undefined' && my.ap && typeof my.ap.openURL === 'function') {
          opened = true
          my.ap.openURL({ url: h5url, fail: () => tryExit() })
        }
      } catch (e) {
        opened = false
      }
      if (!opened) tryExit()
    },
    fail: () => tryExit()
  })
  // #endif"""
rep(SRC_PLAT, OLD, NEW, 'src/platform exitToH5')

# ============ 2) 源码 subscribe.vue ============
SRC_SUB = os.path.join(SRC, 'pages', 'subscribe', 'subscribe.vue')
# 2.1 data 加 h5Url
rep(SRC_SUB,
    "      orderId: '',\n      fromH5: false,",
    "      orderId: '',\n      h5Url: '',\n      fromH5: false,",
    'src subscribe data.h5Url')
# 2.2 onLoad 存 return_url
rep(SRC_SUB,
    "    this.orderId = orderId\n\n    // 绑定小程序身份",
    "    this.orderId = orderId\n"
    "    // [S517] H5 跳过来时带了 return_url，确认后用它跳回原页面\n"
    "    this.h5Url = opts.return_url ? decodeURIComponent(opts.return_url) : ''\n\n"
    "    // 绑定小程序身份",
    'src subscribe onLoad h5Url')
# 2.3 onShow 兜底（支付宝复用实例时 onLoad 不重跑）
rep(SRC_SUB,
    "  methods: {\n    bindMpOpenid(phone) {",
    """  onShow() {
    // [S517] 支付宝 5 分钟内复用小程序实例时 onLoad 不会重跑，scheme 参数只在启动参数里，
    //        这里兜底再取一次（仅在还没拿到手机号时生效，不覆盖已有信息）
    try {
      if (this.phone) return
      const lo = (typeof my !== 'undefined' && my.getLaunchOptionsSync) ? my.getLaunchOptionsSync() : null
      const q = (lo && lo.query) || {}
      if (!q || (!q.phone && !(q.g && q.h && q.j))) return
      const ph = q.phone ? decodeURIComponent(q.phone) : ((q.g || '') + (q.h || '') + (q.j || ''))
      if (!ph) return
      this.phone = ph
      this.displayPhone = ph.length === 11 ? (ph.substring(0, 3) + ' ' + ph.substring(3, 7) + ' ' + ph.substring(7)) : ph
      this.maskedPhone = ph.length === 11 ? (ph.substring(0, 3) + '****' + ph.substring(7)) : ph
      this.accessCode = q.code || q.access_code || ''
      this.orderId = q.order_id || ''
      if (q.return_url) this.h5Url = decodeURIComponent(q.return_url)
      this.bindMpOpenid(ph)
    } catch (e) {
      // ignore
    }
  },
  methods: {
    bindMpOpenid(phone) {""",
    'src subscribe onShow')
# 2.4 onConfirmInfo 用运行时模板
rep(SRC_SUB,
    """    onConfirmInfo() {
      // 唤起订阅授权（微信/支付宝各自模板，取当前平台能用的）
      const plat = platform.getPlatform()
      const templates = api.TEMPLATES[plat === 'alipay' ? 'alipay' : 'wechat']
      const ids = []
      if (templates) {
        if (templates.withdraw) ids.push(templates.withdraw)
        if (templates.general) ids.push(templates.general)
      }
      // 模板未配置（支付宝待领模板）时跳过授权直接继续
      if (ids.length === 0) {
        console.warn('[subscribe] 无可用订阅模板,跳过授权')
        this.subscribed = true
        return
      }
      platform.requestSubscribe(ids).then(() => {
        this.subscribed = true
      })
    },""",
    """    onConfirmInfo() {
      // [S517] 改用运行时模板：先问后端 /user/subscribe-templates（换模板不用发版），
      //        拿不到再退回静态兜底 TEMPLATES。原实现只读静态 TEMPLATES.alipay（全空）
      //        -> ids=[] -> 直接跳过，订阅授权实际从未申请。
      const that = this
      const finish = () => {
        const ids = api.getSubscribeIds ? api.getSubscribeIds(['withdraw', 'refund', 'general']) : []
        if (!ids.length) {
          console.warn('[subscribe] 无可用订阅模板,跳过授权')
          that.subscribed = true
          return
        }
        platform.requestSubscribe(ids).then(() => {
          that.subscribed = true
        })
      }
      try {
        if (api.fetchSubscribeTemplates) {
          api.fetchSubscribeTemplates().then(finish).catch(finish)
          return
        }
      } catch (e) {
        // ignore
      }
      finish()
    },""",
    'src subscribe onConfirmInfo')
# 2.5 onBackToH5 传 url
rep(SRC_SUB,
    """        platform.exitToH5({
          phone: this.phone,
          orderId: this.orderId,
          fromH5: true
        })""",
    """        platform.exitToH5({
          phone: this.phone,
          orderId: this.orderId,
          url: this.h5Url,
          fromH5: true
        })""",
    'src subscribe onBackToH5 url')
# 2.6 模板文案
rep(SRC_SUB,
    """        <view class="card-btn" @tap="onBackToH5">确认</view>
        <view v-if="isAlipay" class="tip">请关闭本小程序，返回支付宝网页继续支付</view>""",
    """        <view class="card-btn" @tap="onBackToH5">返回继续支付</view>
        <view v-if="isAlipay" class="tip">点上方按钮自动跳回支付宝网页；若没反应，请点右上角「×」关闭小程序</view>""",
    'src subscribe 文案')

# ============ 3) 编译产物 platform/index.js ============
OUT_PLAT = os.path.join(OUT, 'platform', 'index.js')
OLD = ('function exitToH5(options){common_vendor.index.showModal({title:"订阅成功",'
       'content:"请关闭本小程序，返回支付宝网页继续完成支付",showCancel:false,success:()=>{'
       'try{if(typeof my!=="undefined"&&my.exitMiniProgram){my.exitMiniProgram()}}catch(e){}}})}')
NEW = ('function exitToH5(options){var o=options||{};var h5=o.url||o.h5Url||"";'
       'var tipManual=function(){common_vendor.index.showModal({title:"请返回支付宝网页",'
       'content:"点右上角「×」或左上角「←」关闭本小程序，即可回到支付宝网页继续完成支付",'
       'showCancel:false,confirmText:"知道了"})};'
       'var tryExit=function(){try{if(typeof my!=="undefined"&&typeof my.exitMiniProgram==="function"){'
       'my.exitMiniProgram({fail:function(){tipManual()}});return}}catch(e){}tipManual()};'
       'common_vendor.index.showModal({title:"订阅成功",content:"点「确定」返回支付宝网页继续完成支付",'
       'showCancel:false,confirmText:"返回继续支付",success:function(){var opened=false;'
       'try{if(h5&&typeof my!=="undefined"&&my.ap&&typeof my.ap.openURL==="function"){opened=true;'
       'my.ap.openURL({url:h5,fail:function(){tryExit()}})}}catch(e){opened=false}'
       'if(!opened)tryExit()},fail:function(){tryExit()}})}')
rep(OUT_PLAT, OLD, NEW, 'out/platform exitToH5')

# ============ 4) 编译产物 pages/subscribe/subscribe.js ============
OUT_SUB = os.path.join(OUT, 'pages', 'subscribe', 'subscribe.js')
# 4.1 data 加 h5Url
rep(OUT_SUB,
    'orderId:"",fromH5:false,isAlipay:false}}',
    'orderId:"",h5Url:"",fromH5:false,isAlipay:false}}',
    'out subscribe data.h5Url')
# 4.2 onLoad 存 h5Url + onShow
rep(OUT_SUB,
    'this.orderId=orderId;this.bindMpOpenid(phone)},methods:{',
    'this.orderId=orderId;this.h5Url=opts.return_url?decodeURIComponent(opts.return_url):"";'
    'this.bindMpOpenid(phone)},'
    'onShow(){try{if(this.phone)return;var lo=(typeof my!=="undefined"&&my.getLaunchOptionsSync)?my.getLaunchOptionsSync():null;'
    'var q=(lo&&lo.query)||{};if(!q||(!q.phone&&!(q.g&&q.h&&q.j)))return;'
    'var ph=q.phone?decodeURIComponent(q.phone):((q.g||"")+(q.h||"")+(q.j||""));if(!ph)return;'
    'this.phone=ph;'
    'this.displayPhone=ph.length===11?(ph.substring(0,3)+" "+ph.substring(3,7)+" "+ph.substring(7)):ph;'
    'this.maskedPhone=ph.length===11?(ph.substring(0,3)+"****"+ph.substring(7)):ph;'
    'this.accessCode=q.code||q.access_code||"";this.orderId=q.order_id||"";'
    'if(q.return_url)this.h5Url=decodeURIComponent(q.return_url);this.bindMpOpenid(ph)}catch(e){}},methods:{',
    'out subscribe onLoad/onShow')
# 4.3 onConfirmInfo 用运行时模板
OLD = ('onConfirmInfo(){const templates=utils_api.apiModule.TEMPLATES["alipay"];const ids=[];'
       'if(templates){if(templates.withdraw)ids.push(templates.withdraw);'
       'if(templates.general)ids.push(templates.general)}'
       'if(ids.length===0){console.warn("[subscribe] 无可用订阅模板,跳过授权");this.subscribed=true;return}'
       'platform_index.platform.requestSubscribe(ids).then(()=>{this.subscribed=true})}')
NEW = ('onConfirmInfo(){const that=this;const finish=()=>{const ids=utils_api.apiModule.getSubscribeIds'
       '?utils_api.apiModule.getSubscribeIds(["withdraw","refund","general"]):[];'
       'if(!ids.length){console.warn("[subscribe] 无可用订阅模板,跳过授权");that.subscribed=true;return}'
       'platform_index.platform.requestSubscribe(ids).then(()=>{that.subscribed=true})};'
       'try{if(utils_api.apiModule.fetchSubscribeTemplates){utils_api.apiModule.fetchSubscribeTemplates()'
       '.then(finish).catch(finish);return}}catch(e){}finish()}')
rep(OUT_SUB, OLD, NEW, 'out subscribe onConfirmInfo')
# 4.4 onBackToH5 传 url
rep(OUT_SUB,
    'platform_index.platform.exitToH5({phone:this.phone,orderId:this.orderId,fromH5:true})',
    'platform_index.platform.exitToH5({phone:this.phone,orderId:this.orderId,url:this.h5Url,fromH5:true})',
    'out subscribe onBackToH5 url')

# ============ 5) 编译产物 subscribe.axml 文案 ============
OUT_AXML = os.path.join(OUT, 'pages', 'subscribe', 'subscribe.axml')
rep(OUT_AXML,
    '<view class="card-btn" onTap="{{g}}">确认</view><view a:if="{{h}}" class="tip">请关闭本小程序，返回支付宝网页继续支付</view>',
    '<view class="card-btn" onTap="{{g}}">返回继续支付</view><view a:if="{{h}}" class="tip">点上方按钮自动跳回支付宝网页；若没反应，请点右上角「×」关闭小程序</view>',
    'out subscribe.axml 文案')

if errors:
    print('❌ 有锚点没命中，未写任何文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

# ---------- 备份 + 写盘 ----------
os.makedirs(BAK, exist_ok=True)
print('改前 md5 / 改后 md5：')
for p, alias in FILES:
    before = hashlib.md5(open(p, 'rb').read()).hexdigest()
    shutil.copy2(p, os.path.join(BAK, alias))
    open(p, 'w', encoding='utf-8', newline='').write(buf[p])
    after = hashlib.md5(open(p, 'rb').read()).hexdigest()
    print('  %-28s %s -> %s' % (alias, before, after))
print('备份目录: %s' % BAK)
