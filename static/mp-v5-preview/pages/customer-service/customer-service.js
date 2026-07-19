// pages/customer-service/customer-service.js

Page({
  data: {
    statusBarHeight: 20
  },

  onLoad() {
    const sysInfo = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: sysInfo.statusBarHeight || 20 })
  },

  goBack() {
    wx.navigateBack({ delta: 1, fail: () => {
      wx.switchTab({ url: '/pages/mine/mine' })
    }})
  },

  goComplaint() {
    wx.navigateTo({ url: '/pages/complaint/complaint' })
  },

  callService() {
    wx.makePhoneCall({
      phoneNumber: '4006981080',
      fail: () => {}
    })
  }
})
