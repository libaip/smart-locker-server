# 工作台 - 标准作业流程（SOP）

所有会话对生产服务器（106.55.7.10 /home/ubuntu/smart-locker）的改动，必须按此流程执行。

## 开工
1. 查看 `.workbench/TASKS.md` 任务登记表：确认要改的文件没被别人登记「进行中」
2. 查看 `git status`：确认没有别人未提交的修改
3. 给本会话编号（S1/S2/S3...，按开工顺序）
4. 执行 `bash .workbench/claim.sh "改动说明" "文件1,文件2"` 登记任务

## 只读调研
5. 先只读排查（看代码/日志/数据库），出方案，等用户明确确认（"好/可以/老规矩"）再动手

## 修改与部署（老规矩）
6. 备份：`cp <目标文件> backups/<文件>.bak.<时间戳>`
7. 本地改 → `python3 -m py_compile` 验证通过
8. **部署前必跑**：`bash .workbench/deploy_check.sh <文件1> <文件2> ...`
   - 通过 → 部署
   - 拦截（文件开工后被别人动过）→ 停下来，和对应会话/用户确认，确认后加 `--force` 放行
9. 部署 → 验证（health/日志/数据）→ `git add + git commit`（message 带会话编号）

## 收工
10. 执行 `bash .workbench/release.sh` 标记任务完成

## APK 设备端代码（2026-08-28 确认）
- APK 源码唯一源头 = **旧机 106.55.7.10 `/home/ubuntu/smart-locker-apk`**（git 仓库，版本 1.4.12 / versionCode 269）
- 新机 175.178.156.121 上的 APK 相关（`/home/ubuntu/smart-locker-apk/` 空目录 + `smart-locker-apk.tar.gz` 旧快照）**不是源码源头，不用维护**
- 改 APK 只允许在旧机 smart-locker-apk 仓库内改 + git commit（会话编号沿用 S 编号体系，如 by S28）
- 编译成品 + 推设备：`/home/ubuntu/push_apk.sh <设备号>`（往 pending_lock_cmds 写 force_update，设备自动下载）
- 设备下载 APK 走 `https://locker.cqdyxl.com/static/locker.apk`（注意核对 static 目录里是否有该文件）
- APK 不参与新老服务器代码同步（方案C），旧机是唯一源头

## 代码修复 vs 数据修复（2026-08-31 确认）
- **代码修复**：改旧机 106.55.7.10 文件 → deploy_check → rsync 同步到新机 175.178.156.121 → 重启新机服务
- **数据修复（SQL/删记录/改数据）**：**必须在生产库执行 = 新机 175.178.156.121 的 PostgreSQL**（服务跑在新机，连的是新机库）
- 旧机 106.55.7.10 的数据库**不是生产库**，数据修复在旧机库改了无效（白改），别在旧机库做数据操作
- 数据修复前：先备份相关表（CSV 导出）→ dry-run 预演 → 确认 → 执行 → 验证
- 两台服务器的数据库是**独立 PG 实例**，数据不会自动同步，改哪台就是哪台

## 更换小程序 appid 迁移（2026-09-10 确认）
- 完整 SOP 见 `.workbench/MP_MIGRATION_SOP.md`（10 节 checklist：配置 / openid前缀 / 订阅模板 / 前端小程序 / H5 / unionid身份 / 双节点部署 / 验证 / 坑位速查 / 禁止事项）
- 核心三件事：① openid 前缀迁移（旧 `oWrA8` → 新 `ooTcRx`；公众号 `oLhbm2` 必须排除）② 3 个订阅模板 id 全部换新（寄存成功 Q3Fts5 / 押金退还 PtRJgP / 退款成功 lJpnAU）③ 用户侧查询一律按 unionid
- 头号坑：openid 前缀硬编码散落在 helpers.py / routes/payment.py / routes/admin_v2.py / routes/user.py，漏改会报 40003 invalid openid
- 次号坑：前端存包订阅授权必须带"寄存成功"模板（deposit.js 的 requestSubscribe 曾漏 deposit_notify），否则用户收不到寄存成功通知
- 三号坑：提现规则页面（withdraw/mine/wallet）文案必须一致且齐全（额度/次数/时间/到账+客服电话），否则微信审核被拒

## 铁律
- 不提交 = 没存档；改完验证后立即 commit
- 禁止 git checkout . / reset --hard / 盲 pull / force push
- 回退只用 git revert
- 生产改动前全量备份
- 登记表是防覆盖第一道锁，deploy_check.sh 是最后一道锁，两道都要过
