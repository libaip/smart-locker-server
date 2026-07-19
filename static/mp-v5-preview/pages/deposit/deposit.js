// pages/deposit/deposit.js - 存包页面逻辑
const API = require('../../utils/api.js')
const util = require('../../utils/util.js')

// 保证金金额配置（可从后端获取）
const DEPOSIT_AMOUNT = 10.00

Page({
  data: {
    currentStep: 1,
    
    // 柜体信息
    cabinetId: '',
    cabinetInfo: {
      name: '寄存柜',
      location: '位置信息'
    },
    
    // 柜格信息
    slots: {
      S: 0,
      M: 0,
      L: 0
    },
    selectedSlot: '',
    slotSizeName: '',
    
    // 表单信息
    phone: '',
    openid: '',
    accessCode: '',
    smsCode: '',
    needSmsCode: false,
    smsCountdown: 0,
    smsTimer: null,
    
    // 订单信息
    orderId: '',
    orderInfo: {},
    depositAmount: DEPOSIT_AMOUNT,
    maskedPhone: '',
    maskedCode: '',
    countdown: 60
  },

  countdownTimer: null,

  onLoad(options) {
    const cabinetId = options.cabinet_id || ''
    const openid = options.openid || wx.getStorageSync('openid') || ''
    this.setData({ cabinetId, openid })
    
    if (cabinetId) {
      this.loadCabinetInfo(cabinetId)
    } else {
      wx.showToast({
        title: '缺少柜体信息',
        icon: 'none'
      })
      setTimeout(() => {
        wx.navigateBack()
      }, 1500)
    }
  },

  onUnload() {
    // 清除定时器
    if (this.data.smsTimer) {
      clearInterval(this.data.smsTimer)
    }
    if (this.countdownTimer) {
      clearInterval(this.countdownTimer)
    }
  },

  // 加载柜体信息
  loadCabinetInfo(cabinetId) {
    wx.showLoading({ title: '加载中...' })
    
    API.getCabinetByGroupCode(cabinetId)
      .then(res => {
        wx.hideLoading()
        if (res.data) {
          this.setData({
            cabinetInfo: {
              name: res.data.name || res.data.cabinet_name || '寄存柜',
              location: res.data.location || res.data.address || '位置信息',
              deposit: res.data.deposit || DEPOSIT_AMOUNT
            },
            depositAmount: res.data.deposit || DEPOSIT_AMOUNT
          })
        }
        // 无论是否有数据都加载柜格
        return this.loadSlots(cabinetId)
      })
      .catch(err => {
        wx.hideLoading()
        console.error('加载柜体信息失败:', err)
        // 继续加载柜格
        return this.loadSlots(cabinetId)
      })
  },

  // 加载柜格信息
  loadSlots(cabinetId) {
    return API.getSlots(cabinetId)
      .then(res => {
        if (res.data) {
          // 格式化柜格数据
          const slots = {
            S: 0,
            M: 0,
            L: 0
          }
          
          if (Array.isArray(res.data)) {
            res.data.forEach(slot => {
              if (slot.size && slot.status === 'available') {
                slots[slot.size] = (slots[slot.size] || 0) + 1
              }
            })
          } else if (res.data.slots) {
            res.data.slots.forEach(slot => {
              if (slot.size && slot.status === 'available') {
                slots[slot.size] = (slots[slot.size] || 0) + 1
              }
            })
          }
          
          this.setData({ slots })
        }
      })
      .catch(err => {
        console.error('加载柜格信息失败:', err)
        // 使用默认空数据
        this.setData({
          slots: { S: 0, M: 0, L: 0 }
        })
      })
  },

  // 选择柜格
  onSelectSlot(e) {
    const size = e.currentTarget.dataset.size
    const { slots } = this.data
    
    // 检查该尺寸是否有可用柜格
    if (slots[size] <= 0) {
      wx.showToast({
        title: '该尺寸柜格已满，请选择其他尺寸',
        icon: 'none'
      })
      return
    }
    
    this.setData({
      selectedSlot: size,
      slotSizeName: util.getSlotSizeName(size)
    })
  },

  // 步骤1下一步
  onNextStep1() {
    if (!this.data.selectedSlot) {
      wx.showToast({
        title: '请选择柜格大小',
        icon: 'none'
      })
      return
    }
    this.setData({ currentStep: 2 })
  },

  // 手机号输入
  onPhoneInput(e) {
    this.setData({ phone: e.detail.value })
  },

  // 取物码输入
  onCodeInput(e) {
    this.setData({ accessCode: e.detail.value })
  },

  // 短信验证码输入
  onSmsCodeInput(e) {
    this.setData({ smsCode: e.detail.value })
  },

  // 发送短信验证码
  onSendSms() {
    const { phone } = this.data
    
    if (!util.validatePhone(phone)) {
      wx.showToast({
        title: '请输入正确的手机号',
        icon: 'none'
      })
      return
    }
    
    API.sendSms(phone)
      .then(res => {
        wx.showToast({
          title: '验证码已发送',
          icon: 'success'
        })
        this.startSmsCountdown()
      })
      .catch(err => {
        console.error('发送验证码失败:', err)
      })
  },

  // 短信倒计时
  startSmsCountdown() {
    this.setData({ smsCountdown: 60 })
    
    const timer = setInterval(() => {
      const countdown = this.data.smsCountdown - 1
      if (countdown <= 0) {
        clearInterval(timer)
        this.setData({ smsCountdown: 0 })
      } else {
        this.setData({ smsCountdown: countdown })
      }
    }, 1000)
    
    this.setData({ smsTimer: timer })
  },

  // 步骤2下一步
  onNextStep2() {
    const { phone, accessCode, smsCode, needSmsCode } = this.data
    
    // 验证手机号
    if (!util.validatePhone(phone)) {
      wx.showToast({
        title: '请输入正确的手机号',
        icon: 'none'
      })
      return
    }
    
    // 验证取物码
    if (!util.validateAccessCode(accessCode)) {
      wx.showToast({
        title: '请输入4位取物码',
        icon: 'none'
      })
      return
    }
    
    // 验证短信验证码（如需要）
    if (needSmsCode && !smsCode) {
      wx.showToast({
        title: '请输入短信验证码',
        icon: 'none'
      })
      return
    }
    
    // 创建订单
    this.createOrder()
  },

  // 创建订单
  createOrder() {
    const { cabinetId, selectedSlot, phone, accessCode, smsCode } = this.data
    
    wx.showLoading({ title: '创建订单...' })
    
    API.createOrder({
      cabinetId,
      slotSize: selectedSlot,
      phone,
      accessCode,
      smsCode,
      openid: this.data.openid
    })
      .then(res => {
        wx.hideLoading()
        if (res.data && res.data.order_id) {
          this.setData({
            orderId: res.data.order_id,
            compartmentNumber: res.data.compartment_number || '' ,
            compartmentLabel: res.data.compartment_label || '',
            maskedPhone: util.maskPhone(phone),
            maskedCode: util.maskCode(accessCode)
          })
          this.setData({ currentStep: 3 })
        } else {
          wx.showToast({
            title: res.message || '创建订单失败',
            icon: 'none'
          })
        }
      })
      .catch(err => {
        wx.hideLoading()
        console.error('创建订单失败:', err)
      })
  },

  // 上一步
  onPrevStep() {
    const { currentStep } = this.data
    if (currentStep > 1) {
      this.setData({ currentStep: currentStep - 1 })
    }
  },

  // 支付
  onPay() {
    const { orderId } = this.data
    
    if (!orderId) {
      wx.showToast({
        title: '订单信息不存在',
        icon: 'none'
      })
      return
    }
    
    wx.showLoading({ title: '支付中...' })
    
    API.handlePay(orderId)
      .then(res => {
        wx.hideLoading()
        // 支付成功后进入完成步骤
        this.setData({ 
          currentStep: 4,
          orderInfo: {
            order_id: orderId,
            access_code: this.data.accessCode,
            slot_no: this.data.compartmentLabel || (this.data.compartmentNumber ? this.data.compartmentNumber + '号' : '--')
          }
        })
        this.startCountdown()
      })
      .catch(err => {
        wx.hideLoading()
        console.error('支付失败:', err)
        wx.showToast({
          title: err.message || '支付失败',
          icon: 'none'
        })
      })
  },

  // 开始倒计时
  startCountdown() {
    let countdown = this.data.countdown
    this.countdownTimer = setInterval(() => {
      countdown--
      if (countdown <= 0) {
        clearInterval(this.countdownTimer)
        this.setData({ countdown: 0 })
      } else {
        this.setData({ countdown })
      }
    }, 1000)
  },

  // 开门放物
  onOpenDoor() {
    const { cabinetId, orderId } = this.data
    
    wx.showLoading({ title: '开门中...' })
    
    API.openDoor(cabinetId, orderId)
      .then(res => {
        wx.hideLoading()
        wx.showToast({
          title: '柜门已打开，请放入物品',
          icon: 'success'
        })
      })
      .catch(err => {
        wx.hideLoading()
        console.error('开门失败:', err)
        wx.showToast({
          title: err.message || '开门失败',
          icon: 'none'
        })
      })
  },

  // 完成
  onDone() {
    wx.navigateBack()
  }
})
