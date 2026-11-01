#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S226 2026-09-15] TASKS.md 记账: T-1789480990 置已完成并补细节."""
import io, shutil, time, sys

T = '/home/ubuntu/smart-locker/.workbench/TASKS.md'
bak = T + '.bak_s227_' + time.strftime('%Y%m%d_%H%M%S')
shutil.copy2(T, bak)

NOTE = (
 "【问题·老板定位】会员提现/提现管理里手动点【审批通过】, 一张提现单含两笔订单时\"只退得动一笔\", "
 "剩下的要靠人工去订单管理补退(补退的退款单号是 RF+时间+订单号, 只有 routes/admin_v2.py:1072 那条订单退款路径会生成, "
 "与微信原路退款的 5030… 一眼可分). "
 "【根因】/admin/withdrawal/approve 的打包分支里, 对 order_ids 的每一笔都传 wd['payment_channel_id']"
 "(= 提现单上挂的那一笔订单的商户号), 而 helpers.do_real_refund 是\"传进来的商户号优先\""
 "(payment_channel_id or row['payment_channel_id']), 于是订单自己收款的商户号被覆盖 -> 微信以\"不是这个商户号收的钱\"拒退 -> "
 "只有该商户号的订单退得动. 队列/白名单/自动打款/投诉那几条路径传的都是每笔订单自己的商户号, 所以只有手动审批这条中招. "
 "【数据】合并提现单 6980 张, 其中 4218 张(60%)跨多个商户号; 跨商户号单里\"渠道与单子不匹配\"的订单共 4706 笔/88607.44元. "
 "只看审批通过(2)/异常(4)的单: 渠道不匹配且未原路退 338 笔/6372元, 其中余额明细 pending(用户看不到也提不出) 331 笔/6371.75元; "
 "被拒单(3)里另有 912 笔/17376.28元渠道不匹配未退(那部分本应退回余额, 未核). "
 "【改法·3处】① 循环里每笔订单用【自己收款的 payment_channel_id + 自己的 order_no】(为空才回退到单上那个); "
 "② orders.refund_amount 由 \"+\" 改成 GREATEST(do_real_refund 内部已写过一次, 再加会记成两倍, 并导致以后再退这笔时算出\"已退够\"直接跳过); "
 "③ 部分失败时写 withdrawal_records.error_msg + 插 alarms(type=withdraw_refund_failed), 后台红点可见. "
 "【验证】只读核对: 单97441 两笔订单分别解析到渠道 94/96, 单67635 三笔解析到 77/76/72(改前一律用单上那一笔的商户号); py_compile OK. "
 "【部署】claim T-1789480990/S227 -> deploy_check 按预期拦截(自己改的) -> --force 放行 -> 175 备份 backups/admin_v2.py.bak_s226_20260915_220345 "
 "-> rsync -> 两端 md5 均为 229d71d4ad99e4529f7c7b89b35aa1c7 -> 175 py_compile OK -> 无感重载 kill -HUP 1736960 1736981. "
 "【验收】smart-locker 与 smart-locker-admin 均 active; /api/health 5001+5002 均200; /api/admin/withdrawal/approve 未登录返回401(路由在); "
 "重载后无新增异常. "
 "【老板指示·本次不做】存量数据修复与补退名单不做(老板: 后面用户反应过来我手动处理). "
 "【回滚】175: cp -a backups/admin_v2.py.bak_s226_20260915_220345 routes/admin_v2.py && sudo kill -HUP 1736960 1736981; "
 "106 备份: routes/admin_v2.py.bak_s226_20260915_220315."
)

lines = io.open(T, encoding='utf-8').read().split('\n')
hit = 0
for i, l in enumerate(lines):
    if l.startswith('| T-1789480990 |'):
        parts = l.split('|')
        parts[4] = ' 已完成 '
        parts[6] = ' 09-15 22:06 '
        parts[7] = parts[7].rstrip() + ' ' + NOTE + ' '
        lines[i] = '|'.join(parts)
        hit += 1
io.open(T, 'w', encoding='utf-8').write('\n'.join(lines))
print('已更新 %d 行; 备份 %s' % (hit, bak))
if hit != 1:
    sys.exit(1)

