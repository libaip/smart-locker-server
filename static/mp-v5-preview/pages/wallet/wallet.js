// pages/wallet/wallet.js
const API = require('../../utils/api.js')

Page({
  data: {
    statusBarHeight: 20,
    balance: '0.00',
    withdrawalRules: ''
  },

  onLoad() {
    const sysInfo = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: sysInfo.statusBarHeight || 20 })
    this.fetchBalance()
    this.fetchWithdrawalRules()
  },

  onShow() {
    this.fetchBalance()
  },

  fetchBalance() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    API.getBalance(phone, openid).then(res => {
      const data = res.data || {}
      const balance = parseFloat(data.balance || data.available_balance || 0)
      this.setData({ balance: balance.toFixed(2) })
    }).catch(() => {})
  },

  fetchWithdrawalRules() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    let url = 'https://locker.cqdyxl.com/api/user/info?'
    if (phone) url += 'phone=' + encodeURIComponent(phone)
    if (openid) url += '&openid=' + encodeURIComponent(openid)
    wx.request({
      url, method: 'GET',
      success: (res) => {
        if (res.data && (res.data.code === 0 || res.data.code === 200)) {
          this.setData({ withdrawalRules: (res.data.data || {}).withdrawal_rules || '' })
        }
      }
    })
  },

  goBack() {
    wx.navigateBack({ delta: 1, fail: () => {
      wx.switchTab({ url: '/pages/mine/mine' })
    }})
  },

  handleWithdraw() {
    // TODO: 跳转到提现页面（复用已有withdraw页面）
    wx.navigateTo({ url: '/pages/withdraw/withdraw' })
  },

  showRule() {
    const rules = this.data.withdrawalRules || '提现无最低金额限制\n提现将在1-3个工作日内原路退回支付账户\n如有疑问请联系客服'
    wx.showModal({
      title: '提现规则',
      content: rules,
      showCancel: false
    })
  },

  goWithdrawRecord() {
    wx.navigateTo({ url: '/pages/withdraw-record/withdraw-record' })
  },

  goTransactions() {
    wx.navigateTo({ url: '/pages/transactions/transactions' })
  },

  goCustomerService() {
    wx.navigateTo({ url: '/pages/customer-service/customer-service' })
  },

  goComplaint() {
    wx.navigateTo({ url: '/pages/complaint/complaint' })
  }
})
