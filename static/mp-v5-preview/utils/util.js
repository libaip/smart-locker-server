// utils/util.js - 工具函数

/**
 * 格式化时间
 * @param {string|number|Date} time - 时间戳或日期字符串
 * @param {string} format - 格式化模板，默认 'YYYY-MM-DD HH:mm:ss'
 */
function formatTime(time, format = 'YYYY-MM-DD HH:mm:ss') {
  if (!time) return '--'
  
  const date = time instanceof Date ? time : new Date(time)
  
  if (isNaN(date.getTime())) return '--'
  
  const year = date.getFullYear()
  const month = (date.getMonth() + 1).toString().padStart(2, '0')
  const day = date.getDate().toString().padStart(2, '0')
  const hour = date.getHours().toString().padStart(2, '0')
  const minute = date.getMinutes().toString().padStart(2, '0')
  const second = date.getSeconds().toString().padStart(2, '0')
  
  return format
    .replace('YYYY', year)
    .replace('MM', month)
    .replace('DD', day)
    .replace('HH', hour)
    .replace('mm', minute)
    .replace('ss', second)
}

/**
 * 格式化日期（简短）
 * @param {string|number|Date} time - 时间戳或日期字符串
 */
function formatDateShort(time) {
  if (!time) return '--'
  const date = time instanceof Date ? time : new Date(time)
  if (isNaN(date.getTime())) return '--'
  
  const month = (date.getMonth() + 1).toString().padStart(2, '0')
  const day = date.getDate().toString().padStart(2, '0')
  const hour = date.getHours().toString().padStart(2, '0')
  const minute = date.getMinutes().toString().padStart(2, '0')
  return `${month}-${day} ${hour}:${minute}`
}

/**
 * 格式化日期（完整）
 * @param {string|number|Date} time - 时间戳或日期字符串
 */
function formatDateFull(time) {
  if (!time) return '--'
  const date = time instanceof Date ? time : new Date(time)
  if (isNaN(date.getTime())) return '--'
  
  const year = date.getFullYear()
  const month = (date.getMonth() + 1).toString().padStart(2, '0')
  const day = date.getDate().toString().padStart(2, '0')
  const hour = date.getHours().toString().padStart(2, '0')
  const minute = date.getMinutes().toString().padStart(2, '0')
  const second = date.getSeconds().toString().padStart(2, '0')
  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

/**
 * 验证手机号格式
 * @param {string} phone - 手机号
 */
function validatePhone(phone) {
  return /^1[3-9]\d{9}$/.test(phone)
}

/**
 * 验证取物码（4位数字）
 * @param {string} code - 取物码
 */
function validateAccessCode(code) {
  return /^\d{4}$/.test(code)
}

/**
 * 隐藏手机号中间4位
 * @param {string} phone - 手机号
 */
function maskPhone(phone) {
  if (!phone || phone.length !== 11) return phone
  return phone.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2')
}

/**
 * 隐藏取物码
 * @param {string} code - 取物码
 */
function maskCode(code) {
  if (!code || code.length !== 4) return code
  return code.replace(/(\d{2})\d{2}/, '$1**')
}

/**
 * 防抖函数
 * @param {function} func - 要执行的函数
 * @param {number} wait - 等待时间（毫秒）
 */
function debounce(func, wait = 500) {
  let timeout
  return function(...args) {
    clearTimeout(timeout)
    timeout = setTimeout(() => func.apply(this, args), wait)
  }
}

/**
 * 验证柜格尺寸
 * @param {string} size - 尺寸标识
 */
function validateSlotSize(size) {
  return ['S', 'M', 'L'].includes(size)
}

/**
 * 获取柜格尺寸中文名
 * @param {string} size - 尺寸标识
 */
function getSlotSizeName(size) {
  const map = {
    'S': '小柜',
    'M': '中柜',
    'L': '大柜'
  }
  return map[size] || size
}

/**
 * 获取柜格尺寸说明
 * @param {string} size - 尺寸标识
 */
function getSlotSizeDesc(size) {
  const map = {
    'S': '存放手机、钱包等小件物品',
    'M': '存放背包、手提包等中小件',
    'L': '存放行李箱、大型背包等'
  }
  return map[size] || ''
}

/**
 * 倒计时格式化（秒转为 mm:ss）
 * @param {number} seconds - 秒数
 */
function formatCountdown(seconds) {
  if (!seconds || seconds <= 0) return '00:00'
  const min = Math.floor(seconds / 60).toString().padStart(2, '0')
  const sec = (seconds % 60).toString().padStart(2, '0')
  return `${min}:${sec}`
}

module.exports = {
  formatTime,
  formatDateShort,
  formatDateFull,
  validatePhone,
  validateAccessCode,
  maskPhone,
  maskCode,
  debounce,
  validateSlotSize,
  getSlotSizeName,
  getSlotSizeDesc,
  formatCountdown
}
