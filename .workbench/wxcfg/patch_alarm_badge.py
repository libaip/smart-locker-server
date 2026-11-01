#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S188 2026-09-15] 补丁 2: 后台首页加"未处理告警数"红点.

背景: 提现退款失败告警(withdraw_refund_failed)已积压 849 条没人处理, 后台完全看不到提醒.
做法:
  1. routes/admin_v2.py 新增 /alarms/pending-count 接口(未处理告警数)
  2. templates/admin-v2.html: 左侧菜单"告警管理/告警记录"挂红色数字角标
     - 菜单数据里给这两项打 alarmBadge:true 标记(避免在 JS 单引号模板串里写转义引号)
     - 页面加载时拉一次, 之后每 60 秒刷新一次; 处理完一条告警后立即刷新
"""
import io, os, shutil, sys, time

ROOT = '/home/ubuntu/smart-locker'
STAMP = time.strftime('%Y%m%d_%H%M%S')


def load(p):
    return io.open(os.path.join(ROOT, p), encoding='utf-8').read()


def save(p, s):
    io.open(os.path.join(ROOT, p), 'w', encoding='utf-8').write(s)


def rep(p, old, new, cnt=1, tag=''):
    s = load(p)
    n = s.count(old)
    if n != cnt:
        print('❌ [%s] %s 匹配 %d 次(期望 %d), 已中止' % (tag, p, n, cnt))
        sys.exit(1)
    save(p, s.replace(old, new))
    print('✅ [%s] %s 替换 %d 处' % (tag, p, n))


for f in ('routes/admin_v2.py', 'templates/admin-v2.html'):
    src = os.path.join(ROOT, f)
    shutil.copy2(src, src + '.bak_s188_' + STAMP)
print('已备份 2 个文件 (.bak_s188_%s)' % STAMP)

# ---------------------------------------------------------------- 1) 新接口
ROUTE = '''@bp.route('/alarms/pending-count', methods=['GET'])
@require_auth
def alarms_pending_count():
    """[S188 2026-09-15] 未处理告警数(后台首页红点用).

    提现退款失败告警(withdraw_refund_failed)曾积压 849 条无人处理, 后台没有任何提醒,
    这里给首页/菜单提供一个未处理计数, 前端做红色角标.
    """
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM alarms WHERE status='0'")
        row = c.fetchone()
        conn.close()
        total = int(row[0]) if row and row[0] is not None else 0
        return json_response(data={'count': total})
    except Exception as e:
        logger.error(f'[alarms_pending_count] {e}')
        return json_response(message=str(e), code=500)


'''
rep('routes/admin_v2.py', "@bp.route('/alarms/list', methods=['GET', 'POST'])",
    ROUTE + "@bp.route('/alarms/list', methods=['GET', 'POST'])", tag='route')

# ---------------------------------------------------------------- 2) 样式
rep('templates/admin-v2.html',
    '.menu-item.active{color:#fff;background:#667eea}',
    '.menu-item.active{color:#fff;background:#667eea}\n'
    '.menu-item .menu-badge{display:inline-block;min-width:18px;height:18px;line-height:18px;padding:0 5px;margin-left:6px;'
    'border-radius:9px;background:#f56c6c;color:#fff;font-size:11px;text-align:center;vertical-align:middle;font-weight:600}\n'
    '.shrink .menu-item .menu-badge{display:none}',
    tag='css')

# ---------------------------------------------------------------- 3) 菜单数据打标记
rep('templates/admin-v2.html', "{key:'alert-mgr',label:'告警管理'}", "{key:'alert-mgr',label:'告警管理',alarmBadge:true}", tag='menu1')
rep('templates/admin-v2.html', "{key:'alarm-record',label:'告警记录'}", "{key:'alarm-record',label:'告警记录',alarmBadge:true}", tag='menu2')

# ---------------------------------------------------------------- 4) 菜单角标(模板串里只用双引号, 避免 JS 单引号串转义)
_old_badge = '<span class="menu-item-text">{{it.label}}</span>'
_new_badge = _old_badge + '<span v-if="it.alarmBadge&&$parent.alarmPending>0" class="menu-badge">{{$parent.alarmPending}}</span>'
rep('templates/admin-v2.html', _old_badge, _new_badge, cnt=1, tag='badge')

# ---------------------------------------------------------------- 5) data
rep('templates/admin-v2.html',
    "adminUser:'管理员',openGroups:{},menuGroupsData:[],",
    "adminUser:'管理员',openGroups:{},menuGroupsData:[],alarmPending:0,",
    tag='data')

# ---------------------------------------------------------------- 6) 方法
METHOD = ("loadAlarmPending:function(){var self=this;this.api('/alarms/pending-count',{},'GET')"
          ".then(function(d){self.alarmPending=(d.data&&d.data.count)||0;}).catch(function(){});},\n")
rep('templates/admin-v2.html', 'loadAlarms:function(p){', METHOD + 'loadAlarms:function(p){', tag='method')

# ---------------------------------------------------------------- 7) 处理告警后刷新
rep('templates/admin-v2.html',
    "resolveAlarm:function(id){var self=this;this.api('/alarms/resolve',{id:id}).then(function(){self.toast('处理成功');self.loadAlarms();})",
    "resolveAlarm:function(id){var self=this;this.api('/alarms/resolve',{id:id}).then(function(){self.toast('处理成功');self.loadAlarms();self.loadAlarmPending();})",
    tag='resolve')

# ---------------------------------------------------------------- 8) 加载 + 定时刷新
rep('templates/admin-v2.html',
    "var og={};menuGroupsData.forEach(function(g){og[g.key]=g.defaultOpen;});this.openGroups=og;this.menuGroupsData=menuGroupsData;",
    "var og={};menuGroupsData.forEach(function(g){og[g.key]=g.defaultOpen;});this.openGroups=og;this.menuGroupsData=menuGroupsData;\n"
    "this.loadAlarmPending();setInterval(function(){app.loadAlarmPending();},60000);",
    tag='mounted')

print('🎉 补丁 2 完成')

