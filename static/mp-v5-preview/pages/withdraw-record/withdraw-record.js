const { getWithdrawals } = require('../../utils/api');
Page({
  data: { list: [] },
  onLoad() { this.loadData(); },
  onPullDownRefresh() { this.loadData(); },
  async loadData() {
    try {
      const openid = wx.getStorageSync('openid');
      const phone = wx.getStorageSync('phone') || '';
      const res = await getWithdrawals(phone, openid);
      let list = [];
      if (res && res.data) {
        const raw = Array.isArray(res.data) ? res.data : (res.data.list || []);
        list = raw.map(w => ({
          ...w,
          statusText: w.status === 2 ? '已到账' : w.status === 3 ? '审核驳回' : w.status === 1 ? '审核通过' : '审核中',
          statusClass: w.status === 2 ? 'status-done' : w.status === 3 ? 'status-fail' : 'status-pending'
        }));
      }
      this.setData({ list });
    } catch (e) { console.error('loadData err', e); }
    wx.stopPullDownRefresh();
  },
  goBack() { wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/mine/mine' }) }); },
  copyOrderNo(e) {
    const no = e.currentTarget.dataset.no;
    wx.setClipboardData({ data: String(no), success: () => wx.showToast({ title: '已复制', icon: 'success' }) });
  },
  goFaq() { wx.showToast({ title: '请联系客服', icon: 'none' }); }
});
