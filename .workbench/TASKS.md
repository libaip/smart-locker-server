# ============================================
# 工作台 - 任务登记表（所有会话必须遵守）
# 铁律：改任何文件前，先看本表；目标文件被登记为「进行中」则不许碰！
# 铁律2:给老板(用户)汇报/陈述一律用大白话(人话),说清改了什么、为什么改、影响什么、要老板定什么,禁止术语堆砌。
# 铁律3:后台管理走独立服务smart-locker-admin(5002),改完admin_v2.py等后端代码必须同时重启smart-locker和smart-locker-admin两个服务,否则后台页面不生效!
# 用法：
#   bash .workbench/claim.sh "说明" "文件1,文件2"   # 开工登记
#   改完部署提交后：bash .workbench/release.sh      # 标记完成
# ============================================

# 当前任务

| 任务ID | 会话 | 目标文件 | 状态 | 开工时间 | 完成时间 | 备注 |
|--------|------|---------|------|---------|---------|------|
| T-000 | 示例 | routes/user.py | 已完成 | 08-20 09:00 | 08-20 09:30 | 示例行，可删 |

# 历史记录（追加在下面，不删除）
| T-1787233992 | S2 | routes/user.py | 已完成 | 08-20 21:53 | 08-20 21:53 | 工作台自测 |
test
test ok
test ok
test
| T-1787234566 | S3 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-20 22:02 | 08-20 22:12 | 提现列表显示退款单号(refund_id): /admin/withdrawals接口带出o.refund_id + 前端两处提现表格加列 |
| T-1787235233 | S4 | audit_reconcile.sh | 已完成 | 08-20 22:13 | 08-20 22:14 | 新增每日对账巡检脚本(提现记录/订单退款/余额一致性) |
| T-1787235322 | S5 | .workbench/deploy_check.sh | 进行中 | 08-20 22:15 | | 存量refund_id回填脚本(orders从payments补单号)+修复deploy_check.sh多文件md5比对bug |
| T-1787236458 | S6 | routes/admin_v2.py | 已完成 | 08-20 22:34 | 08-20 22:35 | 支付渠道统计: 对账单无交易数据时回退订单表口径(修复8-16改对账单统计后数字变0) |
| T-1787236891 | S7 | routes/admin_v2.py | 已完成 | 08-20 22:41 | 08-20 22:41 | 支付渠道统计回退条件补强: 对账单金额<订单金额90%也回退订单口径(修复1749620516等对账单仅2笔仍用对账单) |
| T-1787237745 | S8 | /etc/nginx/sites-enabled/locker-cqdyxl,/etc/systemd/system/smart-locker-admin.service | 已完成 | 08-20 22:55 | 08-20 22:59 | 后台管理独立通道: 新增5002 gunicorn(2worker)+nginx分流/api/admin*到5002 |
| T-1787238302 | S9 | helpers.py | 已完成 | 08-20 23:05 | 08-20 23:05 | 修复_date类型序列化成HTTP日期(Wed,19 Aug..GMT): _format_datetimes支持date输出YYYY-MM-DD |
| T-1787299263 | S10 | .workbench/TASKS.md | 已完成 | 08-21 16:01 | 08-21 16:02 | 白名单来源审计+三改方案记录,大白话存档 |
n
# 大白话记录（S10 追加于 08-21）

## 一、白名单是啥、哪三个地方能进去

白名单 = 系统里的一批"免审名单"。进了名单的人,提现不用等审批、不用摇号,系统直接微信原路退钱。

现在加人只有 3 个口子:
1. **投诉就进**(source=complaint):用户在 H5/小程序点投诉、或在微信支付账单里投诉,系统当场把这人拉进白名单——不管钱退没退成。库里 1921 条。
2. **提现被拒过再提就进**(source=reject_retry):提现被系统自动拒过 1 次,下次再提现,系统直接拉白并当场放款。库里 5541 条,最多。
3. **后台手动退款附带**(source=manual_help):管理员在后台对某单点"手动退款",成功后顺手给这用户加 1 次白名单,用完即消。库里 0 条。

