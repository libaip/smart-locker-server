# -*- coding: utf-8 -*-
"""
patch_html.py - 替换admin-v2.html中的11个placeholder组件为真实功能页面
"""
import re

HTML_FILE = '/home/ubuntu/smart-locker/static/admin-v2.html'

with open(HTML_FILE, 'r', encoding='utf-8') as f:
    html = f.read()

# 备份
with open(HTML_FILE + '.bak3', 'w', encoding='utf-8') as f:
    f.write(html)

# ========== 1. 替换11个placeholder组件 ==========

# 1) 结算流水
html = html.replace(
    "Vue.component('page-settlement',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>结算流水</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-settlement',{template:`
<div class="page-box">
<div class="box-header"><h3>结算流水</h3></div>
<div class="search-bar" style="display:flex;gap:10px;margin-bottom:15px;flex-wrap:wrap">
<div class="search-item"><label>网点</label><select v-model="$parent.settleFilter.location_id" style="width:180px"><option value="">全部</option><option v-for="l in $parent.locations" :value="l.id">{{l.name}}</option></select></div>
<div class="search-item"><label>开始</label><input type="date" v-model="$parent.settleFilter.date_start"></div>
<div class="search-item"><label>结束</label><input type="date" v-model="$parent.settleFilter.date_end"></div>
<div class="search-item"><button class="btn btn-sm btn-blue" @click="$parent.loadSettlement(1)">查询</button></div>
</div>
<div style="display:flex;gap:15px;margin-bottom:15px">
<div class="stat-card"><div class="stat-label">总订单</div><div class="stat-value">{{$parent.settleStats.total_orders||0}}</div></div>
<div class="stat-card"><div class="stat-label">总押金</div><div class="stat-value">¥{{$parent.settleStats.total_deposit||0}}</div></div>
<div class="stat-card"><div class="stat-label">使用中</div><div class="stat-value">{{$parent.settleStats.active_orders||0}}</div></div>
<div class="stat-card"><div class="stat-label">已完成</div><div class="stat-value">{{$parent.settleStats.completed||0}}</div></div>
<div class="stat-card"><div class="stat-label">已退款</div><div class="stat-value">{{$parent.settleStats.refunded||0}}</div></div>
</div>
<table class="data-table">
<thead><tr><th>订单号</th><th>手机号</th><th>柜体</th><th>押金</th><th>状态</th><th>存入时间</th><th>取物时间</th></tr></thead>
<tbody>
<tr v-for="row in $parent.settleList" :key="row.id">
<td>{{row.order_no}}</td><td>{{row.user_phone}}</td><td>{{row.cabinet_name||row.cabinet_id}}</td>
<td>¥{{row.deposit_amount}}</td>
<td><span :class="row.status==1%s'tag tag-blue':row.status==2%s'tag tag-green':'tag tag-gray'">{{row.status==1%s'使用中':row.status==2%s'已完成':row.status==3%s'已退款':row.status==4%s'超时':'已关闭'}}</span></td>
<td>{{row.store_time}}</td><td>{{row.retrieve_time||'-'}}</td>
</tr>
<tr v-if="$parent.settleList.length===0"><td colspan="7" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="pagination" v-if="$parent.settleTotal>20">
<button class="btn btn-sm" :disabled="$parent.settlePage<=1" @click="$parent.loadSettlement($parent.settlePage-1)">上一页</button>
<span>第{{$parent.settlePage}}页/共{{Math.ceil($parent.settleTotal/20)}}页</span>
<button class="btn btn-sm" :disabled="$parent.settlePage>=Math.ceil($parent.settleTotal/20)" @click="$parent.loadSettlement($parent.settlePage+1)">下一页</button>
</div>
</div>`,created(){this.$parent.loadSettlementStats();this.$parent.loadSettlement(1);}});"""
)

# 2) 提现管理
html = html.replace(
    "Vue.component('page-withdraw-manage',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>提现管理</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-withdraw-manage',{template:`
<div class="page-box">
<div class="box-header"><h3>提现管理</h3></div>
<div class="search-bar" style="margin-bottom:15px">
<select v-model="$parent.withdrawFilter.status" style="width:150px" @change="$parent.loadWithdrawals(1)">
<option value="">全部状态</option><option value="0">待审核</option><option value="1">已通过</option><option value="2">已拒绝</option>
</select>
</div>
<table class="data-table">
<thead><tr><th>ID</th><th>订单ID</th><th>手机号</th><th>金额</th><th>状态</th><th>申请时间</th><th>审核时间</th><th>操作</th></tr></thead>
<tbody>
<tr v-for="row in $parent.withdrawList" :key="row.id">
<td>{{row.id}}</td><td>{{row.order_id}}</td><td>{{row.user_phone}}</td><td>¥{{row.amount}}</td>
<td><span :class="row.status===0%s'tag tag-orange':row.status===1%s'tag tag-green':'tag tag-red'">{{row.status===0%s'待审核':row.status===1%s'已通过':'已拒绝'}}</span></td>
<td>{{row.apply_time||row.created_at}}</td><td>{{row.approve_time||'-'}}</td>
<td v-if="row.status===0"><button class="btn btn-sm btn-blue" @click="$parent.approveWithdrawal(row.id,'approve')">通过</button> <button class="btn btn-sm btn-red" @click="$parent.approveWithdrawal(row.id,'reject')">拒绝</button></td>
<td v-else>-</td>
</tr>
<tr v-if="$parent.withdrawList.length===0"><td colspan="8" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="pagination" v-if="$parent.withdrawTotal>20">
<button class="btn btn-sm" :disabled="$parent.withdrawPage<=1" @click="$parent.loadWithdrawals($parent.withdrawPage-1)">上一页</button>
<span>第{{$parent.withdrawPage}}页</span>
<button class="btn btn-sm" :disabled="$parent.withdrawPage>=Math.ceil($parent.withdrawTotal/20)" @click="$parent.loadWithdrawals($parent.withdrawPage+1)">下一页</button>
</div>
</div>`,created(){this.$parent.loadWithdrawals(1);}});"""
)

# 3) 平台流水
html = html.replace(
    "Vue.component('page-platform-flow',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>平台流水</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-platform-flow',{template:`
<div class="page-box">
<div class="box-header"><h3>平台流水</h3></div>
<div style="display:flex;gap:15px;margin-bottom:15px">
<div class="stat-card"><div class="stat-label">总押金</div><div class="stat-value">¥{{$parent.flowStats.total_deposit||0}}</div></div>
<div class="stat-card"><div class="stat-label">总退款</div><div class="stat-value">¥{{$parent.flowStats.total_refund||0}}</div></div>
</div>
<div class="search-bar" style="margin-bottom:15px">
<select v-model="$parent.flowFilter.type" style="width:150px" @change="$parent.loadPlatformFlow(1)">
<option value="">全部类型</option><option value="1">押金</option><option value="2">退款</option>
</select>
</div>
<table class="data-table">
<thead><tr><th>ID</th><th>订单号</th><th>手机号</th><th>类型</th><th>金额</th><th>交易号</th><th>状态</th><th>时间</th></tr></thead>
<tbody>
<tr v-for="row in $parent.flowList" :key="row.id">
<td>{{row.id}}</td><td>{{row.order_no||row.order_id}}</td><td>{{row.user_phone}}</td>
<td><span :class="row.type==1%s'tag tag-blue':'tag tag-orange'">{{row.type==1%s'押金':'退款'}}</span></td>
<td>¥{{row.amount}}</td><td>{{row.transaction_id||'-'}}</td>
<td><span :class="row.status==1%s'tag tag-green':'tag tag-gray'">{{row.status==1%s'成功':'待处理'}}</span></td>
<td>{{row.created_at}}</td>
</tr>
<tr v-if="$parent.flowList.length===0"><td colspan="8" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="pagination" v-if="$parent.flowTotal>20">
<button class="btn btn-sm" :disabled="$parent.flowPage<=1" @click="$parent.loadPlatformFlow($parent.flowPage-1)">上一页</button>
<span>第{{$parent.flowPage}}页</span>
<button class="btn btn-sm" :disabled="$parent.flowPage>=Math.ceil($parent.flowTotal/20)" @click="$parent.loadPlatformFlow($parent.flowPage+1)">下一页</button>
</div>
</div>`,created(){this.$parent.loadPlatformFlow(1);}});"""
)

# 4) 资金流水
html = html.replace(
    "Vue.component('page-fund-flow',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>资金流水</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-fund-flow',{template:`
<div class="page-box">
<div class="box-header"><h3>资金流水</h3></div>
<div style="display:flex;gap:15px;margin-bottom:15px">
<div class="stat-card"><div class="stat-label">总用户数</div><div class="stat-value">{{$parent.fundStats.total_users||0}}</div></div>
<div class="stat-card"><div class="stat-label">总余额</div><div class="stat-value">¥{{$parent.fundStats.total_balance||0}}</div></div>
</div>
<table class="data-table">
<thead><tr><th>手机号</th><th>余额</th><th>总充值</th><th>总提现</th><th>订单数</th><th>首次使用</th></tr></thead>
<tbody>
<tr v-for="row in $parent.fundList" :key="row.phone">
<td>{{row.phone}}</td><td>¥{{row.balance}}</td><td>¥{{row.total_deposited||0}}</td><td>¥{{row.total_withdrawn||0}}</td>
<td>{{row.order_count||0}}</td><td>{{row.first_use_time}}</td>
</tr>
<tr v-if="$parent.fundList.length===0"><td colspan="6" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
</div>`,created(){this.$parent.loadFundFlow();}});"""
)

# 5) 综合查询
html = html.replace(
    "Vue.component('page-query-all',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>综合查询</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-query-all',{template:`
<div class="page-box">
<div class="box-header"><h3>综合查询</h3></div>
<div class="search-bar" style="display:flex;gap:10px;margin-bottom:15px">
<select v-model="$parent.queryType" style="width:120px">
<option value="order">按订单</option><option value="phone">按手机号</option><option value="cabinet">按柜体</option>
</select>
<input type="text" v-model="$parent.queryKeyword" placeholder="输入关键词搜索" style="width:250px" @keyup.enter="$parent.loadQueryAll()">
<button class="btn btn-sm btn-blue" @click="$parent.loadQueryAll()">搜索</button>
</div>
<table class="data-table">
<thead><tr>
<template v-if="$parent.queryType==='order'"><th>订单号</th><th>手机号</th><th>柜体</th><th>押金</th><th>状态</th><th>存入时间</th></template>
<template v-if="$parent.queryType==='phone'"><th>手机号</th><th>余额</th><th>总充值</th><th>总提现</th><th>订单数</th></template>
<template v-if="$parent.queryType==='cabinet'"><th>编号</th><th>名称</th><th>设备ID</th><th>网点</th><th>总格口</th><th>状态</th></template>
</tr></thead>
<tbody>
<template v-if="$parent.queryType==='order'">
<tr v-for="row in $parent.queryResults" :key="row.id"><td>{{row.order_no}}</td><td>{{row.user_phone}}</td><td>{{row.cabinet_name||row.cabinet_id}}</td><td>¥{{row.deposit_amount}}</td><td>{{row.status}}</td><td>{{row.store_time}}</td></tr>
</template>
<template v-if="$parent.queryType==='phone'">
<tr v-for="row in $parent.queryResults" :key="row.phone"><td>{{row.phone}}</td><td>¥{{row.balance}}</td><td>¥{{row.total_deposited||0}}</td><td>¥{{row.total_withdrawn||0}}</td><td>{{row.order_count||0}}</td></tr>
</template>
<template v-if="$parent.queryType==='cabinet'">
<tr v-for="row in $parent.queryResults" :key="row.id"><td>{{row.cabinet_code}}</td><td>{{row.name}}</td><td>{{row.mainboard_device_id}}</td><td>{{row.location_name||'-'}}</td><td>{{row.total_slots}}</td><td>{{row.status==1%s'正常':'停用'}}</td></tr>
</template>
<tr v-if="$parent.queryResults.length===0"><td colspan="6" style="text-align:center;color:#909399;padding:20px">请输入关键词搜索</td></tr>
</tbody>
</table>
</div>`});"""
)

# 6) 公司管理
html = html.replace(
    "Vue.component('page-company-list',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>公司管理</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-company-list',{template:`
<div class="page-box">
<div class="box-header"><h3>公司管理</h3><button class="btn btn-sm btn-blue" style="float:right" @click="$parent.companyModal=true;$parent.companyForm={}">+ 新增</button></div>
<table class="data-table">
<thead><tr><th>ID</th><th>公司名称</th><th>信用代码</th><th>联系人</th><th>电话</th><th>地址</th><th>状态</th><th>操作</th></tr></thead>
<tbody>
<tr v-for="row in $parent.companyList" :key="row.id">
<td>{{row.id}}</td><td>{{row.name}}</td><td>{{row.credit_code}}</td><td>{{row.contact_person}}</td><td>{{row.contact_phone}}</td><td>{{row.address}}</td>
<td><span :class="row.status==1%s'tag tag-green':'tag tag-gray'">{{row.status==1%s'正常':'停用'}}</span></td>
<td><button class="btn btn-sm" @click="$parent.companyForm=Object.assign({},row);$parent.companyModal=true">编辑</button> <button class="btn btn-sm btn-red" @click="$parent.deleteCompany(row.id)">删除</button></td>
</tr>
<tr v-if="$parent.companyList.length===0"><td colspan="8" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="modal" v-if="$parent.companyModal" style="display:block;background:rgba(0,0,0,0.5);position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999">
<div style="background:#fff;width:500px;margin:80px auto;padding:20px;border-radius:8px">
<h3>{{($parent.companyForm.id%s'编辑':'新增')}}公司</h3>
<div style="margin:10px 0"><label>公司名称</label><input type="text" v-model="$parent.companyForm.name" style="width:100%"></div>
<div style="margin:10px 0"><label>信用代码</label><input type="text" v-model="$parent.companyForm.credit_code" style="width:100%"></div>
<div style="margin:10px 0"><label>联系人</label><input type="text" v-model="$parent.companyForm.contact_person" style="width:100%"></div>
<div style="margin:10px 0"><label>电话</label><input type="text" v-model="$parent.companyForm.contact_phone" style="width:100%"></div>
<div style="margin:10px 0"><label>地址</label><input type="text" v-model="$parent.companyForm.address" style="width:100%"></div>
<div style="text-align:right;margin-top:15px"><button class="btn btn-sm" @click="$parent.companyModal=false">取消</button> <button class="btn btn-sm btn-blue" @click="$parent.saveCompany()">保存</button></div>
</div>
</div>
</div>`,created(){this.$parent.loadCompanies();}});"""
)

# 7) 黑名单
html = html.replace(
    "Vue.component('page-blacklist',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>黑名单</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-blacklist',{template:`
<div class="page-box">
<div class="box-header"><h3>黑名单</h3><button class="btn btn-sm btn-blue" style="float:right" @click="$parent.blackModal=true;$parent.blackForm={}">+ 新增</button></div>
<table class="data-table">
<thead><tr><th>ID</th><th>手机号</th><th>原因</th><th>柜体</th><th>操作人</th><th>状态</th><th>操作</th></tr></thead>
<tbody>
<tr v-for="row in $parent.blackList" :key="row.id">
<td>{{row.id}}</td><td>{{row.phone}}</td><td>{{row.reason}}</td><td>{{row.cabinet_name||row.cabinet_id||'全部'}}</td><td>{{row.operator}}</td>
<td><span :class="row.status==1%s'tag tag-red':'tag tag-gray'">{{row.status==1%s'封禁':'解封'}}</span></td>
<td><button class="btn btn-sm" @click="$parent.blackForm=Object.assign({},row);$parent.blackModal=true">编辑</button> <button class="btn btn-sm btn-red" @click="$parent.deleteBlack(row.id)">删除</button></td>
</tr>
<tr v-if="$parent.blackList.length===0"><td colspan="7" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="modal" v-if="$parent.blackModal" style="display:block;background:rgba(0,0,0,0.5);position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999">
<div style="background:#fff;width:400px;margin:80px auto;padding:20px;border-radius:8px">
<h3>{{($parent.blackForm.id%s'编辑':'新增')}}黑名单</h3>
<div style="margin:10px 0"><label>手机号</label><input type="text" v-model="$parent.blackForm.phone" style="width:100%"></div>
<div style="margin:10px 0"><label>原因</label><input type="text" v-model="$parent.blackForm.reason" style="width:100%"></div>
<div style="text-align:right;margin-top:15px"><button class="btn btn-sm" @click="$parent.blackModal=false">取消</button> <button class="btn btn-sm btn-blue" @click="$parent.saveBlack()">保存</button></div>
</div>
</div>
</div>`,created(){this.$parent.loadBlacklist();}});"""
)

# 8) 告警记录
html = html.replace(
    "Vue.component('page-alarm-record',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>告警记录</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-alarm-record',{template:`
<div class="page-box">
<div class="box-header"><h3>告警记录</h3></div>
<div class="search-bar" style="margin-bottom:15px">
<select v-model="$parent.alarmFilter.status" style="width:150px" @change="$parent.loadAlarms(1)">
<option value="">全部</option><option value="0">未处理</option><option value="1">已处理</option>
</select>
</div>
<table class="data-table">
<thead><tr><th>ID</th><th>类型</th><th>柜体</th><th>内容</th><th>级别</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
<tbody>
<tr v-for="row in $parent.alarmList" :key="row.id">
<td>{{row.id}}</td><td>{{row.type}}</td><td>{{row.cabinet_name||row.cabinet_id||'-'}}</td><td>{{row.content}}</td>
<td><span :class="row.level>=2%s'tag tag-red':'tag tag-orange'">{{row.level>=2%s'高':'中'}}</span></td>
<td><span :class="row.status==0%s'tag tag-orange':'tag tag-green'">{{row.status==0%s'未处理':'已处理'}}</span></td>
<td>{{row.created_at}}</td>
<td v-if="row.status==0"><button class="btn btn-sm btn-blue" @click="$parent.resolveAlarm(row.id)">处理</button></td><td v-else>{{row.resolver||'-'}}</td>
</tr>
<tr v-if="$parent.alarmList.length===0"><td colspan="8" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="pagination" v-if="$parent.alarmTotal>20">
<button class="btn btn-sm" :disabled="$parent.alarmPage<=1" @click="$parent.loadAlarms($parent.alarmPage-1)">上一页</button>
<span>第{{$parent.alarmPage}}页</span>
<button class="btn btn-sm" :disabled="$parent.alarmPage>=Math.ceil($parent.alarmTotal/20)" @click="$parent.loadAlarms($parent.alarmPage+1)">下一页</button>
</div>
</div>`,created(){this.$parent.loadAlarms(1);}});"""
)

# 9) 网点告警
html = html.replace(
    "Vue.component('page-location-alarm',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>网点告警</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-location-alarm',{template:`
<div class="page-box">
<div class="box-header"><h3>网点告警</h3></div>
<table class="data-table">
<thead><tr><th>柜体编号</th><th>名称</th><th>设备ID</th><th>网点</th><th>心跳时间</th><th>离线时长</th><th>活跃订单</th><th>告警数</th><th>状态</th></tr></thead>
<tbody>
<tr v-for="row in $parent.locAlarmList" :key="row.id">
<td>{{row.cabinet_code}}</td><td>{{row.name}}</td><td>{{row.mainboard_device_id}}</td><td>{{row.location_name||'-'}}</td>
<td>{{row.last_heartbeat||'-'}}</td>
<td><span :class="row.offline%s'tag tag-red':'tag tag-green'">{{row.offline%s('离线'+row.heartbeat_age_min+'分钟'):'在线'}}</span></td>
<td>{{row.active_orders||0}}</td><td>{{row.alarm_count||0}}</td>
<td><span :class="row.status==1%s'tag tag-green':'tag tag-gray'">{{row.status==1%s'正常':'停用'}}</span></td>
</tr>
<tr v-if="$parent.locAlarmList.length===0"><td colspan="9" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
</div>`,created(){this.$parent.loadLocAlarms();}});"""
)

# 10) 角色权限
html = html.replace(
    "Vue.component('page-role-manage',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>角色权限</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-role-manage',{template:`
<div class="page-box">
<div class="box-header"><h3>角色权限</h3><button class="btn btn-sm btn-blue" style="float:right" @click="$parent.roleModal=true;$parent.roleForm={}">+ 新增</button></div>
<table class="data-table">
<thead><tr><th>ID</th><th>用户名</th><th>角色</th><th>创建时间</th><th>操作</th></tr></thead>
<tbody>
<tr v-for="row in $parent.roleList" :key="row.id">
<td>{{row.id}}</td><td>{{row.username}}</td>
<td><span :class="row.role==='admin'%s'tag tag-red':row.role==='operator'%s'tag tag-blue':'tag tag-gray'">{{row.role==='admin'%s'管理员':row.role==='operator'%s'操作员':'查看者'}}</span></td>
<td>{{row.created_at}}</td>
<td><button class="btn btn-sm" @click="$parent.roleForm=Object.assign({},row);$parent.roleModal=true">编辑</button></td>
</tr>
<tr v-if="$parent.roleList.length===0"><td colspan="5" style="text-align:center;color:#909399;padding:20px">暂无数据</td></tr>
</tbody>
</table>
<div class="modal" v-if="$parent.roleModal" style="display:block;background:rgba(0,0,0,0.5);position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999">
<div style="background:#fff;width:400px;margin:80px auto;padding:20px;border-radius:8px">
<h3>{{($parent.roleForm.id%s'编辑':'新增')}}用户</h3>
<div style="margin:10px 0"><label>用户名</label><input type="text" v-model="$parent.roleForm.username" style="width:100%"></div>
<div style="margin:10px 0"><label>角色</label><select v-model="$parent.roleForm.role" style="width:100%"><option value="admin">管理员</option><option value="operator">操作员</option><option value="viewer">查看者</option></select></div>
<div style="margin:10px 0"><label>密码{{($parent.roleForm.id%s'(留空不修改)':'')}}</label><input type="password" v-model="$parent.roleForm.password" style="width:100%"></div>
<div style="text-align:right;margin-top:15px"><button class="btn btn-sm" @click="$parent.roleModal=false">取消</button> <button class="btn btn-sm btn-blue" @click="$parent.saveRole()">保存</button></div>
</div>
</div>
</div>`,created(){this.$parent.loadRoles();}});"""
)

# 11) 数据重置
html = html.replace(
    "Vue.component('page-data-reset',{template:'<div class=\"page-box\"><div class=\"box-header\"><h3>数据重置</h3></div><p style=\"color:#909399;text-align:center;padding:40px\">功能开发中</p></div>'});",
    """Vue.component('page-data-reset',{template:`
<div class="page-box">
<div class="box-header"><h3>数据重置</h3></div>
<div style="margin-bottom:15px;color:#E6A23C;font-size:13px">⚠ 此操作不可逆，请谨慎操作！选择要清理的数据表：</div>
<table class="data-table">
<thead><tr><th>数据表</th><th>记录数</th><th>选择</th></tr></thead>
<tbody>
<tr v-for="(count,key) in $parent.resetStats" :key="key">
<td>{{key}}</td><td>{{count}}</td>
<td><input type="checkbox" :value="key" v-model="$parent.resetSelected"></td>
</tr>
<tr v-if="Object.keys($parent.resetStats).length===0"><td colspan="3" style="text-align:center;color:#909399;padding:20px">加载中...</td></tr>
</tbody>
</table>
<div style="margin-top:15px">
<button class="btn btn-sm btn-red" @click="$parent.execReset()" :disabled="$parent.resetSelected.length===0">清理选中数据 ({{$parent.resetSelected.length}})</button>
</div>
</div>`,created(){this.$parent.loadResetStats();}});"""
)

# ========== 2. 追加data属性 ==========
# 在statsFilter行后追加
old_data = "statsFilter:{location_id:'',dateRange:[]},orderStats:{},locationStats:[],statsChart:null,"
new_data = old_data + """
settleList:[],settleTotal:0,settlePage:1,settleStats:{},settleFilter:{location_id:'',date_start:'',date_end:''},
withdrawList:[],withdrawTotal:0,withdrawPage:1,withdrawFilter:{status:''},
flowList:[],flowTotal:0,flowPage:1,flowFilter:{type:''},flowStats:{},
fundList:[],fundStats:{},
queryType:'order',queryKeyword:'',queryResults:[],
companyList:[],companyModal:false,companyForm:{},
blackList:[],blackModal:false,blackForm:{},
alarmList:[],alarmTotal:0,alarmPage:1,alarmFilter:{status:''},
locAlarmList:[],
roleList:[],roleModal:false,roleForm:{},
resetStats:{},resetSelected:[],"""
html = html.replace(old_data, new_data)

# ========== 3. 追加methods ==========
# 在resetStatsFilter行后追加
old_method = "resetStatsFilter:function(){this.statsFilter={location_id:'',dateRange:[]};this.loadStats();},"
new_methods = old_method + """
loadSettlement:function(p){var self=this;if(p)this.settlePage=p;this.api('/settlement/list',{page:this.settlePage,location_id:this.settleFilter.location_id,date_start:this.settleFilter.date_start,date_end:this.settleFilter.date_end}).then(function(d){self.settleList=d.data.list||[];self.settleTotal=d.data.total||0;}).catch(function(){});},
loadSettlementStats:function(){var self=this;this.api('/settlement/stats').then(function(d){self.settleStats=d.data||{};}).catch(function(){});},
loadWithdrawals:function(p){var self=this;if(p)this.withdrawPage=p;this.api('/withdrawals/list',{page:this.withdrawPage,status:this.withdrawFilter.status}).then(function(d){self.withdrawList=d.data.list||[];self.withdrawTotal=d.data.total||0;}).catch(function(){});},
approveWithdrawal:function(id,action){var self=this;this.api('/withdrawals/approve',{id:id,action:action}).then(function(){self.toast('处理成功');self.loadWithdrawals();}).catch(function(e){self.toast(e.message||'操作失败','error');});},
loadPlatformFlow:function(p){var self=this;if(p)this.flowPage=p;this.api('/platform-flow/list',{page:this.flowPage,type:this.flowFilter.type}).then(function(d){self.flowList=d.data.list||[];self.flowTotal=d.data.total||0;self.flowStats={total_deposit:d.data.total_deposit,total_refund:d.data.total_refund};}).catch(function(){});},
loadFundFlow:function(){var self=this;this.api('/fund-flow/list').then(function(d){self.fundList=d.data.list||[];self.fundStats={total_users:d.data.total_users,total_balance:d.data.total_balance};}).catch(function(){});},
loadQueryAll:function(){var self=this;if(!this.queryKeyword)return;this.api('/query-all/list',{keyword:this.queryKeyword,type:this.queryType}).then(function(d){self.queryResults=d.data.list||[];}).catch(function(){});},
loadCompanies:function(){var self=this;this.api('/companies/list').then(function(d){self.companyList=d.data.list||[];}).catch(function(){});},
saveCompany:function(){var self=this;this.api('/companies/save',this.companyForm).then(function(){self.toast('保存成功');self.companyModal=false;self.loadCompanies();}).catch(function(e){self.toast(e.message||'保存失败','error');});},
deleteCompany:function(id){var self=this;if(!confirm('确认删除%s'))return;this.api('/companies/delete',{id:id}).then(function(){self.toast('删除成功');self.loadCompanies();}).catch(function(e){self.toast(e.message||'删除失败','error');});},
loadBlacklist:function(){var self=this;this.api('/blacklist/list').then(function(d){self.blackList=d.data.list||[];}).catch(function(){});},
saveBlack:function(){var self=this;this.api('/blacklist/save',this.blackForm).then(function(){self.toast('保存成功');self.blackModal=false;self.loadBlacklist();}).catch(function(e){self.toast(e.message||'保存失败','error');});},
deleteBlack:function(id){var self=this;if(!confirm('确认删除%s'))return;this.api('/blacklist/delete',{id:id}).then(function(){self.toast('删除成功');self.loadBlacklist();}).catch(function(e){self.toast(e.message||'删除失败','error');});},
loadAlarms:function(p){var self=this;if(p)this.alarmPage=p;this.api('/alarms/list',{page:this.alarmPage,status:this.alarmFilter.status}).then(function(d){self.alarmList=d.data.list||[];self.alarmTotal=d.data.total||0;}).catch(function(){});},
resolveAlarm:function(id){var self=this;this.api('/alarms/resolve',{id:id}).then(function(){self.toast('处理成功');self.loadAlarms();}).catch(function(e){self.toast(e.message||'操作失败','error');});},
loadLocAlarms:function(){var self=this;this.api('/location-alarms/list').then(function(d){self.locAlarmList=d.data.list||[];}).catch(function(){});},
loadRoles:function(){var self=this;this.api('/roles/list').then(function(d){self.roleList=d.data.list||[];}).catch(function(){});},
saveRole:function(){var self=this;this.api('/roles/save',this.roleForm).then(function(){self.toast('保存成功');self.roleModal=false;self.loadRoles();}).catch(function(e){self.toast(e.message||'保存失败','error');});},
loadResetStats:function(){var self=this;this.api('/data-reset/stats').then(function(d){self.resetStats=d.data||{};}).catch(function(){});},
execReset:function(){var self=this;if(!confirm('确认清理选中的'+this.resetSelected.length+'个数据表%s此操作不可逆!'))return;this.api('/data-reset/exec',{tables:this.resetSelected}).then(function(){self.toast('清理完成');self.loadResetStats();self.resetSelected=[];}).catch(function(e){self.toast(e.message||'操作失败','error');});},"""

html = html.replace(old_method, new_methods)

# ========== 4. 替换menu handler ==========
old_handler = "else if(v==='role-manage'||v==='data-reset'||v==='settlement'||v==='withdraw-manage'||v==='platform-flow'||v==='fund-flow'||v==='query-all'||v==='company-list'||v==='blacklist'||v==='alarm-record'||v==='location-alarm')void 0;"
new_handler = """else if(v==='settlement'){this.loadSettlementStats();this.loadSettlement(1);}
else if(v==='withdraw-manage'){this.loadWithdrawals(1);}
else if(v==='platform-flow'){this.loadPlatformFlow(1);}
else if(v==='fund-flow'){this.loadFundFlow();}
else if(v==='query-all'){this.queryResults=[];}
else if(v==='company-list'){this.loadCompanies();}
else if(v==='blacklist'){this.loadBlacklist();}
else if(v==='alarm-record'){this.loadAlarms(1);}
else if(v==='location-alarm'){this.loadLocAlarms();}
else if(v==='role-manage'){this.loadRoles();}
else if(v==='data-reset'){this.loadResetStats();}"""
html = html.replace(old_handler, new_handler)

# ========== 写回 ==========
with open(HTML_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'Done! File size: {len(html)} bytes, lines: {html.count(chr(10))+1}')
