# smart-locker 协作规则（所有 Codex 对话必须遵守）

这个项目可能有多个对话同时修改代码，必须防止互相覆盖。以下规则所有对话一律遵守：

## 1. 改代码前
先执行 `git status`，确认有没有别人未提交的修改。
如果有别人未提交的修改，先停下来沟通，不要直接覆盖。

## 2. 改完必须立即提交存档
每次修改完成并验证通过后，**必须立即提交**：
```
git add <改过的文件>
git commit -m "说明这次改了什么"
```
不提交 = 没存档，别人 pull/reset 会把你的修改冲掉。

## 3. 禁止执行的命令（会清空别人未提交的修改）
- `git checkout .` / `git checkout -- .`
- `git reset --hard`
- `git pull`（除非先确认 `git status` 干净，并且用户明确要求）

## 4. 需要回退时
只用 `git revert <commit号>`，只撤销指定的一次提交。
绝对不要整文件回退（checkout/reset），那会连别人的修改一起清掉。

## 5. 重要版本额外备份
关键修复完成并验证后，同步备份到：
`/home/ubuntu/smart-locker/myfix_final_20260801/`
（复制文件过去，并更新 md5 校验）

## 6. 同一文件多个对话
如果发现另一个对话也在改同一个文件（git status / git log 有别人的提交），先沟通再改，不要互相覆盖。

## 7. 数据库/配置修改
修改数据库结构或生产配置前，先备份（pg_dump / cp 配置文件），改完验证再提交。

## 8. GitHub 同步安全（服务器 → GitHub 单向）
- 服务器代码/配置是生产真相源，禁止任何“从 GitHub 拉代码覆盖服务器”的操作：禁止 git pull、git checkout、git reset、强制推送（git push -f）。
- 只允许 服务器 → GitHub 普通快进推送。
- 查询 GitHub 状态必须用 SSH 通道：`git ls-remote github refs/heads/main`；https 的 origin/gitee 会挂起，不要用。
- 推送前先给远端当前 main 打备份标签：`git tag old-main-YYYYMMDD <远端main提交号>`，推送标签后再推分支。
- 推送后核对：远端 main 提交号 == 本地 HEAD。

## 9. 生产改动前全量备份
- 数据库：pg_dump -Fc
- 项目代码（含 .git）
- nginx 配置（含证书）、systemd 服务文件、crontab、acme 证书
- 备份范例：/home/ubuntu/backup_20260811_full/

## 10. 提交内容规范
- 只 git add 明确源码文件；.bak / .dump / .sql / logs / myfix_* 等临时文件不进仓库。
- 未跟踪的 .bak/备份文件是用户保留物：不要删、不要提交。

## 11. 当前状态快照（2026-08-11）
- 服务器分支 peruse-fix，GitHub main = d1dabbe（已同步）。
- 旧 main 备份标签：old-main-20260811 = cfd477c。
- 隐藏订单改造方案已确认逻辑但暂缓，等用户明确开工指令。