注意:网点后台填的"白名单手机号"(whitelist_phones)是另一套东西(订单隐藏用),跟提现审批白名单无关,别混。

## 二、这两天查出来的事(大白话)

1. "人工审批"网点(偃师/林州/亿航等 19 个)其实没人人工批。系统实际干的是:
   - 白名单的人:直接退(必退,不看比例);
   - 没白名单的:按网点设置的"通过率"摇号,摇中就退、摇不中就拒;
   - 通过率 0% 的网点(偃师等):非白名单用户提现要干等 3 天,3 天后系统"自动拒绝",钱退回余额——用户不知道,以为钱丢了,就去投诉。积压从 8-18 开始,现在 2155 笔、6.9 万块。
2. 昨天(8-20)"通过率 0% 的网点"被放行了 224 单,逐单核对:**没有一单是"非白名单摇号放行"的**——全部是白名单豁免(设计如此);只有 1 单来路存疑(誉荣 wd 50217),疑似其他会话跑脚本放的。
3. 今天 76 笔微信投诉里,21 笔(28%)是"先提现、等不到钱、再投诉"的用户。
4. 用户问的"到时间按比例自动批"确实是设计:队列/人工审批网点都有"到点按比例通过"的逻辑,之前说的"绕过人工审批"说法不准确,已更正。

## 三、准备改的三件事(方案已出,等老板拍板默认值)

1. **白名单加有效期**:比如投诉类 90 天、被拒重提类 30 天,过期自动失效,不再终身有效。
2. **投诉类白名单限次数**:比如最多免审 3 次,用完作废(配合现有的每日 3 次上限)。
3. **投诉不马上拉白**:改成"投诉处理完、钱真的退了"才拉白,退不成的白名单不加。

要老板定的 5 个默认值:
- 投诉类白名单有效期?推荐 90 天
- 被拒重提类有效期?推荐 30 天
- 投诉类免审次数?推荐 3 次
- 库里现有 7464 条旧白名单:A 照旧不过期 / B 统一补有效期?推荐 A(不误伤)
- 微信客服自动退款成功要不要也拉白?推荐:要,和投诉一致

## 四、其他存档

