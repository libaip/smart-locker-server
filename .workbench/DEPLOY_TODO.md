# 待部署清单（106 → 175 生产）

> 更新时间：2026-10-16 01:12（用 md5 精确核对）
> 维护方式：每做完一件事更新；部署后标"已部署"+时间
> ★ 铁律：106 改 → 验证 → deploy_check.sh → 部署175 → 重启 → 验证 → commit → release.sh

---

## 一、★★ 待部署（106 有改动、175 没有 —— 共 6 项）

| # | 文件 | 106 md5 | 175 md5 | 改动量 | 风险 | 说明 |
|---|------|---------|---------|--------|------|------|
| 1 | `app.py` | fc4845642707 | 6fa48ba3839f | +71 -2 | **高** | 埋点接入（要重启） |
| 2 | `helpers.py` | 55735d196748 | 21fc3e685213 | +691 -40 | 中 | S1007-S1011 等 |
| 3 | `wx_config.py` | f886e8daa813 | 149452d0a1a5 | +97 -6 | 中 | 订阅模板字段映射 |
| 4 | `static/wx_accounts_page.js` | 224e8fb420fc | eaba0c79689c | +142 -3 | 低 | 微信账号页 |
| 5 | `risk_profile.py` | 139559105411 | **MISSING** | 新文件 | 低 | 175 没有此文件 |
| 6 | `behavior_log.py` | 8c4aeca0f9df | ce6aa63d7892 | 版本不同 | 中 | 用106版（213行） |

## 二、✅ 已一致（不用部署）

```
wxpay.py / routes/device.py / routes/user.py / routes/admin_v2.py / routes/payment.py
static/admin-v2.html / static/deposit.html / static/profile.html / static/user-h5.html
daily_report.py / behavior_scorer.py / behavior_decision.py / behavior_report.py
backfill_behavior.py
```
★ 注：device.py 的历史分叉已解决（两边一致）

## 三、⏸ 待解冻

| 项 | 状态 | 说明 |
|----|------|------|
| 行为打分 cron（3个任务） | #PAUSED | 灰度模式，解开后每10分钟判定一次 |
| dry_run=0（开启实际退款） | 未开 | 观察1周后再开 |

## 四、★ 部署顺序建议

```
第1步（低风险，不用重启）：
   · risk_profile.py（新文件）
   · static/wx_accounts_page.js（前端）
   · 数据库列已在 175 加好（wx_templates.field_map）✅

第2步（中风险，需 HUP 或重启）：
   · helpers.py + wx_config.py（后端逻辑）
   · behavior_log.py（版本统一，但未接入所以不影响）

第3步（高风险，需重启两个服务）：
   · app.py（埋点接入）
   · 重启 smart-locker + smart-locker-admin
   · 验证：健康检查 + 下单 + 退款 + 后台

第4步：灰度观察 1 周 → dry_run=0
```

## 五、★ 已部署记录（2026-10-16）

| 时间 | 项目 | 说明 |
|------|------|------|
| 01:09 | logrotate rotate 14→90 | sudo 改，logrotate -d 验证通过 |
| 01:09 | wx_templates 加 field_map 列 | DDL，0→1 |
| 01:10 | daily_report.py + crontab(9:05,带flock) | 测试通过（9007条/1060用户） |
| 00:57 | 行为打分模块4文件 + 4张表 | ★ 当时未走工作台流程 |
| 01:02 | 行为打分 cron 3个任务 | ★ 已暂停 #PAUSED |
| 01:10 | 统一 user_behavior_log 表结构 | 以175为准（openid,user_phone,action,path,method,ip,...） |

## 六、★★★ 服务器身份（血泪教训）

```
· 106.55.7.10  = VM-0-13-ubuntu (内网 10.1.0.13) = 【开发/源码机】
· 172.16.0.2   = VM-0-2-ubuntu  (公网 175.178.156.121) = 【生产！】

★★★★ 2026-10-16 教训：误在 172.16.0.2（生产）上直接建文件/表/cron，
   以为那是测试环境。已在 TASKS.md 记录违规。以后动手前先确认身份。

★ 正确流程（SOP）：
   ① 106 看 TASKS.md 确认文件没被"进行中"占用
   ② bash .workbench/claim.sh "说明" "文件"
   ③ 在 106 改代码
   ④ 106 验证（py_compile + 功能测试）
   ⑤ bash .workbench/deploy_check.sh <文件>
   ⑥ 部署 175 + 重启 + 验证
   ⑦ 106 git commit + push
   ⑧ bash .workbench/release.sh <TID>
   ⑨ 更新本清单
```
