#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S233] 台账：修判定缺陷（跳转意图不算已进入）+ 加埋点定位剩余弹层"""
import io, time, shutil

P = '.workbench/TASKS.md'
TS = time.strftime('%Y%m%d_%H%M%S')
shutil.copyfile(P, P + '.bak_s233_' + TS)
s = io.open(P, encoding='utf-8').read().split('\n')
last = None
for i, ln in enumerate(s):
    if ln.startswith('| T-'):
        last = i
if last is None:
    raise SystemExit('找不到表格行')

tid = int(time.time())
row = (
    '| T-' + str(tid) + ' | S233 | routes/user.py,static/deposit.html | 已完成 | 09-18 07:55 | 09-18 08:05 | '
    '【起因】老板问"为什么还有少量(约5%)要跳两次，能不能根治"。'
    '【先查结论：老毛病确实没了】9-18 到 07:54：iPhone 要跳两次 5.2%(96单里5单)、安卓 4.9%，逃生按钮 0 次、60秒兜底拦截 0 次；'
    '9-17 整天还是 80.9% vs 7.8%。逐单拉时间线(131242/131272/131290/131340/131409)：每单都进了小程序2~3次、每次都有正常的"返回网页"记录，'
    'H5侧 0 逃生 / 0 兜底 -> 不是被系统弹回去，而是"提示层又多弹了一次"(只有弹层里的按钮才会产生新的跳转意图)，4/5 单仍正常付款。'
    '【查出并修掉一个我自己引入的缺陷(S231)】H5 的"打招呼"写的是 mp_enter_log(phase=jump_intent)，而"他进没进小程序"的判定'
    '(GET /user/mp-entered)是查这张表里有没有该订单的记录 -> 把我自己写的意图也当成了"已进入"。'
    '后果：用户在微信"即将打开小程序"上点取消(根本没进去)，H5 也会放行到支付页，**削弱了 A1 拦截**。'
    '修法：查询加 "AND phase NOT IN (\'jump_intent\',\'jump_intent_used\')"。'
    '验证：只写一条 jump_intent -> entered=false ✅；再补一条 mp_page -> entered=true ✅（测试数据已清理）。'
    '【加埋点定位剩余 5%】现有数据只能看到结果、看不到"弹层为什么弹"，故加 4 个上报(都进 oa_subscribe_log，不改任何逻辑)：'
    '(1) mp_modal_show + reason(no_scheme/gate/failsafe/test)；'
    '(2) mp_gate_no + asks(每次询问结果串: 1=进过 0=没进过 E=网络错 N=订单号为空) + oid；'
    '(3) mp_poll_first + res(轮询第一次问到的结果)；'
    '(4) mp_poll_no_oid(轮询时订单号取不到，只报一次)；并给所有上报带上 oid。'
    '【部署与校验】106 备份 backups/s233_rollback_20260917/；先对临时副本空跑看 diff；py_compile OK；'
    '新 md5: routes/user.py=**a3af8d3c70cc6630280cdac3a454c2cf**、static/deposit.html=**94e08a851a5c2ac6916080069bcda04d**；'
    '175 同步后 md5 一致；零停机重载两个服务后等 25 秒再验证(避免拿到老 worker 的旧响应)：active/active、首页与 /api/health 200、0 条 traceback。'
    '【踩的两个坑】① scp 静默失败一次，补丁脚本没传上去就执行 -> 报"文件不存在"，重传+md5核对后成功；'
    '② 175 的改前备份又被部署脚本覆盖成了新文件(顺序问题，第二次犯)，已用 106 上的正确备份重建回滚点并逐一核对md5。'
    '【明天看什么】mp_modal_show 的 reason 分布 + mp_gate_no 的 asks 串 + mp_poll_first 的 res -> 就能定位"多余的弹层"到底哪条路弹的，再精准修。'
    '【回滚】cp -a /home/ubuntu/smart-locker/backups/s233_rollback_20260917/{user.py,deposit.html}.bak 对应位置 && sudo kill -HUP 1736960 1736981'
    '（回滚点 md5: user.py=0b745b1ae5960dadbe7372ab2633c7b4 / deposit.html=3102762f904a557eac6dc367dccb9348） |'
)

s[last + 1:last + 1] = [row]
io.open(P, 'w', encoding='utf-8', newline='').write('\n'.join(s))
print('已追加 1 行: T-%d (S233 已完成)' % tid)
