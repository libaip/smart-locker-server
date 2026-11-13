const API = require('../../utils/api.js');
Page({
  data: {
    statusBarHeight: 20,
    balance: '0',
    inputAmount: '',
    canSubmit: false,
    withdrawalRules: ''
  },
  onLoad() {
    const sysInfo = wx.getSystemInfoSync();
    this.setData({ statusBarHeight: sysInfo.statusBarHeight || 20 });
    this.fetchBalance();
    this.fetchRules();
  },
  onShow() { this.fetchBalance(); },
  fetchBalance() {
    const phone = wx.getStorageSync('phone') || '';
    const openid = wx.getStorageSync('openid') || '';
    API.getBalance(phone, openid).then(res => {
      const data = res.data || {};
      const balance = parseFloat(data.balance || data.available_balance || 0);
      this.setData({ balance: balance.toFixed(2) });
    }).catch(() => {});
  },
  fetchRules() {
    const phone = wx.getStorageSync('phone') || '';
    const openid = wx.getStorageSync('openid') || '';
    let url = 'https://locker.cqdyxl.com/api/user/info?';
    if (phone) url += 'phone=' + encodeURIComponent(phone);
    if (openid) url += '&openid=' + encodeURIComponent(openid);
    wx.request({
      url, method: 'GET',
      success: (res) => {
        if (res.data && (res.data.code === 0 || res.data.code === 200)) {
          this.setData({ withdrawalRules: (res.data.data || {}).withdrawal_rules || '' });
        }
      }
    });
  },
  onInput(e) {
    const val = e.detail.value;
    const num = parseFloat(val);
    const balance = parseFloat(this.data.balance);
    this.setData({
      inputAmount: val,
      canSubmit: !isNaN(num) && num > 0 && num <= balance
    });
  },
  setAll() {
    const balance = this.data.balance;
    this.setData({
      inputAmount: balance,
      canSubmit: parseFloat(balance) > 0
    });
  },
  doWithdraw() {
    if (!this.data.canSubmit) return;
    const amount = parseFloat(this.data.inputAmount);
    if (amount < 5 || amount > 200) {
      wx.showToast({ title: '单笔提现金额范围5-200元', icon: 'none' });
      return;
    }
    const phone = wx.getStorageSync('phone') || '';
    const openid = wx.getStorageSync('openid') || '';
    wx.showModal({
      title: '确认提现',
      content: '确认提现 ¥' + amount + ' ？',
      success: (res) => {
        if (res.confirm) {
          wx.request({
            url: 'https://locker.cqdyxl.com/api/user/withdraw',
            method: 'POST',
            header: { 'Content-Type': 'application/json' },
            data: { phone, openid, amount },
            success: (res) => {
              if (res.data && (res.data.code === 0 || res.data.code === 200)) {
                wx.showToast({ title: '提现成功', icon: 'success' });
                setTimeout(() => { wx.navigateBack(); }, 1500);
              } else {
                wx.showToast({ title: (res.data && res.data.message) || '提现失败', icon: 'none' });
              }
            },
            fail: () => { wx.showToast({ title: '网络错误', icon: 'none' }); }
          });
        }
      }
    });
  },
  showRule() {
    const rules = this.data.withdrawalRules || '单笔提现金额范围5-200元\n提现将在1-3个工作日内到账\n如有疑问请联系客服';
    wx.showModal({ title: '提现规则', content: rules, showCancel: false });
  },
  goBack() {
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/wallet/wallet' }); } });
  }
});
