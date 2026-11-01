#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S226 2026-09-16] TASKS.md 记待办: 晚上换小程序订阅通知消息模板(附现状/风险/过渡方案)."""
import io, shutil, time, sys

T = '/home/ubuntu/smart-locker/.workbench/TASKS.md'
bak = T + '.bak_s226c_' + time.strftime('%Y%m%d_%H%M%S')
shutil.copy2(T, bak)

DESC = (
 "【待办·老板说「等晚上搞」】换小程序订阅通知消息模板. "
 "一、现状(已查证): 模板配置中心是活的 —— 库表 wx_templates 已有 7 条: 小程序 subscribe_deposit(寄存成功 Q3Fts5)/subscribe_general(押金退还 PtRJgP)/subscribe_refund(退款成功 lJpnAU), "
 "公众号 oa_deposit_ok/oa_deposit_end/oa_refund_ok/oa_withdraw_ok; 发送侧统一走 helpers._wx_tpl(biz,通道,硬编码兜底) 优先读表, set_template() 会清缓存 => 改表立即生效、无需重载. "
 "二、三处订阅弹窗(源码在 D:\\工具配置迁移包_20260811\\小程序_用户2, appid wxcabd4cbdb3096c4b): "
 "① pages/deposit/deposit.js 点【支付】时 ② pages/subscribe/subscribe.js(H5落地页) 点【确认信息】时 ③ pages/withdraw/withdraw.js 点【立即提现】时; 三处请求的是同一批 2 个模板, 列表由后端 /api/user/subscribe-templates 下发; "
 "小程序里另有写死的兜底 ID 共 12 处(deposit 3+2、subscribe 3、withdraw 2、utils/api.js 2), 仅在接口失败时使用. "
 "三、关键风险(必须记住): 订阅授权按【模板 ID】记账, 「总是保持以上选择」也是按模板记的 => 一旦换模板 ID, 老用户的额度+保持记录对新模板全部无效, 发出去就是 43101; "
 "且换的瞬间有通知空窗期(按昨天量级约 1600+ 条/天). 必须【三处同时改】: ①发送用的 wx_templates 表 ②弹窗请求用的 /api/user/subscribe-templates ③代码里的硬编码兜底; 缺一个就会出现「用户订阅的是A、我们发的是B」= 100% 失败. "
 "四、推荐的过渡方案(双模板并行): 弹窗同时请求新+旧模板(微信一次弹窗最多3个模板, 注意别超), 发送时先试新模板、43101 则回退旧模板 => 过渡期不丢通知; 等新模板授权率上来(看日志新模板成功数)再撤旧模板. "
 "五、零成本替代: 若只是想调整「发哪几条/弹窗请求哪几条」而非换模板本身, 只改后端 /api/user/subscribe-templates 一个函数即可, 不动模板 ID, 已授权用户不受影响、不用发小程序. "
 "六、晚上开工前需要老板确认: 换哪一个模板(押金退还/退款成功/寄存成功)、换成什么(新模板 ID)、为什么要换; 以及是否按双模板过渡做. "
 "七、顺带发现(本次未做, 建议一并考虑): 取件页 pages/retrieve/retrieve.js 完全没有订阅弹窗 —— 而「押金退还」通知正是在取包后退款那一刻发, 用户此时往往已无额度; "
 "昨天有 411 人「押金退还」通知被 43101 拒. 建议在小程序取件页(点【开门取物】时)和钱包/我的页各补一次订阅调用, 并在弹窗前加引导语请用户勾「总是保持以上选择」; 后端可加 /api/user/subscribe-status(依据 43101 记录)让前端只在没额度时才弹."
)

lines = io.open(T, encoding='utf-8').read().split('\n')
row = "| T-%d | S226 | wx_templates,routes/user.py | 待办 | %s | | %s |" % (int(time.time()), time.strftime('%m-%d %H:%M'), DESC)
lines.append(row)
io.open(T, 'w', encoding='utf-8').write('\n'.join(lines))
print('已追加待办 T-%d; 备份 %s' % (int(time.time()), bak))

