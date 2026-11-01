# -*- coding: utf-8 -*-
"""
S301-20260919  免押单（押金0）在线订单操作列：不再显示橙色「退款」按钮，
               改显示灰色「无预付款」标签；同时免押单也不显示灰色「已退款」按钮。
背景：老板截图确认——他看到的"还是退款的状态"就是操作列那个橙色「退款」按钮。
      表太宽，右边的「状态」列被 sticky 的操作列盖住，所以状态列的"无预付款"他看不见。
影响面：只改 static/admin-v2.html 的一个 Vue 模板片段 + 新增一个 isNoPrepay 方法。
      押金>0 的订单一律不变（近7天 status2/3 里押金>0 的有 5545 条，退款按钮必须保留）。
"""
import hashlib
import shutil
import sys

P = 'static/admin-v2.html'

src = open(P, 'rb').read()
before = hashlib.md5(src).hexdigest()
s = src.decode('utf-8')

steps = []

# ---------- 1) 新增 isNoPrepay 方法（紧跟 statusClassFor 之后） ----------
a1 = "statusClassFor:function(o){var t=this.statusTextFor(o);if(t==='无预付款')return 'gray';return this.statusClass(o?o.status:0);},"
assert s.count(a1) == 1, '锚点1 命中数=%d' % s.count(a1)
add1 = ("isNoPrepay:function(o){if(!o)return false;"
        "var _hd=(o.deposit_amount!==undefined&&o.deposit_amount!==null);"
        "return _hd&&Number(o.deposit_amount||0)<=0&&Number(o.refund_amount||0)<=0;},")
s = s.replace(a1, a1 + add1, 1)
steps.append('新增 isNoPrepay 方法')

# ---------- 2) 橙色「退款」按钮：免押单不显示 ----------
a2 = r'&&o.refund_status!==\'refunded\'" class="btn-text orange" @click="$parent.refundOrder(o)">退款</button>'
assert s.count(a2) == 1, '锚点2 命中数=%d' % s.count(a2)
n2 = r'&&o.refund_status!==\'refunded\'&&!$parent.isNoPrepay(o)" class="btn-text orange" @click="$parent.refundOrder(o)">退款</button>'
s = s.replace(a2, n2, 1)
steps.append('退款按钮加 !isNoPrepay 判断')

# ---------- 3) 灰色「已退款」按钮：免押单不显示，并在同一位置补「无预付款」标签 ----------
a3 = r'<button v-if="(o.refund_status===\'refunded\'||o.status===4)" class="btn-text" style="color:#bbb;cursor:default" disabled>已退款</button>'
assert s.count(a3) == 1, '锚点3 命中数=%d' % s.count(a3)
n3 = (r'<button v-if="(o.refund_status===\'refunded\'||o.status===4)&&!$parent.isNoPrepay(o)" '
      r'class="btn-text" style="color:#bbb;cursor:default" disabled>已退款</button>'
      r'<span v-if="$parent.isNoPrepay(o)" class="status-tag gray">无预付款</span>')
s = s.replace(a3, n3, 1)
steps.append('已退款按钮加 !isNoPrepay 判断 + 补「无预付款」灰标签')

out = s.encode('utf-8')
after = hashlib.md5(out).hexdigest()

# 自检
checks = {
    'isNoPrepay 出现次数': s.count('isNoPrepay'),
    '无预付款 出现次数': s.count('无预付款'),
    'statusTextFor 出现次数': s.count('statusTextFor'),
    '退款按钮旧式(无isNoPrepay)残留': s.count(r'!==\'refunded\'" class="btn-text orange"'),
}
assert checks['isNoPrepay 出现次数'] == 4, checks          # 1 定义 + 2 按钮 + 1 标签
assert checks['退款按钮旧式(无isNoPrepay)残留'] == 0, checks
assert s.count('<script') == (s.encode('utf-8').count(b'<script')), 'sanity'

shutil.copy2(P, P + '.bak_s301')
open(P, 'wb').write(out)

print('文件: %s' % P)
print('改前 md5: %s (%d 字节)' % (before, len(src)))
print('改后 md5: %s (%d 字节)' % (after, len(out)))
for x in steps:
    print('  [OK] ' + x)
print('自检:')
for k, v in checks.items():
    print('  %s = %s' % (k, v))
