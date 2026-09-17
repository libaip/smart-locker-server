#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S231] 台账：换模板(押金退还->账户余额) + 存包落地页只请求一个订阅"""
import io, time, shutil

P = '.workbench/TASKS.md'
TS = time.strftime('%Y%m%d_%H%M%S')
shutil.copyfile(P, P + '.bak_s231_' + TS)
s = io.open(P, encoding='utf-8').read().split('\n')
last = None
for i, ln in enumerate(s):
    if ln.startswith('| T-'):
        last = i
if last is None:
    raise SystemExit('找不到表格行')

tid = int(time.time())
row = (
    '| T-' + str(tid) + ' | S231 | routes/user.py,helpers.py,static/deposit.html,wx_templates(DB) | 已完成 | 09-17 16:50 | 09-17 16:56 | '
    '【老板要求】(1) 小程序"押金退还通知"换成"账户余额通知"(模板ID ax-O5Qa05IWt7bbhRVk9Pb9A_SbXfIMfbhm0Hoh4gYc)，内容/变量不变；'
    '(2) 存包落地页(H5跳转过去那页)只请求一个订阅；提现页/小程序存包页仍请求两个。'
    '【关键发现·换模板零代码改动】直接问微信拿两个模板的原文对比：新"账户余额通知"关键词=amount1/time2/thing3/thing4，'
    '旧"押金退还通知"也是 amount1/time2/thing3/thing4 —— **四个变量名完全一样，只是标签文字不同**，所以只改配置中心一条记录即可，发送代码无需改。'
    '【改法1·换模板】175 生产库 UPDATE wx_templates SET template_id=新ID WHERE biz=subscribe_general AND channel=mp（旧值 PtRJgPDDeP_...已记档）。'
    '⚠️ 注意：106 的库是旧副本(08-23 后基本没写)，生产库在 175，SQL 必须在 175 上打(本次先在106上打了一次，无副作用，随后已在175补上)。'
    '【改法2·过渡期回退】微信订阅授权按模板ID记账，换ID后老用户只有旧模板授权→新模板必被拒(43101)。'
    '故在 helpers.py 的 send_wx_subscribe_message() 失败分支加：template_id==新账户余额模板时，用旧押金退还模板再发一次；'
    '并新增常量 _TPL_ACCOUNT_NEW / _TPL_DEPOSIT_OLD。过渡期结束后可删除该段。'
    '【改法3·存包落地页只请求一个】小程序的模板请求不带任何身份信息(线上 2852 次请求参数完全一样: 只有 ?_t=时间戳)，服务端无法按页面区分。'
    '做法：H5(static/deposit.html) 在跳转前用 navigator.sendBeacon 打一个"跳转意图"→ /api/user/mp-jump-intent(新增接口) 写 mp_enter_log(phase=jump_intent)；'
    '接口 /user/subscribe-templates 用一条原子 UPDATE...RETURNING 消费 6 秒内最新的一条意图：消费到=存包落地页→只回[账户余额]；否则回[退款成功,账户余额]。'
    'sendBeacon 由浏览器保证在页面卸载时也发出；万一没发成功，服务端按"非落地页"处理(回两条，与改动前一致)。'
    '【部署与校验】106 备份 backups/s231_20260917_165202/；三个补丁先对临时副本空跑看 diff；py_compile OK；'
    '新 md5: routes/user.py=ccba415108e8b6932acf60ef5b32166c / helpers.py=9c4df5fe45e24209787ef9579c65705b / static/deposit.html=3102762f904a557eac6dc367dccb9348；175 同步后三处 md5 一致；'
    '零停机重载两个服务：MainPID 1736960/1736981 未变、active/active、首页与 /api/health 均 200、重载后 0 条 traceback。'
    '【功能自测·通过】POST 意图 → 下一次 GET 模板返回 landing=true 且只有 1 条(账户余额)；再 GET 返回 landing=false 且 2 条；测试数据已清理(TEST231 残留 0)；'
    '接口返回的 general_notify 已是新模板ID(证明配置中心与解析都对)。'
    '【踩坑·两条】① 175 的"改前备份"第一次因 PowerShell 吞掉 $(date) 而失败，当时文件已上传→无备份，随后手工重建了干净回滚点(见下)并逐一核对 md5；'
    '② 重载后 7 秒内自测会拿到旧响应(gunicorn 优雅重载期间老 worker 仍在接请求)，要等新 worker 完全接管(~10-20秒)再验。'
    '【回滚】(a) 模板：UPDATE wx_templates SET template_id=PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA WHERE biz=subscribe_general AND channel=mp; '
    '(b) 代码：cp -a /home/ubuntu/smart-locker/backups/s231_rollback_20260917/{user.py,helpers.py,deposit.html}.bak 对应位置 && sudo kill -HUP 1736960 1736981'
    '（回滚点 md5: user.py=2067f844… / helpers.py=06b0076f… / deposit.html=2773443b…，均已核对） |'
)

s[last + 1:last + 1] = [row]
io.open(P, 'w', encoding='utf-8', newline='').write('\n'.join(s))
print('已追加 1 行: T-%d (S231 已完成)' % tid)
