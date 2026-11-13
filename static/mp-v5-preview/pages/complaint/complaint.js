// pages/complaint/complaint.js
const API = require('../../utils/api.js')

Page({
  data: {
    statusBarHeight: 20,
    userPhone: '',
    content: '',
    orderIndex: 0,
    orderNames: ['请选择'],
    orderNos: [''],
    isSubmitting: false,
    submitted: false
  },

  onLoad() {
    const sysInfo = wx.getSystemInfoSync()
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    this.setData({ statusBarHeight: sysInfo.statusBarHeight || 20, userPhone: phone })
    this.loadOrders()
  },

  loadOrders() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    API.getOrders(phone, openid).then(res => {
      const orders = res.data || []
      const names = ['请选择']
      const nos = ['']
      orders.forEach(o => {
        if (parseInt(o.status) === 3) {
          names.push((o.order_no || '') + ' (' + (o.cabinet_name || '') + ')')
          nos.push(o.order_no || '')
        }
      })
      this.setData({ orderNames: names, orderNos: nos })
    }).catch(() => {})
  },

  onPhoneInput(e) {
    this.setData({ userPhone: e.detail.value })
  },

  onContentInput(e) {
    const val = e.detail.value
    if (val.length <= 140) {
      this.setData({ content: val })
    }
  },

  onOrderSelect(e) {
    this.setData({ orderIndex: parseInt(e.detail.value) })
  },

  goBack() {
    wx.navigateBack({ delta: 1, fail: () => {
      wx.switchTab({ url: '/pages/mine/mine' })
    }})
  },

  onSubmit() {
    const { content, userPhone, orderNos, orderIndex, isSubmitting } = this.data
    if (isSubmitting) return
    if (!content || !content.trim()) {
      wx.showToast({ title: '请填写投诉描述', icon: 'none' })
      return
    }
    this.setData({ isSubmitting: true })
    const phone = wx.getStorageSync('phone') || userPhone
    const openid = wx.getStorageSync('openid') || ''
    API.submitComplaint({
      type: 'self',
      content: content.trim(),
      orderNo: orderNos[orderIndex] || '',
      userPhone: phone,
      openid: openid
    }).then(() => {
      this.setData({ isSubmitting: false, submitted: true })
    }).catch(err => {
      this.setData({ isSubmitting: false })
      wx.showToast({ title: err.message || '提交失败', icon: 'none' })
    })
  }
})
