// pages/withdraw-progress/withdraw-progress.js
const API = require('../../utils/api.js')

Page({
  data: {
    statusBarHeight: 20,
    amount: '--',
    wdTime: '--',
    wdPhone: '--'
  },

  onLoad(options) {
    const sysInfo = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: sysInfo.statusBarHeight || 20 })

    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''

    // Mask phone
    let maskedPhone = '--'
    if (phone && phone.length >= 7) {
      maskedPhone = phone.substring(0, 3) + '****' + phone.substring(phone.length - 4)
    } else if (phone) {
      maskedPhone = phone
    }
    this.setData({ wdPhone: maskedPhone })

    // Set amount from URL params or fetch
    if (options.amount) {
      this.setData({ amount: parseFloat(options.amount).toFixed(2) })
    } else {
      this.fetchLatestWithdrawal()
    }

    // Set time
    const now = new Date()
    const y = now.getFullYear()
    const mo = ('0' + (now.getMonth() + 1)).slice(-2)
    const d = ('0' + now.getDate()).slice(-2)
    const h = ('0' + now.getHours()).slice(-2)
    const mi = ('0' + now.getMinutes()).slice(-2)
    const s = ('0' + now.getSeconds()).slice(-2)
    this.setData({ wdTime: `${y}-${mo}-${d} ${h}:${mi}:${s}` })
  },

  fetchLatestWithdrawal() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    API.get('/user/wallet/withdrawals', { phone, openid }).then(res => {
      const list = res.data || []
      if (list.length > 0) {
        const latest = list[0]
        const totalAmt = parseFloat(latest.amount || 0)
        const latestTime = latest.apply_time || ''
        let sum = totalAmt
        for (let i = 1; i < list.length; i++) {
          if (list[i].apply_time && list[i].apply_time === latestTime) {
            sum += parseFloat(list[i].amount || 0)
          } else break
        }
        this.setData({ amount: sum.toFixed(2) })
        if (latest.apply_time) {
          const t = String(latest.apply_time).replace('T', ' ').substring(0, 19)
          this.setData({ wdTime: t })
        }
      }
    }).catch(() => {})
  },

  goBack() {
    wx.navigateBack({ delta: 1 })
  },

  goRecord() {
    wx.navigateTo({ url: '/pages/withdraw-record/withdraw-record' })
  },

  goWallet() {
    wx.navigateBack({ delta: 1 })
  }
})
