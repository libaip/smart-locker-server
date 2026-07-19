// pages/orders/orders.js
const API = require('../../utils/api.js')

Page({
  data: {
    statusBarHeight: 20,
    currentTab: 'ongoing',
    loading: false,
    ongoingOrders: [],
    doneOrders: [],
    showModal: false,
    modalPhone: '',
    modalCode: ''
  },

  onLoad() {
    const sysInfo = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: sysInfo.statusBarHeight || 20 })
    this.loadOrders()
  },

  onShow() {
    this.loadOrders()
  },

  switchTab(e) {
    this.setData({ currentTab: e.currentTarget.dataset.tab })
  },

  goBack() {
    wx.navigateBack({ delta: 1, fail: () => {
      wx.switchTab({ url: '/pages/mine/mine' })
    }})
  },

  loadOrders() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    this.setData({ loading: true })
    API.getOrders(phone, openid).then(res => {
      const list = res.data || []
      const ongoing = []
      const done = []
      list.forEach(o => {
        const item = this.formatOrder(o, phone)
        if (parseInt(o.status) === 2) ongoing.push(item)
        else done.push(item)
      })
      this.setData({ ongoingOrders: ongoing, doneOrders: done, loading: false })
    }).catch(() => {
      this.setData({ loading: false })
    })
  },

  formatOrder(o, phone) {
    let storeTimeFmt = ''
    if (o.store_time) {
      storeTimeFmt = String(o.store_time).replace('T', ' ').substring(0, 16)
    }
    let elapsed = ''
    if (parseInt(o.status) === 2 && o.store_time) {
      const d = Date.now() - new Date(o.store_time).getTime()
      if (d > 0) {
        const h = Math.floor(d / 3600000)
        const m = Math.floor((d % 3600000) / 60000)
        const parts = []
        if (h > 0) parts.push(h + '小时')
        parts.push(m + '分')
        elapsed = parts.join('')
      }
    }
    return {
      id: o.id,
      order_no: o.order_no || '',
      cabinet_name: o.cabinet_name || ('柜' + o.cabinet_id),
      compartment_number: o.compartment_number || '',
      cabinet_code: o.cabinet_code || '',
      store_time_fmt: storeTimeFmt,
      location_name: o.location_name || '',
      user_phone: o.user_phone || phone || '',
      access_code: o.access_code || '',
      elapsed: elapsed
    }
  },

  showOrderModal(e) {
    const { phone, code } = e.currentTarget.dataset
    this.setData({
      showModal: true,
      modalPhone: phone || '--',
      modalCode: code || '--'
    })
  },

  closeModal() {
    this.setData({ showModal: false })
  },

  midOpenDoor(e) {
    const { id, cabinet, code } = e.currentTarget.dataset
    wx.showModal({
      title: '确认',
      content: '确认要中途开门？',
      success: (res) => {
        if (res.confirm) {
          const phone = wx.getStorageSync('phone') || ''
          API.post('/deposit/mid-retrieve', {
            order_id: id,
            cabinet_code: cabinet,
            access_code: code,
            phone: phone
          }).then(() => {
            wx.showToast({ title: '开门成功', icon: 'success' })
          }).catch(err => {
            wx.showToast({ title: '开门失败: ' + (err.message || ''), icon: 'none' })
          })
        }
      }
    })
  },

  endOrder(e) {
    const id = e.currentTarget.dataset.id
    wx.showModal({
      title: '确认',
      content: '确认要结束订单？押金将退到余额',
      success: (res) => {
        if (res.confirm) {
          const phone = wx.getStorageSync('phone') || ''
          API.post('/deposit/end-storage', {
            order_id: id,
            phone: phone
          }).then(() => {
            wx.showToast({ title: '订单已结束', icon: 'success' })
            setTimeout(() => this.loadOrders(), 1500)
          }).catch(err => {
            wx.showToast({ title: '操作失败', icon: 'none' })
          })
        }
      }
    })
  }
})
