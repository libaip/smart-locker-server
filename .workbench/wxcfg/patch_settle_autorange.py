#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S226 2026-09-16] 代理商分成结算页: 自动带上「还有未结/需重算的月份」+ 加提示.

背景: 结算口径按【下单月】归属, 退款额取订单当前值 => 上月订单在本月退款, 只有"重新结算上月"
      才会把多付的押金扣回(算成负数差额, 自动挂账到下次抵扣). 原来页面默认只选【上个月】一个月,
      老板若只结当月, 跨月退回的押金永远扣不回来(平台持续多付).
改法(纯前端, 只影响页面默认值, 不碰后端/不碰钱):
  1. 进页面时先按【近3个月(不含本月)】试算一次, 把"本次应结 != 0"的月份自动选进区间:
     没结过的月份会被补付, 结过之后又有退款的月份会被重算扣回;
  2. 区间里所有月份都会落账, 所以页面上加一句醒目提示说明这件事;
  3. 老板想只结某个月时, 手动改月份输入框即可(行为不变).
"""
import io, os, shutil, sys, time

ROOT = '/home/ubuntu/smart-locker'
P = 'static/admin-v2.html'
STAMP = time.strftime('%Y%m%d_%H%M%S')


def rep(old, new, cnt=1, tag=''):
    path = os.path.join(ROOT, P)
    s = io.open(path, encoding='utf-8').read()
    n = s.count(old)
    if n != cnt:
        print('❌ [%s] 匹配 %d 次(期望 %d), 已中止(未写入)' % (tag, n, cnt))
        sys.exit(1)
    io.open(path, 'w', encoding='utf-8').write(s.replace(old, new))
    print('✅ [%s] 替换 %d 处' % (tag, n))


src = os.path.join(ROOT, P)
shutil.copy2(src, src + '.bak_s226_' + STAMP)
print('已备份 %s.bak_s226_%s' % (P, STAMP))

# 1) created 里改成先自动挑月份
rep('this.$parent.loadAgents();this.loadPreview();this.loadHistory();',
    'this.$parent.loadAgents();this.autoPickRange();this.loadHistory();',
    tag='created')

# 2) 新增 autoPickRange 方法
rep("methods:{loadPreview:function(){",
    "methods:{autoPickRange:function(){"
    "var self=this;var d=new Date();var y=d.getFullYear();var m=d.getMonth();if(m===0){y-=1;m=12;}"
    "var fmt=function(a,b){return a+'-'+(b<10?'0':'')+b;};"
    "var ey=y,em=m,sy=y,sm=m-2;while(sm<1){sm+=12;sy-=1;}"
    "this.filter.start_month=fmt(sy,sm);this.filter.end_month=fmt(ey,em);"
    "this.$parent.api('/admin/agent-settlement/preview',{agent_id:this.filter.agent_id,start_month:this.filter.start_month,end_month:this.filter.end_month},'GET')"
    ".then(function(r){var rows=(r.data&&r.data.rows)||[];"
    "var ms=rows.filter(function(x){return Math.abs(Number(x.delta_amount||0))>0.005;})"
    ".map(function(x){return x.settle_month;}).sort();"
    "if(ms.length){self.filter.start_month=ms[0];self.filter.end_month=ms[ms.length-1];}})"
    ".catch(function(){}).then(function(){self.loadPreview();});},"
    "loadPreview:function(){",
    tag='method')

# 3) 页面加提示
rep('''<div class="search-item"><label>&nbsp;</label><button class="btn btn-sm" style="background:#e74c3c;border-color:#e74c3c;color:#fff" :disabled="!previewRows.length" @click="confirmSettle()">确认结算</button></div>
</div>''',
    '''<div class="search-item"><label>&nbsp;</label><button class="btn btn-sm" style="background:#e74c3c;border-color:#e74c3c;color:#fff" :disabled="!previewRows.length" @click="confirmSettle()">确认结算</button></div>
</div>
<div style="margin:0 0 14px;padding:10px 12px;background:#fdf6ec;border:1px solid #f5dab1;border-radius:6px;color:#b88230;font-size:13px;line-height:1.7">
<b>关于月份区间</b>（落账后不可撤销）：区间里<b>所有月份都会一起落账打款</b>。进页面时系统已自动把近 3 个月内「<b>还没结过</b>」和「<b>结过之后又有退款需要重算</b>」的月份选进区间 —— 没结过的会<b>补付</b>，需要重算的会把<b>跨月退回的押金扣回</b>（在上月结算时多付的部分，算成负数差额自动挂到下一次抵扣）。只想结某一个月时，改上面的月份输入框再点【试算】即可。
</div>''',
    tag='hint')

print('🎉 补丁完成')

