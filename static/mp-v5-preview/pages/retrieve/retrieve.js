// pages/retrieve/retrieve.js - 取物页面逻辑
const API = require('../../utils/api.js')
const util = require('../../utils/util.js')

Page({
  data: {
    currentStep: 'verify',
    
    // 表单数据
    phone: '',
    code: '',
    canVerify: false,
    openid: '',
    
    // 订单信息
    orderInfo: {},
    maskedPhone: '',
    
    // 结果信息
    resultType: '', // continue-继续寄存, end-取物结束
    refundAmount: 0
  },

  onLoad(options) {
    if (options.phone) {
      this.setData({ phone: options.phone })
    }
    if (options.code) {
      this.setData({ code: options.code })
      this.updateCanVerify()
    }
    if (options.openid) {
      this.setData({ openid: options.openid })
    } else {
      this.setData({ openid: wx.getStorageSync('openid') || '' })
    }
  },

  // 手机号输入
  onPhoneInput(e) {
    this.setData({ phone: e.detail.value })
    this.updateCanVerify()
  },

  // 取物码输入
  onCodeInput(e) {
    this.setData({ code: e.detail.value })
    this.updateCanVerify()
  },

  // 更新验证按钮状态
  updateCanVerify() {
    const { phone, code } = this.data
    const canVerify = util.validatePhone(phone) && util.validateAccessCode(code)
    this.setData({ canVerify })
  },

  // 验证取物
  onVerify() {
    const { phone, code, openid } = this.data
    
    // 前端验证
    if (!util.validatePhone(phone)) {
      wx.showToast({
        title: '请输入正确的手机号',
        icon: 'none'
      })
      return
    }
    
    if (!util.validateAccessCode(code)) {
      wx.showToast({
        title: '请输入4位取物码',
        icon: 'none'
      })
      return
    }
    
    wx.showLoading({ title: '验证中...' })
    
    API.retrieveVerify(phone, code, undefined, openid)
      .then(res => {
        wx.hideLoading()
        if (res.data) {
          this.setData({
            currentStep: 'action',
            orderInfo: res.data,
            maskedPhone: util.maskPhone(phone)
          })
        } else {
          wx.showToast({
            title: res.message || '验证失败',
            icon: 'none'
          })
        }
      })
      .catch(err => {
        wx.hideLoading()
        console.error('验证失败:', err)
        wx.showToast({
          title: err.message || '验证失败，请检查手机号和取物码',
          icon: 'none'
        })
      })
  },

  // 开门取物
  onOpenDoor() {
    const { orderInfo } = this.data
    
    this.setData({ currentStep: 'loading' })
    
    API.openDoor(orderInfo.cabinet_id, orderInfo.slot_id || orderInfo.order_id)
      .then(res => {
        wx.showToast({
          title: '柜门已打开，请取走物品',
          icon: 'success'
        })
        // 短暂延迟后显示操作选择
        setTimeout(() => {
          this.setData({ currentStep: 'action' })
        }, 1000)
      })
      .catch(err => {
        this.setData({ currentStep: 'action' })
        console.error('开门失败:', err)
        wx.showToast({
          title: err.message || '开门失败，请重试',
          icon: 'none'
        })
      })
  },

  // 继续寄存
  onContinueStorage() {
    const { orderInfo } = this.data
    
    wx.showLoading({ title: '处理中...' })
    
    API.continueStorage(orderInfo.order_id)
      .then(res => {
        wx.hideLoading()
        this.setData({
          currentStep: 'result',
          resultType: 'continue'
        })
      })
      .catch(err => {
        wx.hideLoading()
        console.error('继续寄存失败:', err)
        wx.showToast({
          title: err.message || '操作失败，请重试',
          icon: 'none'
        })
      })
  },

  // 取物结束
  onEndStorage() {
    const { orderInfo } = this.data
    
    wx.showModal({
      title: '确认取物结束',
      content: '取物后保证金将退还，是否确认？',
      confirmText: '确认取物',
      cancelText: '取消',
      success: (res) => {
        if (res.confirm) {
          this.doEndStorage()
        }
      }
    })
  },

  // 执行取物结束
  doEndStorage() {
    const { orderInfo } = this.data
    
    wx.showLoading({ title: '处理中...' })
    
    API.endStorage(orderInfo.order_id)
      .then(res => {
        wx.hideLoading()
        this.setData({
          currentStep: 'result',
          resultType: 'end',
          refundAmount: res.data && res.data.refund_amount ? res.data.refund_amount : 10
        })
      })
      .catch(err => {
        wx.hideLoading()
        console.error('取物结束失败:', err)
        wx.showToast({
          title: err.message || '操作失败，请重试',
          icon: 'none'
        })
      })
  },

  // 返回首页
  onBackHome() {
    wx.navigateTo({
      url: '/pages/index/index'
    })
  },

  // 跳转到投诉页面
  onComplaint() {
    const { orderInfo } = this.data
    wx.navigateTo({
      url: `/pages/complaint/complaint?orderNo=${orderInfo.order_id || ''}`
    })
  }
})