- 分析脚本都在本地 D:\.codex\workspace\2026-08-19\complaint_realtime\ 下(report_auto_vs_stuck.sql、report_329_breakdown.sql、check_rate0_released.sql、whitelist_fix_plan.md 等)
- 多会话在生产活动(今天 git 有 S6/S7/S9 提交),改白名单代码前先 claim,别互相踩
| T-1787299420 | S11 | .workbench/TASKS.md | 已完成 | 08-21 16:03 | 08-21 16:03 | 工作台追加规则:向老板汇报一律大白话 |
| T-1787300244 | S12 | helpers.py,routes/user.py,routes/admin_v2.py,routes/admin.py,.workbench/TASKS.md | 已完成 | 08-21 16:17 | 08-21 16:30 | 白名单三改:有效期+网点次数(默认3)+退款成功才拉白,含存量补期补次 |
| T-1787300657 | S13 | routes/admin_v2.py,routes/device.py | 已完成 | 08-21 16:24 | 08-21 16:28 | 修复一键开门(批量): batch_open补device_id/board_no/lock_no+统一在线判断; pending_update放行open_lock指令 |
| T-1787302997 | S14 | routes/merchant.py,routes/admin.py,helpers.py | 已完成 | 08-21 17:03 | 08-21 17:07 | 一键开门改方案B: merchant/admin open_all改逐门send_open_lock(带board/lock); send_open_all去重复推送 |
| T-1787303385 | S15 | ws_proxy.py | 进行中 | 08-21 17:09 | | 一键开门乱序修复: ws_proxy /send 改同步发送(去gevent.spawn), 指令按序到达设备 |
| T-1787317199 | S16 | static/admin-v2.html | 已完成 | 08-21 20:59 | 08-21 20:59 | 后台转圈修复:所有fetch加30秒超时(AbortController),超时自动关loading并提示 |
| T-1787317341 | S17 | static/admin-v2.html | 已完成 | 08-21 21:02 | 08-21 21:04 | 后台网点编辑加余额隐藏配置(启用+天数), 默认15天 |
| T-1787317376 | S18 | static/admin-v2.html | 已完成 | 08-21 21:02 | 08-21 21:02 | 后台转圈根治:轮询静默不参与转圈+转圈8秒硬上限 |
| T-1787317845 | S19 | static/admin-v2.html | 已完成 | 08-21 21:10 | 08-21 21:13 | 网点管理加代理商筛选 |
| T-1787318234 | S20 | routes/admin_v2.py | 已完成 | 08-21 21:17 | 08-21 21:17 | 自有投诉卡单修复:调度器non-wechat段纳入status=1半截单+claim兼容 |
| T-1787318601 | S21 | static/admin-v2.html | 已完成 | 08-21 21:23 | 08-21 21:23 | 在线订单: 选网点后设备下拉联动加载该网点设备 |
| T-1787322116 | S22 | static/admin-v2.html | 已完成 | 08-21 22:21 | 08-21 22:21 | 微信投诉页状态显示补全(退款失败标红)+加提现手动退款按钮 |
| T-1787323006 | S23 | .workbench/TASKS.md | 已完成 | 08-21 22:36 | 08-21 22:36 | 铁律3:改动后端admin_v2.py等需同时重启smart-locker与smart-locker-admin(5002后台通道) |
| T-1787323507 | S24 | routes/admin_v2.py | 已完成 | 08-21 22:45 | 08-21 22:51 | 柜门查询修复: door_status_queries表解决8worker跨进程丢结果 |
| T-1787326350 | S25 | routes/admin_v2.py | 已完成 | 08-21 23:32 | 08-21 23:54 | 柜门查询去双通道: WS推送成功则不插poll命令, 避免设备重复执行/上报混乱 |
| T-1787328454 | S26 | helpers.py,routes/admin_v2.py,routes/admin.py,routes/merchant.py | 已完成 | 08-22 00:07 | 08-22 07:28 | 一键开门列表方案C: send_open_lock_list(单命令带门列表) + 一键开门改用它 |
| T-1787355313 | S27 | routes/admin_v2.py,routes/merchant.py | 已完成 | 08-22 07:35 | 08-22 07:35 | 小程序查询/一键开门修复: admin_v2一键开门改方案C, merchant两个查询接口改DB共享表方案返回真实状态 |
| T-1787357943 | S28 | routes/admin.py,routes/merchant.py,routes/admin_v2.py | 已完成 | 08-22 08:19 | 08-22 08:19 | 一键开门乱序根因修复: open_all三个入口SQL加ORDER BY slot_number(PostgreSQL无ORDER BY返回物理顺序导致乱) |
| T-1787359945 | S29 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-22 08:52 | 08-22 09:23 | 后台远程重启设备: admin_v2加restart接口+设备端处理reboot命令+机器列表详情按钮改重启 |
| T-1787361793 | S30 | routes/admin_v2.py,smart-locker-apk/app/src/main/java/com/smartlocker/screen/MainActivity.java,smart-locker-apk/app/src/main/java/com/smartlocker/screen/service/LockerService.java,smart-locker-apk/app/src/main/java/com/smartlocker/screen/utils/PreferencesHelper.java,smart-locker-apk/app/src/main/res/layout/activity_main.xml,smart-locker-apk/app/src/main/res/layout/guize5.xml | 进行中 | 08-22 09:23 | | 柜门数量开关打通+UI优化: admin_v2推送show_slot_count+设备端控制可用柜门显示/隐藏+密码取包按钮下移/规则区调整, 重新编译1.4.12 |
| T-1787366348 | S31 | app.py | 已完成 | 08-22 10:39 | 08-22 10:39 | 超时清理竞态修复:跨进程文件锁+抢占式取消,防支付中订单被误取消自动退款 |
| T-1787378990 | S32 | helpers.py | 已完成 | 08-22 14:09 | 08-22 14:09 | 修复add_whitelist过期时间参数化bug(拉白全失败),被拒拉白失效 |
| T-1787382356 | S33 | routes/merchant.py | 已完成 | 08-22 15:05 | 08-22 15:05 | 商户端提现金额统计改按订单使用日归集(B方案) |
