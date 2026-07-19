// pages/order-detail/order-detail.js - 订单详情页面
const API = require('../../utils/api.js')
const util = require('../../utils/util.js')

Page({
  data: {
    orderId: '',
    orderInfo: {},
    loading: true,
    // 提现相关
    withdrawEnabled: false,
    isWithdrawing: false,
    withdrawStatus: '' // pending-审核中, approved-已批准, auto_refund-自动退款
  },

  onLoad(options) {
    if (options.orderId) {
      this.setData({ orderId: options.orderId })
      this.loadOrderDetail()
    } else {
      wx.showToast({
        title: '订单信息不存在',
        icon: 'none'
      })
      setTimeout(() => {
        wx.navigateBack()
      }, 1500)
    }
  },

  // 加载订单详情
  loadOrderDetail() {
    this.setData({ loading: true })
    
    API.getOrderDetail(this.data.orderId)
      .then(res => {
        const orderInfo = this.formatOrder(res.data)
        this.setData({
          orderInfo: orderInfo,
          loading: false,
          withdrawEnabled: res.data && res.data.withdraw_enabled === true,
          withdrawStatus: res.data && res.data.withdraw_status ? res.data.withdraw_status : ''
        })
      })
      .catch(err => {
        console.error('获取订单详情失败:', err)
        this.setData({ loading: false })
        wx.showToast({
          title: '获取订单详情失败',
          icon: 'none'
        })
      })
  },

  // 格式化订单数据
  formatOrder(data) {
    if (!data) return {}
    
    const statusMap = {
      'storing': { text: '在存', class: 'storing' },
      'retrieved': { text: '已取', class: 'retrieved' },
      'timeout': { text: '已超时', class: 'timeout' },
      'withdrawal_pending': { text: '审核中', class: 'pending' },
      'withdrawal_approved': { text: '退款中', class: 'pending' }
    }
    
    const status = data.status || 'storing'
    const statusInfo = statusMap[status] || statusMap.storing
    
    return {
      order_id: data.order_id || data.id,
      cabinet_name: data.cabinet_name || data.location || '寄存柜',
      cabinet_id: data.cabinet_id,
      slot_id: data.slot_id,
      slot_no: data.slot_no || data.slot_id,
      slot_size: data.slot_size,
      slot_size_name: util.getSlotSizeName(data.slot_size),
      deposit_time: data.deposit_time ? util.formatDateFull(data.deposit_time) : '',
      retrieve_time: data.retrieve_time ? util.formatDateFull(data.retrieve_time) : '',
      end_time: data.end_time ? util.formatDateFull(data.end_time) : '',
      status: status,
      status_text: statusInfo.text,
      status_class: statusInfo.class,
      phone: data.phone,
      masked_phone: data.phone ? util.maskPhone(data.phone) : '',
      access_code: data.access_code,
      deposit: data.deposit || 10,
      fee: data.fee || 0,
      refund_amount: data.refund_amount || 0,
      order_no: data.order_no || data.order_id || ''
    }
  },

  // 申请提现退款
  onApplyWithdrawal() {
    const { orderInfo, isWithdrawing } = this.data
    
    if (isWithdrawing) return
    
    wx.showModal({
      title: '申请退款',
      content: '确定要申请退还保证金吗？',
      confirmText: '确认申请',
      success: (res) => {
        if (res.confirm) {
          this.doApplyWithdrawal()
        }
      }
    })
  },

  // 执行提现退款
  doApplyWithdrawal() {
    const { orderInfo } = this.data
    
    this.setData({ isWithdrawing: true })
    
    API.applyWithdrawal(orderInfo.order_id, orderInfo.phone)
      .then(res => {
        this.setData({ isWithdrawing: false })
        
        const status = res.data && res.data.status
        let message = ''
        
        if (status === 'approved' || status === 'auto_refund') {
          message = '退款成功，押金已返回'
          this.setData({
            withdrawStatus: status,
            'orderInfo.status': 'withdrawal_approved',
            'orderInfo.status_text': status === 'auto_refund' ? '已退款' : '退款中',
            'orderInfo.status_class': 'pending'
          })
        } else if (status === 'pending') {
          message = '申请已提交，审核中'
          this.setData({
            withdrawStatus: 'pending',
            'orderInfo.status': 'withdrawal_pending',
            'orderInfo.status_text': '审核中',
            'orderInfo.status_class': 'pending'
          })
        } else {
          message = res.data && res.data.message || '申请已提交'
        }
        
        wx.showToast({
          title: message,
          icon: 'success'
        })
      })
      .catch(err => {
        this.setData({ isWithdrawing: false })
        console.error('申请退款失败:', err)
      })
  },

  // 跳转到投诉页面
  onComplaint() {
    const { orderInfo } = this.data
    wx.navigateTo({
      url: `/pages/complaint/complaint?orderNo=${orderInfo.order_no || orderInfo.order_id}`
    })
  },

  // 继续寄存
  onContinueStorage() {
    const { orderInfo } = this.data
    
    wx.showLoading({ title: '处理中...' })
    
    API.continueStorage(orderInfo.order_id)
      .then(res => {
        wx.hideLoading()
        wx.showToast({
          title: '继续寄存成功',
          icon: 'success'
        })
        setTimeout(() => {
          wx.navigateBack()
        }, 1500)
      })
      .catch(err => {
        wx.hideLoading()
        console.error('继续寄存失败:', err)
        wx.showToast({
          title: err.message || '操作失败',
          icon: 'none'
        })
      })
  },

  // 重新开门
  onOpenDoor() {
    const { orderInfo } = this.data
    
    wx.showLoading({ title: '开门中...' })
    
    API.openDoor(orderInfo.cabinet_id, orderInfo.slot_id)
      .then(res => {
        wx.hideLoading()
        wx.showToast({
          title: '柜门已打开',
          icon: 'success'
        })
      })
      .catch(err => {
        wx.hideLoading()
        wx.showToast({
          title: err.message || '开门失败',
          icon: 'none'
        })
      })
  }
})
