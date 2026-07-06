#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复admin-v2.html的JS语法错误"""

import re

def fix_admin_v2():
    # 读取文件
    with open('/home/ubuntu/smart-locker/static/admin-v2.html', 'r', encoding='utf-8') as f:
        content = f.read()
    
    original = content
    
    # 1. 移除593行附近错误的backtick组件定义（// System Settings 到 // Modal之间）
    # 匹配从 "// System Settings\nVue.component('page-system-settings',{template:`" 
    # 到 "// Modal: Cabinet Group" 之前的裸HTML
    pattern1 = r'// System Settings\nVue\.component\(\'page-system-settings\',\{template:`[\s\S]*%s`\}\);\n\n// Cabinet Groups\nVue\.component\(\'page-cabinet-groups\',\{template:`[\s\S]*%s`\}\);\n\n// Offline Orders\nVue\.component\(\'page-offline-orders\',\{template:`[\s\S]*%s`\}\);\n\n// Modal: Cabinet Group\n<div[^>]*>[\s\S]*%s</div>\n\n'
    
    content = re.sub(pattern1, '', content)
    
    # 2. 修复792行附近的重复backtick组件定义（同样的模式）
    pattern2 = r'// System Settings\nVue\.component\(\'page-system-settings\',\{template:`[\s\S]*%s`\}\);\n\n// Cabinet Groups\nVue\.component\(\'page-cabinet-groups\',\{template:`[\s\S]*%s`\}\);\n\n// Offline Orders\nVue\.component\(\'page-offline-orders\',\{template:`[\s\S]*%s`\}\);\n\n// Modal: Cabinet Group\n<div[^>]*>[\s\S]*%s</div>\n\n'
    content = re.sub(pattern2, '', content)
    
    # 3. 将所有剩余的 backtick 模板改成单引号+\n 格式
    # page-system-settings
    old_system_settings = """Vue.component('page-system-settings',{template:`
<div class="page-box">
<div class="box-header"><h3>系统设置</h3><button class="btn btn-sm btn-blue" @click="$parent.saveSettings()">保存设置</button></div>
<div style="max-width:600px;margin-top:20px">
<div v-for="(val,key) in $parent.settingsData" class="form-group">
<label>{{val.desc||key}}</label>
<input v-model="val.value" :placeholder="val.desc||key">
</div>
</div>
</div>`,created(){this.$parent.loadSettings();}});"""
    
    new_system_settings = """Vue.component('page-system-settings',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>系统设置</h3><button class="btn btn-sm btn-blue" @click="$parent.saveSettings()">保存设置</button></div>\\
<div style="max-width:600px;margin-top:20px">\\
<div v-for="(val,key) in $parent.settingsData" class="form-group">\\
<label>{{val.desc||key}}</label>\\
<input v-model="val.value" :placeholder="val.desc||key">\\
</div>\\
</div>\\
</div>`,created(){this.$parent.loadSettings();}});"""
    
    content = content.replace(old_system_settings, new_system_settings)
    
    # page-cabinet-groups (注意内部有单引号嵌套)
    old_cabinet_groups = """Vue.component('page-cabinet-groups',{template:`
<div class="page-box">
<div class="box-header"><h3>柜组管理</h3><button class="btn btn-sm btn-blue" @click="$parent.showCabinetGroupModal()">+ 新增柜组</button></div>
<table><tr><th>ID</th><th>柜组编码</th><th>名称</th><th>网点ID</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
<tr v-for="g in $parent.cabinetGroups"><td>{{g.id}}</td><td>{{g.group_code}}</td><td>{{g.name||'-'}}</td><td>{{g.location_id||'-'}}</td><td><span :class="'status-tag '+(g.status===1%s'green':'red')">{{g.status===1%s'正常':'停用'}}</span></td><td>{{g.created_at}}</td><td><button class="btn-text" @click="$parent.showCabinetGroupModal(g)">编辑</button><button class="btn-text red" @click="$parent.deleteCabinetGroup(g)">删除</button></td></tr>
<tr v-if="!$parent.cabinetGroups.length"><td colspan="7" class="empty-row">暂无数据</td></tr>
</table>
<div class="pagination" v-if="$parent.cabinetGroupsTotal>20"><button class="btn-text" @click="$parent.loadCabinetGroups($parent.cabinetGroupsPage-1)" :disabled="$parent.cabinetGroupsPage<=1">上一页</button><span>{{$parent.cabinetGroupsPage}}</span><button class="btn-text" @click="$parent.loadCabinetGroups($parent.cabinetGroupsPage+1)" :disabled="$parent.cabinetGroupsPage*20>=$parent.cabinetGroupsTotal">下一页</button></div>
</div>`,created(){this.$parent.loadCabinetGroups();}});"""
    
    new_cabinet_groups = """Vue.component('page-cabinet-groups',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>柜组管理</h3><button class="btn btn-sm btn-blue" @click="$parent.showCabinetGroupModal()">+ 新增柜组</button></div>\\
<table><tr><th>ID</th><th>柜组编码</th><th>名称</th><th>网点ID</th><th>状态</th><th>创建时间</th><th>操作</th></tr>\\
<tr v-for="g in $parent.cabinetGroups"><td>{{g.id}}</td><td>{{g.group_code}}</td><td>{{g.name||\\'-'}}</td><td>{{g.location_id||\\'-'}}</td><td><span :class=\\'status-tag \\'+(g.status===1%s\\'green\\':\\'red\\')\\'>{{g.status===1%s\\'正常\\':\\'停用\\'}}</span></td><td>{{g.created_at}}</td><td><button class="btn-text" @click="$parent.showCabinetGroupModal(g)">编辑</button><button class="btn-text red" @click="$parent.deleteCabinetGroup(g)">删除</button></td></tr>\\
<tr v-if="!$parent.cabinetGroups.length"><td colspan="7" class="empty-row">暂无数据</td></tr>\\
</table>\\
<div class="pagination" v-if="$parent.cabinetGroupsTotal>20"><button class="btn-text" @click="$parent.loadCabinetGroups($parent.cabinetGroupsPage-1)" :disabled="$parent.cabinetGroupsPage<=1">上一页</button><span>{{$parent.cabinetGroupsPage}}</span><button class="btn-text" @click="$parent.loadCabinetGroups($parent.cabinetGroupsPage+1)" :disabled="$parent.cabinetGroupsPage*20>=$parent.cabinetGroupsTotal">下一页</button></div>\\
</div>`,created(){this.$parent.loadCabinetGroups();}});"""
    
    content = content.replace(old_cabinet_groups, new_cabinet_groups)
    
    # page-offline-orders
    old_offline_orders = """Vue.component('page-offline-orders',{template:`
<div class="page-box">
<div class="box-header"><h3>离线订单</h3></div>
<table><tr><th>订单号</th><th>用户手机</th><th>柜门号</th><th>金额</th><th>状态</th><th>创建时间</th></tr>
<tr v-for="o in $parent.offlineOrders"><td>{{o.order_no}}</td><td>{{o.user_phone}}</td><td>{{o.slot_id}}</td><td>{{o.amount}}</td><td>{{o.status}}</td><td>{{o.created_at}}</td></tr>
<tr v-if="!$parent.offlineOrders.length"><td colspan="6" class="empty-row">暂无数据</td></tr>
</table>
</div>`,created(){this.$parent.loadOfflineOrders();}});"""
    
    new_offline_orders = """Vue.component('page-offline-orders',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>离线订单</h3></div>\\
<table><tr><th>订单号</th><th>用户手机</th><th>柜门号</th><th>金额</th><th>状态</th><th>创建时间</th></tr>\\
<tr v-for="o in $parent.offlineOrders"><td>{{o.order_no}}</td><td>{{o.user_phone}}</td><td>{{o.slot_id}}</td><td>{{o.amount}}</td><td>{{o.status}}</td><td>{{o.created_at}}</td></tr>\\
<tr v-if="!$parent.offlineOrders.length"><td colspan="6" class="empty-row">暂无数据</td></tr>\\
</table>\\
</div>`,created(){this.$parent.loadOfflineOrders();}});"""
    
    content = content.replace(old_offline_orders, new_offline_orders)
    
    # 4. 将其他使用backtick的组件也改成单引号格式
    # page-blacklist
    content = content.replace(
        "Vue.component('page-blacklist',{template:`",
        "Vue.component('page-blacklist',{template:'"
    )
    # 找到page-blacklist的结束反引号并替换
    content = content.replace(
        "`},created(){this.$parent.loadBlacklist();}});",
        "'},created(){this.$parent.loadBlacklist();}});"
    )
    
    # page-alarm-record
    content = content.replace(
        "Vue.component('page-alarm-record',{template:`",
        "Vue.component('page-alarm-record',{template:'"
    )
    content = content.replace(
        "`},created(){this.$parent.loadAlarms(1);}});",
        "'},created(){this.$parent.loadAlarms(1);}});"
    )
    
    # page-location-alarm
    content = content.replace(
        "Vue.component('page-location-alarm',{template:`",
        "Vue.component('page-location-alarm',{template:'"
    )
    content = content.replace(
        "`},created(){this.$parent.loadLocAlarms();}});",
        "'},created(){this.$parent.loadLocAlarms();}});"
    )
    
    # page-role-manage
    content = content.replace(
        "Vue.component('page-role-manage',{template:`",
        "Vue.component('page-role-manage',{template:'"
    )
    content = content.replace(
        "`},created(){this.$parent.loadRoles();}});",
        "'},created(){this.$parent.loadRoles();}});"
    )
    
    # page-data-reset
    content = content.replace(
        "Vue.component('page-data-reset',{template:`",
        "Vue.component('page-data-reset',{template:'"
    )
    content = content.replace(
        "`},created(){this.$parent.loadResetStats();}});",
        "'},created(){this.$parent.loadResetStats();}});"
    )
    
    # page-settlement
    content = content.replace(
        "Vue.component('page-settlement',{template:`",
        "Vue.component('page-settlement',{template:'"
    )
    content = content.replace(
        "`},created(){",
        "'},created(){"
    )
    
    # page-withdraw-manage
    content = content.replace(
        "Vue.component('page-withdraw-manage',{template:`",
        "Vue.component('page-withdraw-manage',{template:'"
    )
    content = content.replace(
        "`},created(){",
        "'},created(){"
    )
    
    # page-platform-flow
    content = content.replace(
        "Vue.component('page-platform-flow',{template:`",
        "Vue.component('page-platform-flow',{template:'"
    )
    content = content.replace(
        "`},created(){",
        "'},created(){"
    )
    
    # page-fund-flow
    content = content.replace(
        "Vue.component('page-fund-flow',{template:`",
        "Vue.component('page-fund-flow',{template:'"
    )
    content = content.replace(
        "`},created(){",
        "'},created(){"
    )
    
    # page-query-all
    content = content.replace(
        "Vue.component('page-query-all',{template:`",
        "Vue.component('page-query-all',{template:'"
    )
    content = content.replace(
        "`},created(){",
        "'},created(){"
    )
    
    # page-company-list
    content = content.replace(
        "Vue.component('page-company-list',{template:`",
        "Vue.component('page-company-list',{template:'"
    )
    content = content.replace(
        "`},created(){",
        "'},created(){"
    )
    
    # 5. 添加P2级页面组件（在Vue.component定义区域，在new Vue之前添加）
    p2_components = """
// ===== P2 Pages: Remote Open Logs, Device Logs, Door Records, Pending Cmds =====
Vue.component('page-remote-open-logs',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>远程开门日志</h3></div>\\
<div class="search-bar">\\
<input v-model="$parent.remoteOpenFilter" placeholder="柜体编号/操作人" style="width:200px">\\
<button class="btn btn-sm btn-blue" @click="$parent.loadRemoteOpenLogs(1)">搜索</button>\\
</div>\\
<table><tr><th>ID</th><th>柜体</th><th>操作人</th><th>原因</th><th>时间</th></tr>\\
<tr v-for="log in $parent.remoteOpenLogs"><td>{{log.id}}</td><td>{{log.cabinet_name||log.cabinet_id||"-"}}</td><td>{{log.operator||"-"}}</td><td>{{log.reason||"-"}}</td><td>{{log.created_at}}</td></tr>\\
<tr v-if="!$parent.remoteOpenLogs.length"><td colspan="5" class="empty-row">暂无数据</td></tr>\\
</table>\\
<div class="pagination" v-if="$parent.remoteOpenLogsTotal>20">\\
<button class="btn btn-sm" :disabled="$parent.remoteOpenLogsPage<=1" @click="$parent.loadRemoteOpenLogs($parent.remoteOpenLogsPage-1)">上一页</button>\\
<span>{{$parent.remoteOpenLogsPage}} / {{Math.ceil($parent.remoteOpenLogsTotal/20)}}</span>\\
<button class="btn btn-sm" :disabled="$parent.remoteOpenLogsPage>=Math.ceil($parent.remoteOpenLogsTotal/20)" @click="$parent.loadRemoteOpenLogs($parent.remoteOpenLogsPage+1)">下一页</button>\\
</div>\\
</div>`,created(){this.$parent.loadRemoteOpenLogs(1);}});

Vue.component('page-device-logs',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>设备日志</h3></div>\\
<div class="search-bar">\\
<input v-model="$parent.deviceLogFilter" placeholder="设备ID" style="width:200px">\\
<button class="btn btn-sm btn-blue" @click="$parent.loadDeviceLogs($parent.deviceLogFilter)">搜索</button>\\
</div>\\
<table><tr><th>ID</th><th>设备ID</th><th>类型</th><th>内容</th><th>时间</th></tr>\\
<tr v-for="log in $parent.deviceLogList"><td>{{log.id}}</td><td>{{log.device_id}}</td><td>{{log.type||"-"}}</td><td>{{log.content}}</td><td>{{log.created_at}}</td></tr>\\
<tr v-if="!$parent.deviceLogList.length"><td colspan="5" class="empty-row">暂无数据</td></tr>\\
</table>\\
<div class="pagination" v-if="$parent.deviceLogTotal>20">\\
<span>共 {{$parent.deviceLogTotal}} 条</span>\\
</div>\\
</div>`,created(){this.$parent.loadDeviceLogs("");}});

Vue.component('page-door-records',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>开门记录</h3></div>\\
<div class="search-bar">\\
<input v-model="$parent.doorRecordFilter" placeholder="订单号/手机号" style="width:200px">\\
<button class="btn btn-sm btn-blue" @click="$parent.loadDoorRecords(1)">搜索</button>\\
</div>\\
<table><tr><th>ID</th><th>订单号</th><th>手机号</th><th>柜门</th><th>方式</th><th>时间</th></tr>\\
<tr v-for="r in $parent.doorRecords"><td>{{r.id}}</td><td>{{r.order_no||"-"}}</td><td>{{r.phone||"-"}}</td><td>{{r.slot_id||"-"}}</td><td>{{r.open_type||"-"}}</td><td>{{r.created_at}}</td></tr>\\
<tr v-if="!$parent.doorRecords.length"><td colspan="6" class="empty-row">暂无数据</td></tr>\\
</table>\\
<div class="pagination" v-if="$parent.doorRecordsTotal>20">\\
<button class="btn btn-sm" :disabled="$parent.doorRecordsPage<=1" @click="$parent.loadDoorRecords($parent.doorRecordsPage-1)">上一页</button>\\
<span>{{$parent.doorRecordsPage}} / {{Math.ceil($parent.doorRecordsTotal/20)}}</span>\\
<button class="btn btn-sm" :disabled="$parent.doorRecordsPage>=Math.ceil($parent.doorRecordsTotal/20)" @click="$parent.loadDoorRecords($parent.doorRecordsPage+1)">下一页</button>\\
</div>\\
</div>`,created(){this.$parent.loadDoorRecords(1);}});

Vue.component('page-pending-cmds',{template:'\\
<div class="page-box">\\
<div class="box-header"><h3>待执行命令</h3><button class="btn btn-sm btn-blue" @click="$parent.loadPendingCmds()">刷新</button></div>\\
<table><tr><th>ID</th><th>设备ID</th><th>命令</th><th>参数</th><th>状态</th><th>创建时间</th><th>操作</th></tr>\\
<tr v-for="cmd in $parent.pendingCmds"><td>{{cmd.id}}</td><td>{{cmd.device_id||"-"}}</td><td>{{cmd.command||"-"}}</td><td>{{cmd.params||"-"}}</td><td><span :class="cmd.status===1%s\\'status-tag green\\':\\'status-tag orange\\'">{{cmd.status===1%s\\'已完成\\':\\'待执行\\'}}</span></td><td>{{cmd.created_at}}</td><td><button class="btn btn-sm btn-red" @click="$parent.cancelCmd(cmd.id)">取消</button></td></tr>\\
<tr v-if="!$parent.pendingCmds.length"><td colspan="7" class="empty-row">暂无数据</td></tr>\\
</table>\\
<div class="pagination" v-if="$parent.pendingCmdsTotal>20">\\
<span>共 {{$parent.pendingCmdsTotal}} 条</span>\\
</div>\\
</div>`,created(){this.$parent.loadPendingCmds();}});

"""
    
    # 在 new Vue 之前插入P2组件
    content = content.replace('var app = new Vue({', p2_components + 'var app = new Vue({')
    
    # 6. 添加菜单项（在menuItems中添加P2页面）
    # 找到菜单定义，添加新菜单
    old_menu = """menuItems:[
{title:'首页',icon:'🏠',page:'home'},
{title:'设备管理',children:[
{title:'设备列表',page:'cabinet-list'},
{title:'网点管理',page:'location-list'},
{title:'设备地图',page:'map'},
{title:'实时状态',page:'realtime'},
{title:'离线设备',page:'offline-device'},
{title:'告警管理',children:[
{title:'告警记录',page:'alarm-record'},
{title:'网点告警',page:'location-alarm'}
]},
{title:'柜组管理',page:'cabinet-groups'},
{title:'离线订单',page:'offline-orders'},
{title:'系统设置',page:'system-settings'}
]},
{title:'订单管理',children:[
{title:'订单列表',page:'order-list'},
{title:'订单统计',page:'order-stats'},
{title:'售后工单',page:'store-record'},
{title:'退款记录',page:'refund-list'}
]},
{title:'财务管理',children:[
{title:'结算记录',page:'settlement'},
{title:'提现管理',page:'withdraw-manage'},
{title:'平台流水',page:'platform-flow'},
{title:'资金流水',page:'fund-flow'}
]},
{title:'查询统计',children:[
{title:'综合查询',page:'query-all'},
{title:'网点查询',page:'company-list'}
]},
{title:'用户权限',children:[
{title:'用户管理',page:'user-manage'},
{title:'角色权限',page:'role-manage'},
{title:'黑名单',page:'blacklist'},
{title:'数据重置',page:'data-reset'}
]},
{title:'APK管理',children:[
{title:'APK版本',page:'apk-version'}
]}
],"""
    
    new_menu = """menuItems:[
{title:'首页',icon:'🏠',page:'home'},
{title:'设备管理',children:[
{title:'设备列表',page:'cabinet-list'},
{title:'网点管理',page:'location-list'},
{title:'设备地图',page:'map'},
{title:'实时状态',page:'realtime'},
{title:'离线设备',page:'offline-device'},
{title:'设备日志',page:'device-logs'},
{title:'待执行命令',page:'pending-cmds'},
{title:'告警管理',children:[
{title:'告警记录',page:'alarm-record'},
{title:'网点告警',page:'location-alarm'}
]},
{title:'柜组管理',page:'cabinet-groups'},
{title:'离线订单',page:'offline-orders'},
{title:'系统设置',page:'system-settings'}
]},
{title:'订单管理',children:[
{title:'订单列表',page:'order-list'},
{title:'订单统计',page:'order-stats'},
{title:'售后工单',page:'store-record'},
{title:'退款记录',page:'refund-list'},
{title:'开门记录',page:'door-records'}
]},
{title:'日志监控',children:[
{title:'远程开门日志',page:'remote-open-logs'},
{title:'设备日志',page:'device-logs'},
{title:'待执行命令',page:'pending-cmds'}
]},
{title:'财务管理',children:[
{title:'结算记录',page:'settlement'},
{title:'提现管理',page:'withdraw-manage'},
{title:'平台流水',page:'platform-flow'},
{title:'资金流水',page:'fund-flow'}
]},
{title:'查询统计',children:[
{title:'综合查询',page:'query-all'},
{title:'网点查询',page:'company-list'}
]},
{title:'用户权限',children:[
{title:'用户管理',page:'user-manage'},
{title:'角色权限',page:'role-manage'},
{title:'黑名单',page:'blacklist'},
{title:'数据重置',page:'data-reset'}
]},
{title:'APK管理',children:[
{title:'APK版本',page:'apk-version'}
]}
],"""
    
    content = content.replace(old_menu, new_menu)
    
    # 7. 添加watch中的menu handler（找到现有的switch case，添加P2页面的case）
    # 查找现有case并添加新的
    old_switch_end = """case 'system-settings':this.loadSettings();break;
}"""
    
    new_switch_end = """case 'system-settings':this.loadSettings();break;
case 'remote-open-logs':this.loadRemoteOpenLogs(1);break;
case 'device-logs':this.loadDeviceLogs('');break;
case 'door-records':this.loadDoorRecords(1);break;
case 'pending-cmds':this.loadPendingCmds();break;
}"""
    
    content = content.replace(old_switch_end, new_switch_end)
    
    # 8. 添加data属性（remoteOpenFilter, deviceLogFilter, doorRecordFilter, cancelCmd）
    old_data_end = """pendingCmds:[],
pendingCmdsTotal:0,"""
    
    new_data_end = """pendingCmds:[],
pendingCmdsTotal:0,
remoteOpenFilter:'',
deviceLogFilter:'',
doorRecordFilter:'',"""
    
    content = content.replace(old_data_end, new_data_end)
    
    # 9. 添加cancelCmd方法（在loadPendingCmds之后）
    old_pending_cmds = """loadPendingCmds:function(){var self=this;this.api('/admin/pending-cmds').then(function(d){self.pendingCmds=d.data&&d.data.list||[];self.pendingCmdsTotal=d.data&&d.data.total||0;}).catch(function(){});},"""
    
    new_pending_cmds = """loadPendingCmds:function(){var self=this;this.api('/admin/pending-cmds').then(function(d){self.pendingCmds=d.data&&d.data.list||[];self.pendingCmdsTotal=d.data&&d.data.total||0;}).catch(function(){});},
cancelCmd:function(cmdId){var self=this;this.confirm2('确定取消该命令%s',function(){self.api('/admin/cancel-cmd',{id:cmdId}).then(function(d){self.toast(d.message||'操作成功');self.loadPendingCmds();}).catch(function(e){self.toast(e.message||'操作失败','error');});});},"""
    
    content = content.replace(old_pending_cmds, new_pending_cmds)
    
    # 写回文件
    with open('/home/ubuntu/smart-locker/static/admin-v2.html', 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("修复完成！")
    
    # 检查是否还有backtick
    if 'template:`' in content:
        print("警告: 还有未修复的backtick模板")
        import subprocess
        result = subprocess.run(['grep', '-n', 'template:`', '/home/ubuntu/smart-locker/static/admin-v2.html'], capture_output=True, text=True)
        print(result.stdout)
    else:
        print("所有backtick模板已修复")

if __name__ == '__main__':
    fix_admin_v2()
