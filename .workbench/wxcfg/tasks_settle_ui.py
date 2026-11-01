#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S226 2026-09-16] TASKS.md 记账: 结算页自动带未结/需重算月份."""
import io, shutil, time

T = '/home/ubuntu/smart-locker/.workbench/TASKS.md'
bak = T + '.bak_s226d_' + time.strftime('%Y%m%d_%H%M%S')
shutil.copy2(T, bak)

DESC = (
 "【老板: 结算时上月订单在本月退款怎么算 -> 已改页面】一、口径(读代码确认): 代理商月度结算按【下单时间 created_at】把订单归到某个月, "
 "退款额取订单【当前】的 refund_amount(只要 refund_time 非空就扣, 不管退款发生在哪个月) => 上月下单、本月退款的订单, 退款记在【上个月】那张账上; "
 "上月已落账时重算会变小 => 本次应结 delta 变负数 => 确认结算时自动挂账(carry_after=min(0,net)), 下次结算抵扣(payable=max(0,carry+delta)). "
 "公式: 押金-退款=余额; 余额×0.6%=手续费; 押金×5%=平台抽成(按全额押金算, 退款不退抽成); 收费收益单算; 本月应结=余额-手续费-抽成+收费应结; 本次应结=本月应结-已结算. "
 "二、发现的坑(老板确认后已修): 结算只重算【选中的月份】, 原来页面默认只选上个月一个月, 若只结当月则上月订单在本月退的押金永远扣不回来 = 平台持续多付; "
 "实测(只算不动钱, 复算 2026-07/08/09): 应【扣回】2949.97 (王昊 2417.88 = 7月-102.38+8月-2315.50; 刘共同 452.05; 刘 39.76; 葛 40.28; 陈 0), "
 "应【补付】51020.89 (夏峰 26631.20、共同 22918.98、金马考场 1470.71 —— 这 3 家 7/8 月从未结算过), 9 月未结应结 259394.56; "
 "只结9月 vs 连7/8一起结 相差 48070.92. 存量结算记录仅 8 条、负数 0 条、挂账全 0 => \"追溯扣回+挂账\"机制至今从未被触发过. "
 "三、改动(纯前端, 不碰后端/不碰钱): static/admin-v2.html ① created 改调 autoPickRange ② 新增 autoPickRange: 先按【近3个月(不含本月)】试算, "
 "把 本次应结(delta)!=0 的月份自动选进区间(没结过的补付、结过又有退款的重算扣回) ③ 页面加橙色提示, 说明区间内所有月份都会一起落账打款、负数如何挂账抵扣. "
 "四、部署: 106 改(备份 static/admin-v2.html.bak_s226_20260916_083149) -> 抽出内联 script(183077字符) node --check 通过 -> "
 "同步 175(备份 backups/admin-v2-static.html.bak_s226_20260916_083238) -> 两端 md5 40a8e8318a9d5b9084ac7b3a82c43feb -> "
 "/admin-v2 页面 5001+5002 均含 autoPickRange 与新提示 -> /api/health 均 200; 静态文件无需重载. "
 "五、未做: 未落账、未打款、未改任何数据. "
 "六、回滚: 175 上 cp -a backups/admin-v2-static.html.bak_s226_20260916_083238 static/admin-v2.html. "
 "七、待老板操作: 自己打开【统计分析-代理商分成结算】核对自动选中的月份与金额, 确认无误再点【确认结算】(真给钱、不可撤销)."
)

lines = io.open(T, encoding='utf-8').read().split('\n')
row = "| T-%d | S226 | static/admin-v2.html | 已完成 | %s | %s | %s |" % (
    int(time.time()), time.strftime('%m-%d %H:%M'), time.strftime('%m-%d %H:%M'), DESC)
lines.append(row)
io.open(T, 'w', encoding='utf-8').write('\n'.join(lines))
print('已记账 T-%d; 备份 %s' % (int(time.time()), bak))

