#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S232] 台账：提现页/其它位置也只要一个订阅（退款成功）"""
import io, time, shutil

P = '.workbench/TASKS.md'
TS = time.strftime('%Y%m%d_%H%M%S')
shutil.copyfile(P, P + '.bak_s232_' + TS)
s = io.open(P, encoding='utf-8').read().split('\n')
last = None
for i, ln in enumerate(s):
    if ln.startswith('| T-'):
        last = i
if last is None:
    raise SystemExit('找不到表格行')

tid = int(time.time())
row = (
    '| T-' + str(tid) + ' | S232 | routes/user.py | 已完成 | 09-17 17:00 | 09-17 17:02 | '
    '【老板要求 + 依据】老板实测"一次弹窗放两个订阅，用户往往只勾一个，另一个就再也拿不到额度"。'
    '数据支持：今天小程序通道发送成功 1,430 次、**43101(没额度) 1,270 次**（近一半失败）。'
    '故把两个模板拆到各自"该用它"的时刻：存包落地页只问「账户余额」(S231 已上线)，提现页/其它只问「退款成功」。'
    '【改法·一行】routes/user.py 的 get_subscribe_templates(): '
    '`_tpls = [_general] if _landing else [_withdraw, _general]` -> `... else [_withdraw]`（+注释）。'
    '【安全网·关键】小程序存包页(deposit.js)读的是具名字段 deposit_notify/general_notify/withdraw_notify，'
    '仍是 2 条不同模板(账户余额+退款成功) → 万一真有人在小程序内直接存包(不经过 H5 扫码落地页)，他照样拿得到「账户余额」授权。'
    '实测依据：14:12 之后付款的 72 笔订单 100% 都有"进过小程序落地页"的记录，说明付款用户都经过落地页；'
    '该改动的暴露面仅是"小程序内直接存包"，而具名字段已兜住。'
    '【部署与校验】106 备份 backups/s232_rollback_20260917/user.py.bak(md5 ccba415108e8b6932acf60ef5b32166c)；'
    '先对临时副本空跑看 diff(+237 字符，只有那一处)；py_compile OK；新 md5 **0b745b1ae5960dadbe7372ab2633c7b4**；'
    '175 先备份(顺序正确)再上传，三处 md5 一致；零停机重载两个服务：MainPID 1736960/1736981 未变、active/active、首页与 /api/health 200、0 条 traceback。'
    '【功能自测·通过】① 存包落地页(打过招呼) → landing=True、**1 条=账户余额**；'
    '② 提现页/其它 → landing=False、**1 条=退款成功**；③ 具名字段去重后仍是 **2 个模板**(安全网)；'
    '测试数据已清理(TEST232 残留 0)。另：真实用户已产生 jump_intent（说明 S231 的 H5 打招呼在线上正常工作）。'
    '【教训沿用】重载后必须等新 worker 完全接管(本次等 25 秒)再自测，否则会拿到旧响应。'
    '【回滚】cp -a /home/ubuntu/smart-locker/backups/s232_rollback_20260917/user.py.bak /home/ubuntu/smart-locker/routes/user.py && sudo kill -HUP 1736960 1736981 |'
)

s[last + 1:last + 1] = [row]
io.open(P, 'w', encoding='utf-8', newline='').write('\n'.join(s))
print('已追加 1 行: T-%d (S232 已完成)' % tid)
