// utils/api.js - 用户端小程序API封装

const BASE_URL = 'https://locker.cqdyxl.com/api'

function request(options) {
  var url = options.url
  var method = options.method || 'GET'
  var data = options.data || {}
  var header = options.header || {}
  return new Promise(function(resolve, reject) {
    wx.request({
      url: BASE_URL + url,
      method: method,
      data: data,
      header: Object.assign({ 'Content-Type': 'application/json' }, header),
      timeout: 15000,
      success: function(res) {
        if (res.data && (res.data.code === 0 || res.data.code === 200)) {
          resolve(res.data)
        } else {
          reject({ code: res.data ? res.data.code : -1, message: res.data ? res.data.message : '请求失败' })
        }
      },
      fail: function(err) {
        reject({ code: -1, message: '网络请求失败' })
      }
    })
  })
}

function get(url, data) { return request({ url: url, method: 'GET', data: data }) }
function post(url, data) { return request({ url: url, method: 'POST', data: data }) }

// 微信登录
function wxLogin(code) { return post('/wx/login', { code: code }) }

// 解密手机号
function decryptPhone(encryptedData, iv, sessionKey) { return post('/wx/phone', { encrypted_data: encryptedData, iv: iv, session_key: sessionKey }) }

// 一步登录+解密手机号：code+encryptedData+iv一起发后端
function loginPhone(code, encryptedData, iv) { return post('/wx/login-phone', { code: code, encrypted_data: encryptedData, iv: iv }) }

// 获取用户余额
function getBalance(phone, openid) { 
  var data = {}
  if (phone) data.phone = phone
  if (openid) data.openid = openid
  return get('/user/balance', data)
}

// 申请提现
function applyWithdrawal(phone, amount, openid) { 
  var data = { phone: phone, amount: amount }
  if (openid) data.openid = openid
  return post('/user/withdraw', data)
}

// 获取订单列表
function getOrders(phone, openid) { 
  var data = {}
  if (phone) data.phone = phone
  if (openid) data.openid = openid
  return get('/user/orders', data)
}

// 获取订单详情
function getOrderDetail(orderId) { return get('/order/' + orderId) }

// 创建存包订单
function createOrder(params) {
  return post('/deposit/create-order', {
    cabinet_id: params.cabinetId,
    slot_size: params.slotSize || 'M',
    phone: params.phone,
    access_code: params.accessCode,
    sms_code: params.smsCode || '',
    openid: params.openid || ''
  })
}

// 取物验证
function retrieveVerify(phone, accessCode, cabinetId, openid) {
  var data = { phone: phone, access_code: accessCode }
  if (cabinetId) data.cabinet_id = cabinetId
  if (openid) data.openid = openid
  return post('/deposit/retrieve', data)
}

// 继续寄存
function continueStorage(orderId) { return post('/deposit/continue-storage', { order_id: orderId }) }

// 结束取物
function endStorage(orderId) { return post('/deposit/end-storage', { order_id: orderId }) }

// 获取柜体信息(通过二维码参数)
function getCabinetInfo(cabinetId) { return get('/cabinets/' + cabinetId) }

// 获取柜体信息(通过主板ID)
function getCabinetByMainboard(deviceId) { return get('/cabinets/by-mainboard/' + deviceId) }

// 获取柜格列表
function getSlots(cabinetId) { return get('/cabinets/' + cabinetId + '/slots') }

// 获取柜体信息(通过group_code)
function getCabinetByGroupCode(groupCode) { return get('/cabinets/by-group/' + groupCode) }

// 远程开锁
function openDoor(cabinetId, slotId) { return post('/cabinets/' + cabinetId + '/slots/' + slotId + '/open') }

// 支付处理
function handlePay(orderId) { return post('/order/' + orderId + '/pay') }

// 查询支付状态
function getPayStatus(orderId) { return get('/order/' + orderId + '/pay-status') }

// 提交投诉
function submitComplaint(params) {
  var data = {
    type: params.type || 'self',
    content: params.content,
    order_no: params.orderNo || '',
    user_phone: params.userPhone
  }
  if (params.openid) data.openid = params.openid
  return post('/complaints', data)
}

// 获取投诉记录
function getComplaintList(phone, openid) { 
  var data = {}
  if (phone) data.user_phone = phone
  if (openid) data.openid = openid
  return get('/complaints', data)
}

// 获取提现记录
function getWithdrawals(phone, openid) {
  var data = {}
  if (phone) data.phone = phone
  if (openid) data.openid = openid
  return get('/user/wallet/withdrawals', data)
}

// 获取交易明细
function getTransactions(phone, openid, params) {
  var data = { type: 'all', page: 1, limit: 50 }
  if (phone) data.phone = phone
  if (openid) data.openid = openid
  if (params) Object.assign(data, params)
  return get('/user/wallet/transactions', data)
}

// 中途开门取物
function midRetrieve(params) {
  return post('/deposit/mid-retrieve', {
    order_id: params.orderId,
    cabinet_code: params.cabinetCode,
    access_code: params.accessCode,
    phone: params.phone || ''
  })
}

// 获取用户信息
function getUserInfo(phone, openid) {
  var data = {}
  if (phone) data.phone = phone
  if (openid) data.openid = openid
  return get('/user/info', data)
}

module.exports = {
  BASE_URL: BASE_URL,
  request: request, get: get, post: post,
  wxLogin: wxLogin,
  decryptPhone: decryptPhone,
  loginPhone: loginPhone,
  getBalance: getBalance, applyWithdrawal: applyWithdrawal,
  getOrders: getOrders, getOrderDetail: getOrderDetail,
  createOrder: createOrder,
  retrieveVerify: retrieveVerify,
  continueStorage: continueStorage, endStorage: endStorage,
  getCabinetInfo: getCabinetInfo,
  getCabinetByMainboard: getCabinetByMainboard,
  getCabinetByGroupCode: getCabinetByGroupCode,
  getSlots: getSlots,
  openDoor: openDoor,
  handlePay: handlePay, getPayStatus: getPayStatus,
  submitComplaint: submitComplaint,
  getComplaintList: getComplaintList,
  getWithdrawals: getWithdrawals,
  getTransactions: getTransactions,
  midRetrieve: midRetrieve,
  getUserInfo: getUserInfo
}
