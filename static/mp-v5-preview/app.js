// app.js - 用户端小程序入口
App({
  globalData: {
    baseUrl: 'https://locker.cqdyxl.com/api',
    payMode: 'wechat',
    appId: 'wx57eaea52dcfff4e8',
    openid: '',
    userPhone: ''
  },

  onLaunch() {
    console.log('智能寄存柜-用户端小程序启动')
    this.autoLogin()
  },

  autoLogin() {
    var that = this
    wx.login({
      success: function(res) {
        if (res.code) {
          wx.request({
            url: that.globalData.baseUrl + '/wx/login',
            method: 'POST',
            data: { code: res.code },
            header: { 'Content-Type': 'application/json' },
            success: function(resp) {
              if (resp.data && resp.data.code === 200 && resp.data.data && resp.data.data.openid) {
                that.globalData.openid = resp.data.data.openid
                wx.setStorageSync('openid', resp.data.data.openid)
                console.log('[登录] openid获取成功')
              } else {
                console.log('[登录] openid获取失败，将使用手机号流程')
              }
            },
            fail: function() {
              console.log('[登录] wx/login请求失败，将使用手机号流程')
            }
          })
        }
      }
    })
  }
})
