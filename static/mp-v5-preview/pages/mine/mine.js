const app = getApp()

Page({
  data: {
    currentPage: 'main',  // 'main' | 'balance' | 'rules'
    phone: '',
    balance: '0.00',
    orderCountText: '',
    avatarUrl: '',
    showComplaint: false,
    complaintText: ''
  },

  onLoad(options) {
    this.loadUserInfo()
    this.fetchBalance()
    this.fetchOrderCount()
  },

  onShow() {
    this.fetchBalance()
    this.fetchOrderCount()
  },

  // 加载用户信息
  loadUserInfo() {
    const userInfo = wx.getStorageSync('userInfo') || {}
    const phone = userInfo.phone || wx.getStorageSync('phone') || ''
    const avatarUrl = userInfo.avatarUrl || ''
    this.setData({
      phone: this.maskPhone(phone),
      avatarUrl: avatarUrl
    })
  },

  // 手机号脱敏
  maskPhone(phone) {
    if (!phone || phone.length < 7) return phone || '未登录'
    return phone.substring(0, 3) + '****' + phone.substring(phone.length - 4)
  },

  // 获取余额
  fetchBalance() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    let url = 'https://locker.cqdyxl.com/api/user/balance?'
    if (phone) url += 'phone=' + encodeURIComponent(phone)
    if (openid) url += '&openid=' + encodeURIComponent(openid)

    wx.request({
      url: url,
      method: 'GET',
      success: (res) => {
        if (res.data && (res.data.code === 0 || res.data.code === 200)) {
          const data = res.data.data || {}
          const balance = parseFloat(data.balance || data.available_balance || 0)
          this.setData({
            balance: balance.toFixed(2)
          })
        }
      },
      fail: (err) => {
        console.log('balance fetch error:', err)
      }
    })
  },

  // 获取订单数量
  fetchOrderCount() {
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    let url = 'https://locker.cqdyxl.com/api/user/orders?'
    if (phone) url += 'phone=' + encodeURIComponent(phone)
    if (openid) url += '&openid=' + encodeURIComponent(openid)

    wx.request({
      url: url,
      method: 'GET',
      success: (res) => {
        if (res.data && (res.data.code === 0 || res.data.code === 200)) {
          const orders = res.data.data || []
          let active = 0
          if (Array.isArray(orders)) {
            orders.forEach(o => {
              if (o.status == 2) active++
            })
          }
          if (active > 0) {
            this.setData({ orderCountText: active + '个进行中' })
          }
        }
      },
      fail: (err) => {
        console.log('orders fetch error:', err)
      }
    })
  },

  // 返回上一页
  goBack() {
    wx.navigateBack({ delta: 1 })
  },

  // 跳转到我的订单 - 新页面
  onMyOrders() {
    wx.navigateTo({ url: '/pages/orders/orders' })
  },

  // 跳转到钱包页面 - 独立页面
  showBalancePage() {
    wx.navigateTo({ url: '/pages/wallet/wallet' })
  },

  // 从余额页返回主页
  backToMain() {
    this.setData({ currentPage: 'main' })
  },

  // 从规则页返回余额页
  backToBalance() {
    this.setData({ currentPage: 'balance' })
  },

  // 显示投诉弹窗 -> 跳转到投诉页面
  showComplaintModal() {
    wx.navigateTo({ url: '/pages/complaint/complaint' })
  },

  // 关闭投诉弹窗
  closeComplaintModal() {
    this.setData({ showComplaint: false, complaintText: '' })
  },

  // 投诉输入
  onComplaintInput(e) {
    this.setData({ complaintText: e.detail.value })
  },

  // 提交投诉
  submitComplaint() {
    const text = this.data.complaintText
    if (!text || !text.trim()) {
      wx.showToast({ title: '请输入投诉内容', icon: 'none' })
      return
    }
    const phone = wx.getStorageSync('phone') || ''
    const openid = wx.getStorageSync('openid') || ''
    wx.request({
      url: 'https://locker.cqdyxl.com/api/complaint/submit',
      method: 'POST',
      header: { 'Content-Type': 'application/json' },
      data: {
        phone: phone,
        openid: openid,
        content: text
      },
      success: (res) => {
        if (res.data && (res.data.code === 0 || res.data.code === 200)) {
          wx.showToast({ title: '提交成功', icon: 'success' })
          this.closeComplaintModal()
        } else {
          wx.showToast({ title: res.data.message || '提交失败', icon: 'none' })
        }
      },
      fail: () => {
        wx.showToast({ title: '网络错误', icon: 'none' })
      }
    })
  },

  // 联系客服 -> 跳转到客服中心页面
  callService() {
    wx.navigateTo({ url: '/pages/customer-service/customer-service' })
  },

  // 显示提现规则
  showRulesPage() {
    this.setData({ currentPage: 'rules' })
  },

  // 提现
  handleWithdraw() {
    wx.navigateTo({ url: '/pages/withdraw/withdraw' })
  },

  // 交易明细 -> 新页面
  showTransactions() {
    wx.navigateTo({ url: '/pages/transactions/transactions' })
  },

  // 提现记录 -> 新页面
  showWithdrawHistory() {
    wx.navigateTo({ url: '/pages/withdraw-record/withdraw-record' })
  }
})
