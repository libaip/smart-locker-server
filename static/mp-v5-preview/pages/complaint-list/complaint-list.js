// pages/complaint-list/complaint-list.js - 投诉记录页面
const API = require('../../utils/api.js')
const util = require('../../utils/util.js')

Page({
  data: {
    phone: '',
    canSearch: false,
    hasSearched: false,
    loading: false,
    complaints: []
  },

  onLoad() {
    // 读取上次查询的手机号
    const phone = wx.getStorageSync('userPhone') || ''
    this.setData({
      phone: phone,
      canSearch: util.validatePhone(phone)
    })
  },

  // 手机号输入
  onPhoneInput(e) {
    const phone = e.detail.value
    this.setData({
      phone: phone,
      canSearch: util.validatePhone(phone)
    })
  },

  // 查询投诉记录
  onSearch() {
    const { phone } = this.data
    
    if (!util.validatePhone(phone)) {
      wx.showToast({
        title: '请输入正确的手机号',
        icon: 'none'
      })
      return
    }
    
    this.setData({ loading: true, hasSearched: true })
    
    API.getComplaintList(phone)
      .then(res => {
        this.setData({ loading: false })
        
        // 保存手机号到本地
        wx.setStorageSync('userPhone', phone)
        
        if (res.data && Array.isArray(res.data)) {
          const complaints = res.data.map(item => this.formatComplaint(item))
          this.setData({ complaints })
        } else {
          this.setData({ complaints: [] })
        }
      })
      .catch(err => {
        this.setData({ loading: false })
        console.error('查询投诉记录失败:', err)
        this.setData({ complaints: [] })
      })
  },

  // 格式化投诉数据
  formatComplaint(item) {
    const typeMap = {
      'self': '使用问题',
      'device': '设备故障',
      'fee': '费用问题',
      'other': '其他'
    }
    
    const statusMap = {
      'pending': { text: '待处理', class: 'pending' },
      'processing': { text: '处理中', class: 'processing' },
      'replied': { text: '已回复', class: 'replied' },
      'closed': { text: '已关闭', class: 'closed' }
    }
    
    const type = item.type || 'self'
    const status = item.status || 'pending'
    const statusInfo = statusMap[status] || statusMap.pending
    
    return {
      id: item.id,
      type_text: typeMap[type] || '其他',
      content: item.content || '',
      content_summary: item.content ? (item.content.length > 50 ? item.content.substring(0, 50) + '...' : item.content) : '',
      order_no: item.order_no || '',
      status: status,
      status_text: statusInfo.text,
      status_class: statusInfo.class,
      reply: item.reply || '',
      create_time: item.create_time ? util.formatDateShort(item.create_time) : '',
      create_time_full: item.create_time || ''
    }
  },

  // 查看投诉详情
  onViewDetail(e) {
    const { id } = e.currentTarget.dataset
    wx.showModal({
      title: '投诉详情',
      content: '投诉ID: ' + id + '\n\n如需查看完整详情，请联系客服。',
      showCancel: false
    })
  }
})
