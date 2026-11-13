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

## 12. 微信投诉回调配置（2026-08-19 全量核配）
- 每个商户号必须在微信侧配置投诉通知回调地址，否则投诉不会实时推送（只能靠 5 分钟 cron 兜底登记，延迟 0~5 分钟+）。
- 回调地址统一：`https://locker.cqdyxl.com/api/admin_v2/wechat-complaint/notify`
- 配置/校验 API（商户证书签名）：
  - 查：`GET /v3/merchant-service/complaint-notifications`
  - 配：`POST /v3/merchant-service/complaint-notifications`，body `{"url":"https://locker.cqdyxl.com/api/admin_v2/wechat-complaint/notify"}`
  - 改：先 `DELETE /v3/merchant-service/complaint-notifications` 再 POST（已存在时直接 POST 会报 PARAM_ERROR 数据已存在）
- **新增商户号后必须补配**，校验脚本：`python3 /home/ubuntu/smart-locker/scripts/check_complaint_notify_url.py`（69 个商户已全部配置，2026-08-19）
- 常见坑：
  - 地址少写 /admin_v2/ 段（曾发生 1748250234）；商户号含 404 数字（如 1749404244）不是未配置，看返回体 `RESOURCE_NOT_EXISTS` 判断。
  - **结案 SIGN_ERROR（401 证书序列号有误）**：调度器查证书时带 `AND is_active=1` 会把 is_active=0 的渠道（如 1749584092）漏掉，退回用 config 主商户序列号签名导致微信验签失败。**查证书一律不要加 is_active 过滤**（2026-08-19 已修）。存量补结案脚本：`/tmp/fix_complete_backlog.py` 思路（从 payment_channels 按 mch_id 取 cert_serial_no + cert_name，POST complete）。
- 实时链路（2026-08-19 三段式）：回调(验签+解密) → complaints 表 status=0 → 秒回首响 status=1 → 满 5 分钟调度器退款 → 到账通知 → 结案 status=3；退款失败重试 3 次转人工 status=2。话术常量在 routes/admin_v2.py 顶部 WECHAT_* 。
- 验证命令：`sudo journalctl -u smart-locker -f | grep wechat_complaint_notify`；或 `./verify_complaint_flow.sh <订单号>`

## 13. 工作台协作协议（2026-08-20 起强制）

多会话同时改代码会互相覆盖，**所有会话必须遵守**：

1. **改任何文件前，先看 `.workbench/TASKS.md`**：目标文件被登记为「进行中」则不许碰，先协调。
2. **开工必登记**：`bash .workbench/claim.sh "说明" "文件1,文件2"`（自动分配会话编号 S1/S2…并记录文件 md5 基线）。
3. **部署前必校验**：`bash .workbench/deploy_check.sh <文件1> <文件2>`——文件开工后被别人动过会**拦截**；确认无冲突后加 `--force` 放行。**禁止跳过校验直接部署**。
4. **提交带会话编号**：commit message 末尾加 `by S<n>`，可追溯。
5. **收工必登记**：`bash .workbench/release.sh <任务ID>` 标记完成。

## 14. 主备互换后的代码修改SOP（2026-08-23 起，方案A，所有会话必须遵守）

### 机器角色（已互换，勿搞错）
| 机器 | IP | 角色 | 做什么 |
|---|---|---|---|
| **旧机**（轻量） | 106.55.7.10 | **代码源 + Git仓库 + 工作台** | 改代码、备份、claim/deploy_check/release、git commit/push 都在这 |
| **新机**（CVM） | 175.178.156.121 | **生产运行环境** | 应用/nginx/PG/证书在这跑；**无 .git**（rsync 排除），禁止任何 git 命令 |

### 标准流程（改代码必走）
1. 旧机看 `cat .workbench/TASKS.md`：目标文件被登记「进行中」→ 不许碰，先协调
2. 旧机 `bash .workbench/claim.sh "说明" "文件1,文件2"` 登记（拿 S编号）
3. 旧机备份：`cp <文件> backups/<文件>.bak.<时间戳>`
4. 旧机改代码 → `python3 -m py_compile <文件>` 通过
5. 旧机 `bash .workbench/deploy_check.sh <文件>`（被拦先确认无冲突再 --force；注意多claim匹配bug，S5修复中）
6. **部署到新机（在新机上执行拉取）**：
   ```bash
   # 新机执行：从旧机拉单文件
   rsync -a -e "ssh -o StrictHostKeyChecking=no" ubuntu@106.55.7.10:/home/ubuntu/smart-locker/<文件路径> /tmp/
   sudo cp /tmp/<文件名> /home/ubuntu/smart-locker/<文件路径>
   cd /home/ubuntu/smart-locker && python3 -m py_compile <文件路径>
   # 按改的文件重启对应服务（新机）
   sudo systemctl restart smart-locker        # 主API(5001)
   sudo systemctl restart smart-locker-admin  # 后台/投诉(5002)
   sudo systemctl restart ws-proxy            # WebSocket(5004)
   sudo systemctl restart wecom-kf            # 企微客服(5005)
   ```
7. 新机验证：`curl -s http://127.0.0.1:5001/api/health` + `sudo journalctl -u <服务> -n 30 --no-pager`
8. 旧机提交推送（老规矩）：
   ```bash
   git add <文件>
   git commit -m "说明 by S编号"        # 必须带 by S#，hook会拦
   git tag old-main-$(date +%Y%m%d) $(git ls-remote github refs/heads/main | cut -f1)
   git push github old-main-$(date +%Y%m%d)
   git push github peruse-fix:main
   ```
   ⚠️ 推送**只用 SSH remote `github`**（git@github.com:libaip/smart-locker-server.git）；`gh`/`origin` 是 https 无凭证会挂起
9. 旧机 `bash .workbench/release.sh <任务ID>` 标记完成

### 红线
- **新机无 .git**：git 命令报错正常，别在新机跑 git；代码改动必须经旧机存档，否则 rsync 覆盖丢失
- **别跑完整 `sync_from_primary.sh`**（脚本 PRIMARY 指向旧版、且会同步 crontab：旧机 crontab 为空，会把新机 13 条生产 cron 清掉）——脚本修正前一律用上面的单文件 rsync
- 旧机 crontab 为空是**正常状态**（备机不跑任务）；新机 13 条 cron 是生产
- 数据库 DDL：新主是逻辑复制发布者，**改表结构后必须在备机(旧机)补 schema**（对比 information_schema，见 smart-locker-迁移CVM方案.md §7.3）
- 证书/nginx 改动在新机（/etc/letsencrypt、/etc/nginx），改完 `nginx -t && systemctl reload nginx`
- 多会话冲突：TASKS.md 铁律 + deploy_check 双保险；`routes/admin_v2.py` 被 S30 登记（柜门任务），改前先协调
6. 完整流程见 `.workbench/WORKFLOW.md`（开工→调研→备份→改→校验→部署→提交→收工）。