// pages/index/index.js - 首页
var API = require('../../utils/api.js')
var app = getApp()

Page({
  data: {
    phone: '',
    inputPhone: '',
    code: '',
    canRetrieve: false,
    loginLoading: false,
    balance: '0.00',
    openid: '',
    agreementChecked: false
  },

  doWxLogin: function() {
    var that = this
    wx.login({
      success: function(res) {
        if (res.code) {
          API.wxLogin(res.code).then(function(resp) {
            if (resp && resp.data && resp.data.openid) {
              wx.setStorageSync('openid', resp.data.openid)
              app.globalData.openid = resp.data.openid
              that.setData({ openid: resp.data.openid })
            }
            if (resp && resp.data && resp.data.session_key) {
              wx.setStorageSync('sessionKey', resp.data.session_key)
            }
          }).catch(function() {})
        }
      }
    })
  },

  onLoad: function() {
    var phone = wx.getStorageSync('userPhone') || ''
    var openid = wx.getStorageSync('openid') || ''
    this.setData({ phone: phone, openid: openid })
    if (openid && !app.globalData.openid) {
      app.globalData.openid = openid
    }
    if (!wx.getStorageSync('sessionKey')) {
      this.doWxLogin()
    }
  },

  onShow: function() {
    var phone = wx.getStorageSync('userPhone') || ''
    var openid = wx.getStorageSync('openid') || ''
    this.setData({ phone: phone, openid: openid })
    if (phone) this.loadBalance()
  },

  // 手机号输入
  onPhoneInput: function(e) {
    this.setData({ inputPhone: e.detail.value })
  },

  // 取物码输入
  onCodeInput: function(e) {
    this.setData({ code: e.detail.value })
    this.updateCanRetrieve()
  },

  // 协议勾选变化
  onAgreementChange: function(e) {
    var checked = e.detail.value && e.detail.value.length > 0
    this.setData({ agreementChecked: checked })
  },

  // 跳转隐私政策
  onShowPrivacy: function() {
    wx.navigateTo({ url: '/pages/privacy/privacy' })
  },

  // 跳转用户协议
  onShowAgreement: function() {
    wx.navigateTo({ url: '/pages/agreement/agreement' })
  },

  updateCanRetrieve: function() {
    var p = this.data.inputPhone || this.data.phone
    var c = this.data.code
    this.setData({ canRetrieve: p.length === 11 && c.length === 4 })
  },

  // 微信授权手机号登录 - getPhoneNumber回调
  // 关键：getPhoneNumber的加密数据必须用同一次wx.login的session_key解密
  // 所以流程是：先wx.login → 拿到session_key → 再把session_key随decryptPhone一起发后端
  onGetPhoneNumber: function(e) {
    var that = this
    
    // 用户拒绝授权
    if (e.detail.errMsg && e.detail.errMsg.indexOf('fail') !== -1) {
      wx.showToast({ title: '需要授权手机号才能登录', icon: 'none' })
      return
    }
    
    // 验证协议勾选
    if (!this.data.agreementChecked) {
      wx.showToast({ title: '请先阅读并同意用户协议', icon: 'none' })
      return
    }
    
    this.setData({ loginLoading: true })
    wx.showLoading({ title: '登录中...' })
    
    var encryptedData = e.detail.encryptedData || ''
    var iv = e.detail.iv || ''
    
    if (!encryptedData || !iv) {
      wx.hideLoading()
      that.setData({ loginLoading: false })
      wx.showToast({ title: '授权失败，请重试', icon: 'none' })
      return
    }
    
    // wx.login拿code，然后code+encryptedData+iv一起发后端
    // 后端一步到位：code→session_key→解密手机号
    wx.login({
      success: function(loginRes) {
        if (!loginRes.code) {
          wx.hideLoading()
          that.setData({ loginLoading: false })
          wx.showToast({ title: '登录失败', icon: 'none' })
          return
        }
        API.loginPhone(loginRes.code, encryptedData, iv).then(function(resp) {
          wx.hideLoading()
          that.setData({ loginLoading: false })
          var openid = (resp && resp.data && resp.data.openid) || ''
          var phone = (resp && resp.data && resp.data.phone) || ''
          var sessionKey = (resp && resp.data && resp.data.session_key) || ''
          if (openid) {
            wx.setStorageSync('openid', openid)
            app.globalData.openid = openid
            that.setData({ openid: openid })
          }
          if (sessionKey) wx.setStorageSync('sessionKey', sessionKey)
          if (phone) {
            wx.setStorageSync('userPhone', phone)
            that.setData({ phone: phone, agreementChecked: false })
            wx.showToast({ title: '登录成功', icon: 'success' })
            that.loadBalance()
          } else {
            wx.showToast({ title: '获取手机号失败', icon: 'none' })
          }
        }).catch(function(err) {
          wx.hideLoading()
          that.setData({ loginLoading: false })
          console.error('loginPhone failed', err)
          wx.showToast({ title: '登录失败，请重试', icon: 'none' })
        })
      },
      fail: function() {
        wx.hideLoading()
        that.setData({ loginLoading: false })
        wx.showToast({ title: '微信登录失败', icon: 'none' })
      }
    })
  },

  // 余额
  loadBalance: function() {
    var that = this
    var phone = this.data.phone
    var openid = this.data.openid || app.globalData.openid || ''
    if (!phone && !openid) return
    API.getBalance(phone, openid).then(function(res) {
      var d = res.data || res || {}
      that.setData({
        balance: d.balance != null ? parseFloat(d.balance).toFixed(2) : '0.00'
      })
    }).catch(function() {})
  },

  // 扫码存包
  onScanCode: function() {
    var that = this
    var phone = this.data.phone
    var openid = this.data.openid || app.globalData.openid || ''
    if (!phone) {
      wx.showToast({ title: '请先输入手机号', icon: 'none' })
      return
    }
    wx.scanCode({
      scanType: ['qrCode'],
      success: function(res) {
        var url = res.result || ''
        if (url) {
          var cabinetId = ''
          var match = url.match(/cabinet_id=(\d+)/)
          if (match) cabinetId = match[1]
          match = url.match(/device_id=([^\&]+)/)
          if (match) {
            API.getCabinetByMainboard(match[1]).then(function(r) {
              if (r.data && r.data.id) {
                wx.navigateTo({
                  url: '/pages/deposit/deposit?cabinet_id=' + r.data.id + '&phone=' + phone + '&openid=' + openid
                })
              } else {
                wx.showToast({ title: '未找到对应柜体', icon: 'none' })
              }
            }).catch(function() {
              wx.showToast({ title: '查询柜体失败', icon: 'none' })
            })
            return
          }
          match = url.match(/group_code=([^\&]+)/)
          if (match) {
            API.getCabinetByGroupCode(match[1]).then(function(r) {
              if (r.data && r.data.id) {
                wx.navigateTo({
                  url: '/pages/deposit/deposit?cabinet_id=' + r.data.id + '&phone=' + phone + '&openid=' + openid
                })
              } else {
                wx.showToast({ title: '未找到对应柜体', icon: 'none' })
              }
            }).catch(function() {
              wx.showToast({ title: '查询柜体失败', icon: 'none' })
            })
            return
          }
          if (cabinetId) {
            wx.navigateTo({
              url: '/pages/deposit/deposit?cabinet_id=' + cabinetId + '&phone=' + phone + '&openid=' + openid
            })
          } else {
            wx.showToast({ title: '二维码格式不正确', icon: 'none' })
          }
        }
      },
      fail: function(err) {
        if (err.errMsg && err.errMsg.indexOf('cancel') === -1) {
          wx.showToast({ title: '扫码失败', icon: 'none' })
        }
      }
    })
  },

  // 快速取物
  onQuickRetrieve: function() {
    var phone = this.data.inputPhone || this.data.phone
    var code = this.data.code
    if (!phone || phone.length !== 11) {
      wx.showToast({ title: '请输入正确的手机号', icon: 'none' })
      return
    }
    if (!code || code.length !== 4) {
      wx.showToast({ title: '请输入4位取物码', icon: 'none' })
      return
    }
    var openid = this.data.openid || app.globalData.openid || ''
    wx.navigateTo({
      url: '/pages/retrieve/retrieve?phone=' + phone + '&code=' + code + '&openid=' + openid
    })
  },

  // 我的订单
  onMyOrders: function() {
    wx.navigateTo({ url: '/pages/orders/orders' })
  },

  // 退出
  onLogout: function() {
    var that = this
    wx.showModal({
      title: '提示',
      content: '确定退出登录？',
      success: function(res) {
        if (res.confirm) {
          wx.removeStorageSync('userPhone')
          that.setData({ phone: '', balance: '0.00' })
        }
      }
    })
  }
})
