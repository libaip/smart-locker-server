#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S226 2026-09-15] TASKS.md 记待办: 公众号 access_token 被多进程互相顶掉导致兜底通道全天0成功."""
import io, shutil, time, sys

T = '/home/ubuntu/smart-locker/.workbench/TASKS.md'
bak = T + '.bak_s226b_' + time.strftime('%Y%m%d_%H%M%S')
shutil.copy2(T, bak)

DESC = (
 "【待办·老板要求「空了改」】(S226 2026-09-15 查证) 公众号兜底通道 access_token 被自己人反复顶掉, 全天0成功. "
 "现象: 今天(09-15) 公众号订阅通知兜底发送 688 次, 成功 0 次; 其中 497 次 errcode=40001"
 "(invalid credential, access_token is invalid or not latest), 184 次 43101(用户没订阅, 属正常). "
 "根因: 同一公众号同一时刻只有一把 access_token 有效(取新的, 旧的约几分钟后失效); 而我们取 token 的地方各自为政且缓存不共享: "
 "① helpers.get_oa_access_token() 是【纯进程内缓存】+ 老接口 /cgi-bin/token, 8+2 个 worker 各存一份; "
 "② H5签名(app.py ~853)/公众号带参二维码(app.py ~516)/查是否关注(routes/user.py:2489)/webhook按unionid查用户(routes/webhook.py:172) 各自再取; "
 "③ 每3分钟跑的 run_withdrawal_batch.py、每5分钟的 complaint_auto.py 等【每次都是新进程, 缓存等于没有】, 一发通知就取新的顶掉网页进程手里的. "
 "结果网页进程拿着已作废的 token 一直发失败, 且失败后不立即重取(要等 2 小时缓存过期), 一挂就是几小时. "
 "实测佐证: 半夜(业务空闲)取一把新 token, 每10秒探一次连续2分钟一直有效(40003 而非 40001); 而白天 40001 集中在 08:00-17:00 业务高峰. "
 "对比: 小程序那条用的是微信推荐的 stable_token + 数据库共享缓存(helpers.get_access_token), 全天 0 个 40001, 健康. "
 "影响: 白天这条兜底通道基本全废, 497 次本该能发的通知全丢; 受害最重的是「只有旧小程序 openid 但关注了公众号」那批人(小程序发不到, 公众号也发不到). "
 "修法(约15行): 把 get_oa_access_token 改成与小程序一致 —— stable_token 接口 + 存 system_settings 共享缓存(如 wx_oa_access_token), "
 "所有进程共用一把, 互不顶替. 只动这一个函数, 不碰小程序 token 路径. "
 "改动前基线(09-15, 用于改后对比): 小程序订阅消息 成功 1684 条/686 个微信号(模板 押金退还 PtRJgP 1024 条、退款成功 lJpnAU 660 条), "
 "失败 637 条全部 43101; 公众号兜底 成功 0 / 40001 497 / 43101 184. "
 "注意: 改完那 497 次里真正有订阅授权的会发出去, 没授权的仍会 43101(正常现象). "
 "背景: 公众号兜底功能是 09-14 晚上才上线的(09-14 只有 1 条记录), 09-15 是它第一个整天."
)

lines = io.open(T, encoding='utf-8').read().split('\n')
ts = time.strftime('%m-%d %H:%M')
row = "| T-%d | S226 | helpers.py | 待办 | %s | | %s |" % (int(time.time()), ts, DESC)
# 插到"待办"区最后一条 T- 行之后(简单起见: 追加到文件末尾的表格前, 这里直接追加到末尾)
lines.append(row)
io.open(T, 'w', encoding='utf-8').write('\n'.join(lines))
print('已追加待办 T-%d; 备份 %s' % (int(time.time()), bak))

