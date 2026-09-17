#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S229] 往 .workbench/TASKS.md 追加一条：补掉 iPhone 跳转剩余两个口子"""
import io, time, shutil

P = '.workbench/TASKS.md'
TS = time.strftime('%Y%m%d_%H%M%S')
shutil.copyfile(P, P + '.bak_s229_' + TS)
s = io.open(P, encoding='utf-8').read().split('\n')
last = None
for i, ln in enumerate(s):
    if ln.startswith('| T-'):
        last = i
if last is None:
    raise SystemExit('找不到表格行')

tid = int(time.time())
row = (
    '| T-' + str(tid) + ' | S229 | routes/user.py,static/deposit.html | 已完成 | 09-17 14:10 | 09-17 14:15 | '
    '【补 S228(T-1789623714) 剩下的两个口子 —— 老板质疑"为什么要改小程序"，查实**不用改小程序**】'
    '【背景】老板问：11 次"用网页支付"逃生按钮、以及那个来回跳 3 次的，是不是同一个病根？逐个对账结果：'
    '8 个逃生用户里 **7 个其实已经进了小程序**，最典型 19812040456：12:39:00 进小程序、12:39:37 被 H5 判成"没进过"→ 逃生；'
    '只有 1 个（18861426289）真没进去（取消了微信"即将打开小程序"）—— 那是拦截门正常干活。'
    '【关键发现·不用动小程序】小程序 subscribe 页 onLoad 就会调 /user/link-mp-openid（带 order_id）报信"我进来了"，'
    '今天 1,943 次；但 H5 判"进没进小程序"只查 mp_enter_log，而那张表原先只有用户在小程序里点「返回网页」时（mp-exit-log）才写 —— '
    '于是"进去了但用 叉/返回键关掉小程序"的人在 H5 眼里等于"没进过"→ 弹回设置页 + 挂提示 → 反复点 → 逃生。这是我的漏，不是小程序的问题。'
    '【改法1·后端 routes/user.py】在 link_mp_openid_from_mini() 落库阶段补写一条 mp_enter_log(phase=mp_page)，'
    '用独立连接写（失败只告警，不污染主事务、不影响绑定主流程）；H5 的 /api/user/mp-entered 只查"有无记录"，立刻认账。'
    '不影响统计口径（统计脚本按 phase=attempt 计数）。'
    '【改法2·H5 static/deposit.html】S228 的轮询有个自杀条件 `if (mpOpenedTime === 0) 停表`，'
    '而 resumeFromMpJump() 在用户带回 H5 时正好把 mpOpenedTime 清零 → 轮询当场被掐死，只剩 mpProceedOrGate"问 2 次、间隔 1.5 秒"，'
    'iPhone 上报晚一两秒就两次全空 → 仍误拦（铁证：老板测试号 18888889999 的订单 131116 跳了 2 次且埋点报 sec=3，说明第一次轮询已被掐死）。'
    '改：删掉那个自杀条件（只认"已放行/已到支付页/新单/3 分钟超时"显式停），并把询问从 2 次加到 4 次、兜底放行 6 秒→9 秒。'
    '**A1 规矩不变**：真取消跳转（小程序页面根本没打开、无任何记录）的仍被拦回设置页；逃生阀原样保留。'
    '【部署与校验】106 备份 backups/iosjump_s229_20260917_141019/、175 备份 backups/iosjump_s229_20260917_141035/；'
    '先对临时副本空跑看 diff；user.py 5ec4a5a31a5019e0b28e7b6db72a0ab0 → **d9461d9be2ec64e6727958d7cf53de2c**（py_compile OK），'
    'deposit.html d16df301371412d1e66a88632c57b475 → **2773443b6c1901a4161d817cf123f899**；'
    '**106/175/本地三处 md5 完全一致**；主脚本块(94,553 字符) JS 语法检查通过；'
    '零停机重载两个服务（铁律3）：主进程 1736960/1736981 **未变**、is-active=active/active、首页 200、/api/health 200、'
    '重载后 60 秒 0 条 traceback/exception；**功能实证：14:12:50 出现第一条真实 mp_page 记录（订单 131149），说明小程序一进页面就被认账了**。'
    '【过程中的小插曲】上传 user.py 到 175 时 scp 超时一次（网络抖动），重传后 md5 一致；期间 H5 已上线、后端仍是旧版，'
    '属"半上线"但无风险（判定更严，不会误放行）；已补齐。'
    '【回滚】cp -a /home/ubuntu/smart-locker/backups/iosjump_s229_20260917_141035/user.py.bak /home/ubuntu/smart-locker/routes/user.py && '
    'cp -a /home/ubuntu/smart-locker/backups/iosjump_s229_20260917_141035/deposit.html.bak /home/ubuntu/smart-locker/static/deposit.html && '
    'sudo kill -HUP 1736960 1736981 (175) |'
)

s[last + 1:last + 1] = [row]
io.open(P, 'w', encoding='utf-8', newline='').write('\n'.join(s))
print('已追加 1 行: T-%d (S229 已完成)' % tid)
print('台账备份: %s.bak_s229_%s' % (P, TS))
