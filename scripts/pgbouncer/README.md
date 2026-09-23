# pgbouncer 运维存档（S646 / 2026-09-24）

本目录保存 **175 生产上 pgbouncer 相关的服务器配置改动**。这些文件位于生产的 `/etc` 下、**不在应用仓库里**，
故在此存档一份，保证「改了什么、为什么改、怎么回滚、怎么复现」可追溯（工作规则：改动先落 106，再同步 175）。

## 背景（一句话）

`systemctl is-active pgbouncer` 显示 `failed`，但守护进程一直正常服务。
根因：**启动脚本与 pgbouncer 自己的 pidfile 路径不一致**，启动脚本"看不见"正在跑的实例。

## 事实与证据（详见 `D:\.codex\docs\smart-locker\ios跳转诊断_20260917\S646_pgbouncer状态与日志隐患调查_20260924.md`）

| 项 | 值 |
|---|---|
| 启动脚本 pidfile | `/etc/init.d/pgbouncer` → `PIDDIR=/var/run/postgresql` → `/var/run/postgresql/pgbouncer.pid`（**不存在**） |
| pgbouncer 实际 pidfile | `pgbouncer.ini` → `pidfile = /tmp/pgbouncer.pid`（内容 `1368`） |
| 直接证据 | `/tmp/pgbouncer.log`：`2026-09-22 14:36:28.190 CST [1693379] FATAL unix socket is in use, cannot continue` |
| 触发机制 | 多余的一次 `start` → ssd 因 pidfile 不存在认为"没在跑" → 拉起第二个实例 → 抢不到 6432 → 自杀 → 启动脚本 `exit 1` → unit `failed`；**原进程 1368 未受影响** |
| 真实风险 | 修复前 `systemctl stop` 是空转（`No /usr/sbin/pgbouncer found running; none killed.`）、`restart` 是"空转+新实例自杀"= **假重启**，排障时会被误导 |
| 开机自启 | 正常（`uptime -s` = 2026-08-22 21:46:59，pgbouncer 21:47:09 起来；unit `enabled`） |
| 监控依赖 | 无（cron/timer/仓库 `grep is-active.*pgbouncer` 0 处）；应用 `/api/health` 会真跑 `SELECT 1` 经 6432，才是正确探针 |

## 已执行的改动（S646-B，2026-09-24 07:02）

`/etc/init.d/pgbouncer` **仅 1 行**：

```diff
@@ -18 +18 @@
-PIDDIR=/var/run/postgresql
+PIDDIR=/tmp
```

- md5：`c668252e7f10e9ab3c5813e2ba5d4d1d` → `decff3cbb757e7027e02a20431f154de`
- 手法：`systemctl daemon-reload` → `reset-failed` → `start`（ssd 报 `already running` + 脚本带 `--oknodo` → 返回 0 → unit 转 active，**进程未被触碰**）
- 结果：`is-active` `failed` → **`active`**（`SubState=running`、`Result=success`）；PID 仍 **1368** 且仅 1 个；6432 accepting；health 5001/5002 均 200
- 生产备份：`175:/home/ubuntu/backups/s646_175_before_20260924_070203/`（`pgbouncer.init` + `pgbouncer.ini`）
- 回滚：`cp -a <备份>/pgbouncer.init /etc/init.d/pgbouncer && systemctl daemon-reload`
  ⚠️ 回滚后**不要**用 `systemctl stop`（那时它又能真的停服务了）；如需停服务请 `kill -TERM $(cat /tmp/pgbouncer.pid)` 后手工 `/usr/sbin/pgbouncer -d /etc/pgbouncer/pgbouncer.ini`

## 日志隐患与处置（S646-A）

| 事实 | 值 |
|---|---|
| pgbouncer 日志 | `pgbouncer.ini` → `logfile = /tmp/pgbouncer.log` |
| 清空前 | **4.6 GB / 30,662,643 行**，约 140 MB/天 |
| 为什么没人能清 | 文件带 **`chattr +a`（append-only）**：连 root 都不能截断/删除；`logrotate` 永远无法生效 |
| 另一道锁 | `fs.protected_regular`：root 也不能在 sticky 的 `/tmp` 里用 `O_CREAT` 方式打开别人的文件（这正是 `truncate`/`: >`/`dd` 失败、而 logrotate `copytruncate` 成功的原因） |
| logrotate 现状 | 包自带规则指向 `/var/log/postgresql/pgbouncer.log`（该文件一直是 0 字节）→ **"该管的没管"** |
| 处置 | 老板批准去掉 `+a` → `logrotate -f` 实测成功（copy+truncate）→ **根分区 20G→16G，可用 27G→32G** |

- 已在 175 新建 `/etc/logrotate.d/pgbouncer`（见 `logrotate.d-pgbouncer.tmp`：`size 200M` / `rotate 7` / `compress` / `delaycompress` / `copytruncate` / `su postgres postgres`），`logrotate -d` 语法通过、`-f` 实测成功。
- 另清理**我们自己的**旧日志：`today.log`(154M)、`win24.log`(146M)、`s631_pre6h.log`(30M)、`verify_debug.log`(11M)、`psql_err.log`(5.0M)。

## 下一步：S646-C（计划内，凌晨 4 点，见 `s646c_regularize.sh` + `cron.d-s646c`）

把日志与 pidfile 从 `/tmp` 挪到正规位置，并把启动脚本还原为包默认，使两者**天然一致**：

- `logfile = /tmp/pgbouncer.log` → **`/var/log/postgresql/pgbouncer.log`**（不再受 /tmp 清理策略影响）
- `pidfile = /tmp/pgbouncer.pid` → **`/var/run/postgresql/pgbouncer.pid`**（与包默认启动脚本一致；S646-B 的本地补丁随之还原，避免被包升级覆盖后再次错位）
- 代价：pgbouncer 需**真重启一次**（<1 秒连接中断，在途请求会失败）→ 故在凌晨低峰执行
- 脚本自带：前置安全门（health 200×2 + **待处理投诉 = 0**）、失败手工兜底、再失败**自动回滚**、收尾重写 logrotate 指向新路径并清理 /tmp 旧日志、成功后**自删 cron 条目**
