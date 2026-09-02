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
| T-1788357059 | 待办 | 小程序通用版(uni-app) | 待办 | 09-02 21:50 | | 微信+支付宝通用版工程已建(D:\工具配置迁移包_20260811\小程序_通用版, uni-app Vue3): 工程骨架+platform适配层+subscribe订阅授权页完成, 微信/支付宝两端均编译通过; 微信原生版不动继续跑; 待办: ①企业认证过->开放平台创建小程序应用->appid填manifest.json的mp-alipay.appid ②领支付宝订阅模板(长期优先:取件/退款提醒)->填utils/api.js的TEMPLATES.alipay ③后端加支付宝接口(link-alipay绑定/订阅消息发送templatemessage.send/alipay.py手机网站支付下单退款回调/payment_channels支持channel_type=alipay) ④H5按UA分流跳alipays scheme ⑤功能页逐步迁移(17页), 迁移完微信切换; 注意: 支付宝无exitMiniProgram等价物, 返回H5链路需真实环境验证 |
| T-1788329411 | 待办 | 支付宝接入(整体) | 待办 | 09-02 14:10 | | 支付宝双通道接入(分摊微信封号风险+微信不可用兜底): ①企业支付宝注册(用户办理, 用营业执照, 流程见.workbench/ALIPAY_REGISTER.md) ②后端alipay.py(手机网站支付下单/退款/回调验签, 照抄wxpay.py结构) ③payment_channels支持channel_type=alipay+新回调路由/api/pay/alipay/notify ④H5(deposit.html+store.html)按UA分流: 微信走原流程, 支付宝扫同一设备二维码(https://locker.cqdyxl.com/store?cabinet_id=x)进支付宝通道 ⑤支付宝小程序极简版(subscribe授权订阅消息1页, 逻辑翻版微信subscribe.js, my.requestSubscribeMessage传2模板: 取件提醒+退款提醒, 每单授权每单推, 拒绝授权不卡支付) ⑥推送优先订阅消息(alipay.open.app.mini.templatemessage.send), 长期模板优先一次性兜底, 推不了先记日志(短信宝接好后再补短信) ⑦退款走支付宝原路(alipay.trade.refund) ⑧投诉/对账链路接alipay通道 |

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
| T-1787383003 | S34 | routes/merchant.py | 已完成 | 08-22 15:16 | 08-22 15:16 | 商户端提现统计改订单维度(与后台一致,含余额退款) |
| T-1787398174 | S35 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-22 19:29 | 08-22 20:04 | 后台多问题修复: 在线订单设备下拉按网点过滤/机器列表网点代理商筛选生效/支付渠道加商户号筛选+昨天账单金额显示/刷新转圈/告警管理设备掉线上线记录/会员充值记录加时间商户单号手机号+默认当天 |
| T-1787405255 | S36 | routes/merchant.py | 已完成 | 08-22 21:27 | 08-22 22:30 | 商户订单详情开门记录修复: door_records双查(数字id+订单号)+类型语义化(存包/中途/结束开门)+合并远程开门日志显示操作人(商家/代理商/平台) |
| T-1787412569 | S37 | routes/merchant.py | 已完成 | 08-22 23:29 | 08-22 23:35 | 告警记录按最新离线时间倒序: device-status接口ORDER BY last_heartbeat DESC, 最新离线的排最上面 |
| T-1787413039 | S38 | routes/admin_v2.py | 已完成 | 08-22 23:37 | 08-22 23:40 | 投诉验签404修复: 平台证书404(RESOURCE_NOT_EXISTS)时改用微信支付公钥(/v3/pay/public-key)验签 |
| T-1787449752 | S39 | routes/admin.py | 已完成 | 08-23 09:49 | 08-23 09:54 | per_use按次收费模式补默认寄存规则+display_text乱码修复(需同步到新机172.16.0.2并重启) |
| T-1787450119 | S40 | AGENTS.md | 已完成 | 08-23 09:55 | 08-23 09:56 | 新增第14节: 主备互换后的代码修改SOP(方案A: 旧机改代码+git, 新机部署运行) |
| T-1787453512 | S41 | wecom-kf/app.py | 已完成 | 08-23 10:51 | 08-23 10:56 | 客服自动回复菜单6: 开启小程序卡片推送(MP_CARD_ENABLED=True)+新缩略图media_id+兼容'6 退预付款'文本触发 |
| T-1787454321 | S42 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-23 11:05 | 08-23 11:11 | 提现管理加网点/代理商筛选: 后端withdrawals接口支持location_id/agent_id过滤+前端加下拉框 |
| T-1787455447 | S43 | helpers.py | 已完成 | 08-23 11:24 | 08-23 11:25 | 白名单重复拉白改方案2: add_whitelist次数取较小值(min)不重置回满, 有效期不刷新 |
| T-1787456097 | S44 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-23 11:34 | 08-23 11:53 | 统计分析加退款实时金额+使用人数(顶部+网点明细); 会员提现加审批人筛选 |
| T-1787457690 | S45 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-23 12:01 | 08-23 12:01 | 修复统计分析筛选失效+退款实时金额口径(按refund_time独立查询,明细按退款日列表) |
| T-1787471917 | S46 | app.py | 已完成 | 08-23 15:58 | 08-23 16:01 | 余额隐藏定时任务SQL参数化(INTERVAL %s改参数绑定,防注入/防拼接问题) |
| T-1787473035 | S47 | routes/admin_v2.py | 已完成 | 08-23 16:17 | 08-23 16:19 | 修复统计分析网点筛选时退款实时金额显示0(GROUP BY补退款日期) |
| T-1787476978 | S48 | routes/admin_v2.py,app.py | 已完成 | 08-23 17:22 | 08-23 17:27 | 余额不足隐藏+同步隐藏订单: 失败单保持pending不退余额+订单logic_mark=Y; 超期隐藏定时任务同步隐藏订单 |
| T-1787490691 | S49 | app.py | 已完成 | 08-23 21:11 | 08-23 21:12 | 修复超期隐藏未同步隐藏历史订单(订单隐藏逻辑从if hidden>0移出独立执行) |
| T-1787491824 | S50 | app.py | 已完成 | 08-23 21:30 | 08-23 21:33 | 修复订单隐藏误伤: 只隐藏超期隐藏余额的订单, 恢复已提现/提现冻结/已取消订单 |
| T-1787492172 | S51 | routes/user.py | 已完成 | 08-23 21:36 | 08-23 21:38 | 隐藏改用户端局部: 回滚logic_mark恢复商户订单, 用户端订单列表按余额隐藏+无提现记录过滤 |
| T-1787493190 | S52 | wecom-kf/app.py | 已完成 | 08-23 21:53 | 08-23 21:54 | 客服卡片小程序路径改首页: MP_WALLET_PATH=pages/index/index |
| T-1787494386 | S53 | wecom-kf/app.py | 已完成 | 08-23 22:13 | 08-23 22:14 | 客服卡片小程序路径加.html后缀: MP_WALLET_PATH=pages/mine/mine.html |
| T-1787495358 | S54 | app.py,routes/admin_v2.py | 已完成 | 08-23 22:29 | 08-23 22:33 | 修复商户订单比例隐藏被破坏: 移除S48两处打logic_mark=Y(定时任务+失败单), 恢复auto_hidden比例隐藏 |
| T-1787532215 | S55 | wecom-kf/app.py | 已完成 | 08-24 08:43 | 08-24 08:44 | 客服菜单隐藏第7条(功能保留), 第6条回复加未到账提示语 |
| T-1787532473 | S56 | wecom-kf/app.py | 已完成 | 08-24 08:47 | 08-24 08:48 | 客服第6条文案改'下方微信小程序', 用户发11位手机号自动走提现未到账流程 |
| T-1787571703 | S57 | routes/admin_v2.py | 已完成 | 08-24 19:41 | 08-24 19:44 | 修复微信投诉网点筛选: admin_complaints接口支持location_id参数(COUNT+列表查询加柜机关联) |
| T-1787583987 | 待办 | - | 待办 | 08-24 23:06 | | 备用公众号方案(第二层): 找回开放平台登录邮箱/管理员微信 -> 开放平台绑定备用公众号wx4f65dc701e9111fa(验证unionid与主公众号一致) -> 若邮箱找不回则做手机号匹配切换方案; JS接口安全域名/业务域名/消息服务器配置等真用到时再配 |
| 待办 | 待办 | - | 待办 | 08-24 23:06 | | 备用域名(第一层): 用公司主体购买新域名+ICP备案(公司备案,别用个人), 备案通过后配nginx+支付回调备用配置 |
| 待办 | 待办 | - | 待办 | 08-24 23:06 | | 商户小程序上线: 程序_商家_v2(订单详情状态只显进行中/已结束+开门记录类型文字) 微信开发者工具清缓存编译预览后上传发布 |
| 待办 | 待办 | - | 待办 | 08-24 23:06 | | 新服务器175.178.156.121与旧服务器106.55.7.10代码同步机制: 目前新服务器非git仓库, 需建立同步流程避免再次出现改错服务器 |
| T-1787620706 | S59 | wecom-kf/app.py | 已完成 | 08-25 09:18 | 08-25 09:19 | 客服菜单恢复显示第7条(预付款未到账) |
| T-1787621385 | S60 | app.py | 进行中 | 08-25 09:29 | | 幽灵柜门巡逻误杀待支付柜门修复: 释放条件加status IN(1,2), 防止柜门被二次分配导致支付回调唯一约束冲突(82434事故) |
| T-1787637572 | S61 | routes/user.py | 已完成 | 08-25 13:59 | 08-25 14:01 | 修复扫码H5下单误报设备离线: h5_store在线判断从只看心跳改为is_device_online(心跳+WS连接+5004在线) |
| T-1787638967 | S62 | routes/admin_v2.py | 已完成 | 08-25 14:22 | 08-25 14:25 | 微信投诉回复改只发一条: 删除受理通知WECHAT_FIRST_REPLY发送, 退款成功发统一话术WECHAT_FINAL_REPLY(预付款已全额退款), 失败/无需退款保留原话术 |
| T-1787646944 | S63 | routes/admin_v2.py,helpers.py,routes/user.py | 已完成 | 08-25 16:35 | 08-25 16:39 | 拉黑未结束订单投诉用户: blacklist加unionid字段, 自动拉黑(手机号+unionid), check_use_limits拦截, 批量拉黑历史188人 |
| T-1787647961 | S64 | routes/user.py | 进行中 | 08-25 16:52 | | 结束订单设备离线不再拒绝: 离线时跳过远程开门直接结束退押金, 防止设备故障/断电导致订单卡死(18888889999订单68857) |
| T-1787665260 | S65 | routes/admin_v2.py | 已完成 | 08-25 21:41 | 08-25 21:43 | 拉黑累计投诉>=2次用户: 调度器自动拉黑(手机号+unionid), 历史批量拉黑 |
| T-1787666626 | S66 | static/deposit.html,routes/user.py | 进行中 | 08-25 22:03 | | H5取包只允许中途取物: 隐藏取包结束按钮+retrieve/confirm的end分支拒绝, 结束订单统一走end-storage |
| T-1787667239 | S67 | helpers.py | 已完成 | 08-25 22:13 | 08-25 22:21 | 黑名单拦截提示语改为: 操作异常，请联系客服4006981080 |
| T-1787705366 | S68 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-26 08:49 | 08-26 08:51 | 黑名单加筛选(手机号/原因/状态)+解除改为临时解除(status=0保留记录)+重新封禁 |
| T-1787705769 | S69 | routes/admin_v2.py | 已完成 | 08-26 08:56 | 08-26 08:57 | 修复累计投诉>=2次自动拉黑被嵌套在if内不执行: 移出if每次投诉都执行+手机号兜底 |
| T-1787705938 | S70 | helpers.py,routes/admin_v2.py | 已完成 | 08-26 08:58 | 08-26 09:01 | 临时解除改一次性: blacklist加unban_use_once, 解除后用户可用1次自动重新拉黑 |
| T-1787709914 | S71 | routes/admin_v2.py | 已完成 | 08-26 10:05 | 08-26 10:12 | S71: 投诉退款调度器修复: 本地无订单时查微信API确认REFUND则结案, 调度器只处理status 0/1防无限重试 |
| T-1787710553 | S72 | routes/admin_v2.py | 已完成 | 08-26 10:15 | 08-26 10:16 | 修复投诉退款失败无限重试空转: 调度器查询排除refund_retry>=3的投诉(转人工后不再自动重试) |
| T-1787714676 | S73 | routes/admin_v2.py,static/admin-v2.html | 进行中 | 08-26 11:24 | | 微信投诉表格微信交易号列改为商家: 后端返回商户名称(pc.name按订单渠道+c.mch_id双匹配), 前端表头改商家 |
| T-1787715795 | S74 | routes/merchant.py | 进行中 | 08-26 11:43 | | 商家订单管理显示状态修正: 订单列表从status(2,4)改为(2,3), 显示进行中+已结束, 统计接口口径不动 |
| T-1787718480 | S75 | wecom-kf/app.py,wecom-kf/renew_thumb.py | 已完成 | 08-26 12:28 | 08-26 12:28 | 企微客服小程序卡片缩略图media_id失效修复+自动续期: 重新上传thumb.png, renew_thumb.py定时每2天更新media_id并重启+同步旧机 |
| T-1787720353 | S76 | helpers.py | 已完成 | 08-26 12:59 | 08-26 13:00 | 修复扫码误报设备离线: is_device_online心跳过期时也查WS连接/5004在线列表,避免心跳延迟误判 |
| T-1787751058 | S77 | complaint_auto.py | 已完成 | 08-26 21:30 | 08-26 21:34 | 修复微信投诉拉取: complaint_auto分页拉取(数据量大400), 同时登记PENDING+PROCESSING状态投诉 |
| T-1787751842 | S78 | routes/admin_v2.py | 已完成 | 08-26 21:44 | 08-26 21:53 | 修复订单已退款投诉本地完结但微信侧仍PENDING: 定期同步逻辑先调微信complete接口再更新本地 |
| T-1787753008 | S79 | routes/user.py,routes/admin_v2.py,routes/merchant.py,static/admin-v2.html,static/deposit.html | 进行中 | 08-26 22:03 | | 免押模式: 后台开关(商户号全封停时用户免押使用), 订单标记free_use不计入商家/后台业绩统计, H5+小程序自动跳过支付直接开门 |
| T-1787756073 | S80 | routes/admin_v2.py | 进行中 | 08-26 22:54 | | 代理商统计排除免押单: agent/stats订单数与金额加free_use=0条件(商户端已排除) |
| T-1787757123 | S81 | routes/admin_v2.py,routes/user.py,static/admin-v2.html | 进行中 | 08-26 23:12 | | 免押开关改存PG: 开关接口与store_init判断统一读PG(system_settings), 修复SQLite WAL/连接缓存导致开关切换页面后状态丢失; 前端补Vue data字段+传参格式修复 |
| T-1787793358 | S82 | routes/admin_v2.py | 已完成 | 08-27 09:15 | 08-27 09:16 | 关闭余额不足自动拒绝的订阅通知(不再给用户发'提现被拒绝-余额不足'消息) |
| T-1787839547 | S83 | complaint_notify_check.py | 进行中 | 08-27 22:05 | | 投诉通知URL自动检查修复脚本: 每30分钟遍历启用商户号核对投诉通知回调URL, 未配/配错自动修复 |
| T-1787842342 | S84 | routes/admin_v2.py | 进行中 | 08-27 22:52 | | 商户号保存后自动配置投诉通知URL: admin_channel_save保存成功即触发complaint_notify_check, 新增/编辑商户号即配好通知(保留30分钟轮询兜底) |
| T-1787885376 | S85 | routes/admin_v2.py | 已完成 | 08-28 10:49 | 08-28 10:56 | 修复手动退款后投诉卡死: 调度器订单已退款分支先回复再complete, 修auto_complete失败误判OK |
| T-1787891602 | S86 | routes/user.py | 已完成 | 08-28 12:33 | 08-28 12:34 | 修复扫码进H5页面误报设备离线: store/init, retrieve, create-order 3处is_heartbeat_online改is_device_online(心跳过期查WS) |
| T-1787891983 | S87 | helpers.py | 已完成 | 08-28 12:39 | 08-28 12:40 | 彻底修复设备离线误判: is_device_online先查WS在线列表(实时最准)再回退心跳, 5004查询失败容错不误判 |
| T-1787892713 | S88 | routes/user.py | 已完成 | 08-28 12:51 | 08-28 12:53 | 修复小程序中途开门提示系统维护中: deposit_mid_retrieve注释_is_miniprogram_request拦截(接口有归属/次数/网点校验安全) |
| T-1787919390 | S89 | routes/user.py | 已完成 | 08-28 20:16 | 08-28 20:18 | 用户端订单列表去掉auto_hidden过滤(商户端比例隐藏误伤用户端, 22,736笔被误藏恢复显示) |
| T-1787919521 | S90 | routes/user.py | 已完成 | 08-28 20:18 | 08-28 20:20 | 小程序交易明细隐藏超时订单: get_user_transactions排除订单余额pending且无available/withdrawn/提现的明细 |
| T-1787919632 | S91 | routes/admin_v2.py | 已完成 | 08-28 20:20 | 08-28 20:22 | 证书序列号兜底: 投诉回复/完结时若cert_serial_no为空自动从证书文件补提并更新数据库 |
| T-1787919800 | S92 | 无代码改动(数据修复) | 已完成 | 08-28 20:23 | 08-28 20:24 | 批量补处理11条微信侧PENDING但本地已完结的投诉(先回复再complete) |
| T-1787922760 | S93 | routes/admin_v2.py | 已完成 | 08-28 21:12 | 08-28 21:22 | 投诉自动退款成功时补建提现记录: _auto_refund_complaint_order退款成功后同步插入withdrawal_records(status=2, approver=投诉自动退款), 修复余额明细withdrawn但提现记录空白 |
| T-1787927217 | S94 | wecom-kf/app.py | 已完成 | 08-28 22:23 | 08-28 22:24 | 客服自动退款成功时补建提现记录(与S93同口径), 修复企微客服退款漏记账 |
| T-1787927970 | 待办 | complaint_auto.py,static/admin-v2.html.fixed | 待办 | 08-28 22:30 | | 补提交S77漏掉的complaint_auto.py分页修复(改完未commit,工作区躺着) + 删除admin-v2.html.fixed历史遗留文件, 一次性commit+push |
| T-1787961644 | 待办 | 新老服务器同步机制 | 待办 | 08-28 22:40 | | 方案C: 旧机改代码commit后自动部署到新机(rsync+重启), 需配旧机->新机SSH免密(sudo白名单限重启命令), sync_new.sh脚本+git hook, 白天同步不自动重启/晚上统一重启, wecom-kf留手动同步命令 |
| T-1788012422 | S97 | routes/admin_v2.py | 已完成 | 08-29 22:07 | 08-29 22:25 | 问题1修复: 会员管理手动退款与用户提现撞车导致余额负数-①member_refund退款前检查待审核wr(有则拦截) ②admin_withdrawal_approve审核时订单已refunded则不再重复退款并把用户提现扣的余额补回 |
| T-1788048307 | S98 | routes/admin_v2.py | 已完成 | 08-30 08:05 | 08-30 08:12 | 问题3修复: 提现退款失败一次即隐藏不再重试-自动提现rcnt>=3分支改为bd保持pending隐藏+wr置3拒绝, 用户端不提示后台手动处理, 不动logic_mark不影响商户订单数 |
| T-1788058436 | 待办 | 短信宝短信接入 | 待办 | 08-30 10:40 | | 短信宝配置: 注册(smsbao.com)->问客服通知模板变量{1}与小程序名固定文本能否过审->能行则充值+申请签名(科莱智)+变量模板->凭据(API用户名/API Key/模板ID)交给我接send_sms.py+退款场景补发短信 |
| T-1788059393 | S100 | routes/admin_v2.py | 已完成 | 08-30 11:09 | 08-30 11:15 | 问题3补充修复: run_withdrawal_batch_auto里4处退款失败仍恢复余额(白名单/队列审批/人工审批/后台审批部分失败)改为保持pending隐藏+wr置3, 存量73笔改pending; 不影响通过率未达标拒绝分支 |
| T-1788068805 | S101 | routes/admin_v2.py | 已完成 | 08-30 13:46 | 08-30 14:04 | 商户异常隐藏记录收尾: ①8条wr=1卡住置为拒绝(status=3)+bd隐藏 ②订单已refunded但wr挂失败的置为完成(status=2) ③新增后台已线下转账处理入口(wr置2+bd withdrawn+订单refunded+备注) |
| T-1788100721 | S102 | routes/admin_v2.py | 已完成 | 08-30 22:38 | 08-30 23:15 | 订单退款与提现申请撞车修复: admin_order_refund联动待审核wr时, 订单从wr移除后补生成一条订单退款提现记录(金额=押金,approver=管理员-订单退款), 不再把wr金额扣成0; 存量539条amount=0记录修复 |
| T-1788103123 | S103 | routes/admin_v2.py,static/admin-v2.html | 已完成 | 08-30 23:18 | 08-30 23:23 | 在线订单列表支付商户列显示渠道名称: admin_orders返回pc.name as pay_mch_name, 前端显示pay_mch_name||pay_mch_id兜底 |
| T-1788172641 | S104 | routes/user.py,routes/admin_v2.py | 已完成 | 08-31 18:37 | 08-31 18:48 | 三修复: A.user_withdraw金额<=0不生成0元wr B.get_user_orders余额隐藏订单也隐藏(wr=3被拒也藏) C.清理历史遗留wr status=0且bd=available(拦截提现) |
| T-1788176438 | S105 | 生产库数据修复 | 已完成 | 08-31 19:30 | 08-31 19:35 | C类负数用户797个修正: balance=仅available bd金额, total_deposited=真实押金和, total_withdrawn=已退款和; 负数用户1476->679, 负数总额34481->16085元 |
| T-1788184335 | S106 | routes/user.py,routes/admin_v2.py,static/admin-v2.html,config.py,helpers.py | 已完成 | 08-31 21:52 | 08-31 22:10 | 短信宝短信接入: 结束订单退押金时发短信(场景A), 网点级开关控制发不发(sms_enabled字段+前端配置), send_sms.py调短信宝API, 模板{fee}{amount}{appName} |
| T-1788188835 | S106补充 | 生产库+前端 | 已完成 | 08-31 22:50 | | 网点短信发送默认关闭: 生产库42网点sms_enabled置0+DB默认0+前端默认false; 短信宝API待客服确认(返回0但后台无记录/签名套智行, 疑VIP通道需人工报备开通) |
| T-1788245480 | 待办 | 短信宝 | 待办 | 09-01 00:10 | | 短信宝等工信部正式审批(签名+模板已预审通过): 审批通过后改send_smsbao用新模板(无引流词版: 您的寄存押金{1}元已退款,请注意查收)+模板匹配发送, 测试稳定后按网点打开sms_enabled |
