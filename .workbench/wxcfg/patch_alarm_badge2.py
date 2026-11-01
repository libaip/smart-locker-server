#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S188 2026-09-15] 补丁 2b: 红点改到真正的线上文件 static/admin-v2.html, 并撤销之前改错的 templates 副本.

原补丁改的是 templates/admin-v2.html(252KB, 8月17日), 但后台页面实际由 app.py:433/440
`send_from_directory('static', 'admin-v2.html')` 发出(278KB, 9月13日) -> 改错文件, 页面看不到红点.
本脚本: ① 从备份还原 templates/admin-v2.html; ② 把红点改动打到 static/admin-v2.html.
"""
import io, os, shutil, sys, time, glob

ROOT = '/home/ubuntu/smart-locker'
STAMP = time.strftime('%Y%m%d_%H%M%S')
P = 'static/admin-v2.html'


def rep(p, old, new, cnt=1, tag=''):
    path = os.path.join(ROOT, p)
    s = io.open(path, encoding='utf-8').read()
    n = s.count(old)
    if n != cnt:
        print('❌ [%s] %s 匹配 %d 次(期望 %d), 已中止' % (tag, p, n, cnt))
        sys.exit(1)
    io.open(path, 'w', encoding='utf-8').write(s.replace(old, new))
    print('✅ [%s] %s 替换 %d 处' % (tag, p, n))


# ---------------- ① 还原改错的 templates 副本 ----------------
baks = sorted(glob.glob(os.path.join(ROOT, 'templates/admin-v2.html.bak_s188_*')))
if baks:
    shutil.copy2(baks[-1], os.path.join(ROOT, 'templates/admin-v2.html'))
    print('↩️  已从 %s 还原 templates/admin-v2.html' % os.path.basename(baks[-1]))
else:
    print('⚠️  找不到 templates/admin-v2.html 的备份, 未还原')

# ---------------- ② 给线上文件打补丁 ----------------
src = os.path.join(ROOT, P)
shutil.copy2(src, src + '.bak_s188b_' + STAMP)
print('已备份 %s.bak_s188b_%s' % (P, STAMP))

rep(P, '.menu-item.active{color:#fff;background:#667eea}',
    '.menu-item.active{color:#fff;background:#667eea}\n'
    '.menu-item .menu-badge{display:inline-block;min-width:18px;height:18px;line-height:18px;padding:0 5px;margin-left:6px;'
    'border-radius:9px;background:#f56c6c;color:#fff;font-size:11px;text-align:center;vertical-align:middle;font-weight:600}\n'
    '.shrink .menu-item .menu-badge{display:none}',
    tag='css')

rep(P, "{key:'alert-mgr',label:'告警管理'}", "{key:'alert-mgr',label:'告警管理',alarmBadge:true}", tag='menu1')
rep(P, "{key:'alarm-record',label:'告警记录'}", "{key:'alarm-record',label:'告警记录',alarmBadge:true}", tag='menu2')

_old = '<span class="menu-item-text">{{it.label}}</span>'
_new = _old + '<span v-if="it.alarmBadge&&$parent.alarmPending>0" class="menu-badge">{{$parent.alarmPending}}</span>'
rep(P, _old, _new, tag='badge')

rep(P, "adminUser:'管理员',openGroups:{},menuGroupsData:[],",
    "adminUser:'管理员',openGroups:{},menuGroupsData:[],alarmPending:0,", tag='data')

METHOD = ("loadAlarmPending:function(){var self=this;this.api('/alarms/pending-count',{},'GET')"
          ".then(function(d){self.alarmPending=(d.data&&d.data.count)||0;}).catch(function(){});},\n")
rep(P, 'loadAlarms:function(p){', METHOD + 'loadAlarms:function(p){', tag='method')

rep(P,
    "resolveAlarm:function(id){var self=this;this.api('/alarms/resolve',{id:id}).then(function(){self.toast('处理成功');self.loadAlarms();})",
    "resolveAlarm:function(id){var self=this;this.api('/alarms/resolve',{id:id}).then(function(){self.toast('处理成功');self.loadAlarms();self.loadAlarmPending();})",
    tag='resolve')

rep(P,
    'var og={};menuGroupsData.forEach(function(g){og[g.key]=g.defaultOpen;});this.openGroups=og;this.menuGroupsData=menuGroupsData;',
    'var og={};menuGroupsData.forEach(function(g){og[g.key]=g.defaultOpen;});this.openGroups=og;this.menuGroupsData=menuGroupsData;\n'
    'this.loadAlarmPending();setInterval(function(){app.loadAlarmPending();},60000);',
    tag='mounted')

print('🎉 补丁 2b 完成')

