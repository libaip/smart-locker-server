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

## 铁律
- 不提交 = 没存档；改完验证后立即 commit
- 禁止 git checkout . / reset --hard / 盲 pull / force push
- 回退只用 git revert
- 生产改动前全量备份
- 登记表是防覆盖第一道锁，deploy_check.sh 是最后一道锁，两道都要过
