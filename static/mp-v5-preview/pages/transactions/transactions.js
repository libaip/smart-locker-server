const { getTransactions } = require('../../utils/api');
Page({
  data: { list: [], filter: 'all' },
  onLoad() { this.loadData(); },
  onPullDownRefresh() { this.loadData(); },
  switchTab(e) {
    const tab = e.currentTarget.dataset.tab;
    this.setData({ filter: tab }, () => this.loadData());
  },
  async loadData() {
    try {
      const phone = wx.getStorageSync('phone') || '';
      const openid = wx.getStorageSync('openid') || '';
      const res = await getTransactions(phone, openid, { type: this.data.filter });
      let list = [];
      if (res && res.data) {
        const raw = res.data.list || (Array.isArray(res.data) ? res.data : []);
        list = raw.map(t => ({
          ...t,
          isWithdraw: (t.remark && t.remark.indexOf('提现') >= 0) || t.amount < 0
        }));
      }
      this.setData({ list });
    } catch (e) { console.error('loadData err', e); }
    wx.stopPullDownRefresh();
  },
  goBack() { wx.navigateBack({ fail: () => { wx.switchTab({ url: '/pages/wallet/wallet' }); } }); }
});
