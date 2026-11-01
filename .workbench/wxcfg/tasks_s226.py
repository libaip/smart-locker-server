#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S188 2026-09-15] TASKS.md 记账: T-1789479727 置为已完成并补部署/验收细节."""
import io, shutil, time, sys

T = '/home/ubuntu/smart-locker/.workbench/TASKS.md'
bak = T + '.bak_s226_' + time.strftime('%Y%m%d_%H%M%S')
shutil.copy2(T, bak)

NOTE = (
 "【根因】投诉即时退款(routes/user.py _auto_process_self_complaint)与投诉调度器(routes/admin_v2.py 4 处)都是 "
 "`UPDATE withdrawal_records SET status=2 WHERE order_id=%s`, 而合并提现单的 order_id 只是批次里的第一个订单; "
 "于是只退了投诉指向的那一笔, 整张单却被标成已通过, 剩余押金既没退给用户、余额明细又被隐藏(pending), 用户看不到也提不出来. "
 "老板案例 18290383562: 40元合并单(订单127827+126822)只退了20却整张标通过, 第二笔要再投诉一次才退, 提现记录还虚高成60元. "
 "【改法】helpers.py 新增 settle_withdrawal_for_order(): 从包含该订单的待处理提现单里逐单扣减; 扣减后还有别的订单则保持待处理"
 "(同步金额/订单列表, 若移除的正好是单里的 order_id 则换成剩余首单), 全退完才置已通过; 并补一条该订单自己的已通过记录(幂等). "
 "4 处调用点全部改为调它; 另用文本 LIKE 预筛替代 order_ids::jsonb 强转, 避免脏数据抛异常到退款主流程. "
 "【验证】回滚级验证(autocommit=False 事务内造2条假单后整体 ROLLBACK, 生产库无残留): 合并单->matched=1 deducted=1 approved=0 inserted=True; "
 "单单->approved=1 且不补记录; 已结算订单重复调用->matched=0(幂等). "
 "【任务2·后台红点】新增 GET /api/alarms/pending-count(未处理告警数); static/admin-v2.html 左侧菜单 告警管理/告警记录 挂红色数字角标, "
 "加载时拉一次 + 每60秒刷新 + 处理完一条告警立即刷新. 注意: 后台页面由 app.py `send_from_directory('static','admin-v2.html')` 发出, "
 "templates/admin-v2.html 是没人用的旧副本(我初次改错, 已从备份还原). "
 "【部署】claim T-1789479727/S226 -> deploy_check 按预期拦截(是我自己改的) -> --force 放行 -> 175 备份 backups/*.bak_s188_20260915_214723 "
 "与 backups/admin-v2-static.html.bak_s188_20260915_214906 -> rsync -> 两端 md5 一致(helpers.py 06b0076f8dfe46560a0099a62ed1311a, "
 "routes/user.py 5ec4a5a31a5019e0b28e7b6db72a0ab0, routes/admin_v2.py 828d2f3c77ca30c72f9292f5b4c96261, static/admin-v2.html 0db373e5568b84b35775662395bd2116) "
 "-> 175 py_compile OK -> 无感重载 kill -HUP 1736960 1736981. "
 "【验收】smart-locker 与 smart-locker-admin 均 active, /api/health 5001+5002 均200; /api/alarms/pending-count 未登录返回 401(路由已注册, 非404); "
 "/admin-v2 页面含 menu-badge/alarmBadge/loadAlarmPending 各 3 处; 对外 https://locker.cqdyxl.com/admin-v2 可达; 重载后 15 分钟无新增异常; "
 "库内未处理告警 849 条(红点口径一致). "
 "【本次未做】849 条积压告警的批量重试(需先给微信支付商户号充值, 属运营动作); 历史 152 张已错标的合并单(168笔/3094.26元)的数据修复未做, 待老板定口径. "
 "【回滚】175: cp -a backups/helpers.py.bak_s188_20260915_214723 helpers.py; cp -a backups/user.py.bak_s188_20260915_214723 routes/user.py; "
 "cp -a backups/admin_v2.py.bak_s188_20260915_214723 routes/admin_v2.py; cp -a backups/admin-v2-static.html.bak_s188_20260915_214906 static/admin-v2.html; "
 "然后 sudo kill -HUP 1736960 1736981."
)

lines = io.open(T, encoding='utf-8').read().split('\n')
hit = 0
for i, l in enumerate(lines):
    if l.startswith('| T-1789479727 |'):
        parts = l.split('|')
        parts[3] = ' helpers.py,routes/user.py,routes/admin_v2.py,static/admin-v2.html '
        parts[4] = ' 已完成 '
        parts[6] = ' 09-15 21:55 '
        parts[7] = parts[7].rstrip() + ' ' + NOTE + ' '
        lines[i] = '|'.join(parts)
        hit += 1
io.open(T, 'w', encoding='utf-8').write('\n'.join(lines))
print('已更新 %d 行; 备份 %s' % (hit, bak))
if hit != 1:
    sys.exit(1)

