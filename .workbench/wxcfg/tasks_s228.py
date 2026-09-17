#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S228] 往 .workbench/TASKS.md 追加两条：本次修复（已完成）+ 明天验收指标（待办）"""
import io, time, shutil

P = '.workbench/TASKS.md'
TS = time.strftime('%Y%m%d_%H%M%S')
shutil.copyfile(P, P + '.bak_iosjump_' + TS)
s = io.open(P, encoding='utf-8').read().split('\n')

last = None
for i, ln in enumerate(s):
    if ln.startswith('| T-'):
        last = i
if last is None:
    raise SystemExit('找不到表格行')
print('最后一行表格在第 %d 行: %s' % (last + 1, s[last][:60]))

tid = int(time.time())

row1 = (
    '| T-' + str(tid) + ' | S228 | static/deposit.html | 已完成 | 09-17 13:39 | 09-17 13:45 | '
    '【老板报的现象】iPhone 扫码存包: 跳小程序→订阅成功→却回到「设置手机号」页 + 弹「请跳转小程序进行下一步」→ 再点一次才到支付页。'
    '【根因·H5 拿错信号猜「跳没跳成」】deposit.html 里 9-12 加的「2 秒兜底」: 跳出去 2 秒后只要 document.hidden===false 就判「没跳过去」→ '
    'showMpTipModal()+goToStoreStep1()(退回设置页); 而 iPhone(WKWebView)跳走小程序后 H5 仍报「前台可见」, 这句判断在 iPhone 上几乎必然误报——'
    '用户人在小程序里正常订阅, H5 在背后已把他判成「跳失败」并退回设置页; 等他返回只能再点一次, 第二次才从服务端 /api/user/mp-entered 拿到「进过」→放行。'
    '【证据·9-17 全天 1,300 单实测】(1) 需「进两次小程序」的订单: iPhone 366/423=86.5%, 安卓 66/821=8.0%, 其他 2/56=3.6%; '
    '(2) 「订单创建→小程序点返回」耗时中位数 iPhone 11 秒/安卓 13 秒, **2 秒内返回的 0 单** → 那句 2 秒判断对 iPhone 订单必然误报; '
    '(3) 当天 11 人点了「网页支付」逃生按钮(tries=2/3), 其中一人同一订单来回跳 3 次; '
    '(4) 反证「新用户专属」不成立: iPhone 新客 295/340=86.8% vs 老客 71/83=85.5%, 安卓新客 6.5% vs 老客 14.4%; '
    '(5) mp_enter_log 的上报时刻=用户在小程序点「返回网页」那一刻(subscribe.js onBackToH5), **不是**进入小程序——人还在小程序里时服务端必然答「没进过」, 更证明不能拿固定时间点猜。'
    '【改法·只动 H5 一个文件·5 处】(1)(2) 删掉 mpAutoJump / launchMp 里那两处 2 秒误判兜底(全文 document.hidden===false 残留 0); '
    '(3) 新增 mpStartEnterPoll(): 跳出去后每 2 秒问一次 /api/user/mp-entered, 服务端答「进过」→立即 goToStep2Direct() 进支付页, 3 分钟自动停; '
    '60 秒仍无进店记录才兜底弹一次提示(埋点 mp_failsafe_gate); (4) goToStep2Direct() 停轮询; (5) reserveDoorAndProceed() 新单开始停轮询。'
    '**A1 规矩不变**: 真取消微信「即将打开小程序」的人(服务端查无进店记录)照旧被拦回设置页; A1-b 逃生按钮/次数上限(mp_jump_max_retry=3)原样保留。'
    '【部署与校验】106 备份 backups/iosjump_s228_20260917_133931/deposit.html.bak(旧 md5 5d2c6522701cfa057a7fe19c15ff37be / 158156 字节)→ '
    '补丁 .workbench/wxcfg/patch_ios_jump_s228.py(精确匹配+断言唯一, 先对临时副本空跑看 diff)→新 md5 d16df301371412d1e66a88632c57b475 / 161370 字节; '
    '175 备份 backups/iosjump_s228_20260917_133955/deposit.html.bak 后同步; **三处(106/175/本地)md5 完全一致**; '
    '静态页无需重启: curl 线上 / 得到 mpStartEnterPoll×3、旧判断 0 处、大小 161370(与磁盘一致), app.py 的 / 与 /store SSR 均每次请求实时读盘; '
    '主脚本块(94,195 字符)JS 语法检查通过, 改前改后一致(唯一报错的 script#3 是 type=text/wxtag-template 微信模板标签, 非 JS)。'
    '【回滚】cp -a /home/ubuntu/smart-locker/backups/iosjump_s228_20260917_133955/deposit.html.bak /home/ubuntu/smart-locker/static/deposit.html (175; 立即生效无需重启) |'
)

row2 = (
    '| T-' + str(tid + 1) + ' | S228 | (观察项) | 待办 | 09-17 13:45 | | '
    '【明天验收 iPhone 跳转修复(T-' + str(tid) + ')的四个数】(a) iPhone「进两次小程序」比例应从 86.5% 降到接近安卓水平(现 8%); '
    '(b) 新增埋点 mp_enter_poll_ok 条数应≈iPhone 订单数(说明轮询接管了放行); '
    '(c) mp_gate_escape(逃生按钮)应显著少于今天的 11 条; '
    '(d) **红线**: 不能出现「取消跳转却进了支付页」——抽查当日投诉/用户反馈与 mp_failsafe_gate 条数。'
    '查法: 当日 journalctl -u smart-locker 里 mp_exit_log 按 order_id 统计 attempt 次数(分平台), 以及 oa_subscribe_log 里 mp_enter_poll_ok / mp_failsafe_gate / mp_gate_escape。 |'
)

s[last + 1:last + 1] = [row1, row2]
io.open(P, 'w', encoding='utf-8', newline='').write('\n'.join(s))
print('已追加 2 行: T-%d(已完成) / T-%d(待办)' % (tid, tid + 1))
print('台账备份: %s.bak_iosjump_%s' % (P, TS))
