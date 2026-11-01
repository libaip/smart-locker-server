# 交接单：deposit.html 用户可见文案规范化（S328 已撤回，转交执行）

> 原会话 S328（任务 T-1790406579）已完成调研 + 试改 + 验证，**改动已撤回**（文件还原到改前版本），
> 本文件是可直接执行的完整清单。由接手会话重新 claim 后执行。

---

## 一、任务目标

把用户可见的存包页 `static/deposit.html` 文案做两件事：

1. **3 处「押金」→「预付款」**（微信官方能看到页面内容，避免被判押金业务）
2. **5 处「原路退回」→「退回至余额，可随时提现」**（页面承诺须与系统实际行为一致）

**为什么必须改**：系统实际行为是「取件后进余额 → 用户主动提现」（见 `routes/user.py:1330` 注释
"结束订单，预付款退到用户余额（不直接退微信）"，以及后端 message `routes/user.py:1375/2383`
"预付款已退至余额"）。而页面却承诺"0-3 个工作日内原路退回" —— 属于**不实告知**，
用户据此投诉"押金没原路退回"时，截图页面即成立。这是投诉的第二个源头。

微信可见性对照：

| 展示位置 | 微信能否看到 |
|---|---|
| 柜体设备屏幕（`usage_rules` WebSocket 推送） | ❌ 看不到 |
| **H5/小程序页面（本文件）** | ✅ **能看到**（小程序代码包审核 + webview 抓取 + 用户投诉截图）|
| 支付请求 body | ✅ 能看到（已规范化："寄存服务预付款…余额提现"）|

---

## 二、基本信息

| 项 | 值 |
|---|---|
| 目标文件 | `/home/ubuntu/smart-locker/static/deposit.html` |
| 改哪台 | **106 旧机**（106.55.7.10）改 → 同步到 **175 生产机**（175.178.156.121 / 内网 172.16.0.2）|
| 改前 md5 | `e76f6a78ea6e28c6bf7fc03d5577e5bb`（= git HEAD = 175 生产机当前版本，三者一致）|
| 改后 md5 | `bd0bcdb7f010d64e129a3b36897d262b`（原会话已实测得到此值）|
| 改动量 | 8 处，diff 恰好 16 行（8 行改 8 行），总行数不变 2997 |
| 编码 | UTF-8 无 BOM（改后首字节 `3c 21 44` = `<!D`）|

### 服务器访问方式（执行前必读）

| 目标 | 登录命令 |
|---|---|
| **106 旧机**（改代码、git、工作台） | `ssh -i "C:\Users\Admin（无密码）\.ssh\id_ed25519" ubuntu@106.55.7.10` |
| **175 生产机**（同步目标、重启服务） | 先登 106，再执行：`ssh -i /home/ubuntu/.ssh/prod_key -o StrictHostKeyChecking=no ubuntu@172.16.0.2`（公网可用 `175.178.156.121`）|

- 175 的免密密钥**已在 106 上**：`/home/ubuntu/.ssh/prod_key`（411 字节，已验证可用，连上后 hostname = `VM-0-2-ubuntu`）
- **175 无 .git**（这是正常的，别在 175 上跑 git 命令；代码改动必须经 106 存档）
- 生产库**只读**账号（仅用于核对数据，不可写）：
  `PGPASSWORD=locker_repl_2024 psql -h 172.16.0.2 -p 5432 -U replicator -d smart_locker`
  需要写库时必须 SSH 到 175 后 `sudo -u postgres psql -d smart_locker`（175 上 ubuntu sudo 免密）

### 工作规则文件（必须遵守）

| 文件 | 内容 |
|---|---|
| `/home/ubuntu/smart-locker/.workbench/WORKFLOW.md` | 标准作业流程：开工 → 调研 → 备份 → 改 → 校验 → 部署 → 提交 → 收工 |
| `/home/ubuntu/smart-locker/AGENTS.md` 第 13 节 | 工作台协作协议（claim / deploy_check / commit 带 by S<n> / release） |
| `/home/ubuntu/smart-locker/AGENTS.md` 第 14 节 | 主备互换后的代码修改 SOP（106 改 → 同步 175） |

**开工前必做两步**（原会话曾漏掉第 2 步）：
1. 看 `.workbench/TASKS.md`，确认目标文件没被登记「进行中」
2. 看 `git status`，确认没有别人未提交的修改（当前有：`routes/websocket.py`、`static/user-center.html`、`.workbench/TASKS.md`）

---

## 三、8 处逐条对照（行号基于改前版本）

### A 组：押金 → 预付款（4 处）

| # | 行 | 原文 | 新文 |
|---|---|---|---|
| 1 | 519 | `订阅后，“押金退还 / 退款到账”会用微信“服务通知”提醒你` | `订阅后，“预付款退还 / 退款到账”会用微信“服务通知”提醒你` |
| 2 | 638 | `alert('请在小程序或订单中心结束订单退押金')` | `alert('请在小程序或订单中心结束订单退预付款')` |
| 3 | 757 | `这样才能收到押金退还 / 退款到账的微信提醒` | `这样才能收到预付款退还 / 退款到账的微信提醒` |

### B 组：原路退回 → 余额口径（5 处）

| # | 行 | 原文 | 新文 |
|---|---|---|---|
| 4 | 429 | `5. 取出物品关好柜门，预付款原路退回` | `5. 取出物品关好柜门，预付款退回至余额，可在钱包提现` |
| 5 | 572 | `请支付后使用设备，结束使用后预付款可申请原路退回。` | `请支付后使用设备，结束使用后预付款退回至余额，可随时提现。` |
| 6 | 788 | `1. 用户取包并关好柜门后，预付款将在0-3个工作日内原路退回至支付账户。` | `1. 用户取包并关好柜门后，预付款将即时退回至小程序余额，可在钱包提现。` |
| 7 | 790 | `3. 退款仅支持原路退回，不支持更换退款账户。` | `3. 提现仅支持退回原支付账户，不支持更换账户。` |
| 8 | 831 | `A: 取包成功后0-3个工作日内原路退回。` | `A: 取包成功后预付款即时退回小程序余额，可在「我的余额」提现。` |

**⚠️ 引号是全角 `“ ”`，替换必须逐字节精确匹配。**

### 各处的用户场景（便于理解影响面）

| 行 | 界面位置 | 用户何时看到 |
|---|---|---|
| 429 | 首页 →「使用帮助」→ 5 步流程第 5 步 | 点"帮助" |
| 519 | 订阅引导弹窗 | 引导订阅通知时 |
| 572 | 支付页 ⚠️ 警告条（"支付开门"按钮正上方） | **付款前最后一眼** |
| 638 | 取包页隐藏按钮的 alert（当前 `display:none`） | 兜底触发时 |
| 757 | 跳小程序提示文案 | 跨端跳转时 |
| 788 / 790 | 《用户预付款协议》→ 二、预付款退还 第 1 / 3 条 | 点协议链接阅读 |
| 831 | 帮助弹窗 → 常见问题 FAQ | 点帮助拉到最下面 |

> 补充：同一帮助弹窗上方（L823-825）**已经是正确口径**（"预付款退回至余额""可在「我的余额」中查看和提现"），
> 只有 L831 的 FAQ 是漏改的旧口径 —— 同一屏内自相矛盾。

---

## 四、现成工具（原会话留在 106 上）

| 路径 | 用途 |
|---|---|
| `/tmp/fix_wording.py` | **试运行**：只统计 8 处匹配次数，不写文件（正确结果：8 个 `count=1`，最后打印 `DRYRUN_RESULT: ALL_MATCH`）|
| `/tmp/fix_wording_apply.py` | **执行替换**：带 `assert count==1` 保护，任一不匹配即中止不写盘 |
| `/home/ubuntu/smart-locker/backups/static_deposit.html.bak_20260926_151200` | 改前备份（md5 `e76f6a78`，回滚用）|

执行方式：
```bash
cd /home/ubuntu/smart-locker
python3 /tmp/fix_wording.py          # 先试运行，确认 ALL_MATCH
python3 /tmp/fix_wording_apply.py    # 再实际替换
```

---

## 五、验证方法（改完自检，全部应通过）

```bash
cd /home/ubuntu/smart-locker
md5sum static/deposit.html                       # 应为 bd0bcdb7f010d64e129a3b36897d262b
grep -c '押金' static/deposit.html                # 应为 0
grep -c '原路退回' static/deposit.html            # 应为 0
grep -c '退回至余额' static/deposit.html          # 应为 3
grep -o '<script' static/deposit.html | wc -l    # 应为 6
grep -o '</script>' static/deposit.html | wc -l  # 应为 6
head -c 3 static/deposit.html | od -An -tx1      # 应为 3c 21 44（无 BOM）
diff backups/static_deposit.html.bak_20260926_151200 static/deposit.html | grep -cE '^[<>]'   # 应为 16
```

---

## 六、部署流程与已知坑

### 标准流程（WORKFLOW.md）
1. **开工前**：查 `.workbench/TASKS.md`（确认没人登记本文件「进行中」）+ **查 `git status`（确认无别人未提交修改）**
2. `bash .workbench/claim.sh "说明" "static/deposit.html"`
3. 备份 → 改 → 校验
4. `bash .workbench/deploy_check.sh static/deposit.html`
5. **rsync 同步到 175**（175 也备份一份）
6. 106 `git add static/deposit.html` + `git commit -m "… by S<n>"`
7. `bash .workbench/release.sh <任务ID>`

### ⚠️ 坑 1：deploy_check 可能误拦
原会话遇到：claim 记录的基线 md5 = `e9b8b9afbce5510894a83ae6af2497c0`（**异常值**，既不是 HEAD 也不是当时实际文件），
而实际改前文件 = HEAD = `e76f6a78`。原因推测：claim 执行瞬间文件正处于某个中间状态。
**处理**：确认改前文件确为 `e76f6a78`（= HEAD = 175 生产机）后，加 `--force` 放行。

### ⚠️ 坑 2：工作区有别人未提交的修改
执行时 `git status` 显示（非本次任务的文件）：
```
 M .workbench/TASKS.md
 M routes/websocket.py
 M static/user-center.html
```
**处理**：commit 时**只** `git add static/deposit.html`，**绝不** `git add -A`；
rsync 也只同步这一个文件，**不要整目录同步**（会把别人未完成的代码带到生产）。

### ⚠️ 坑 3：静态文件无需重启服务
`/store` 是按请求读取 `static/deposit.html` 渲染（app.py:822 读盘做占位替换），
改静态文件即时生效，**无需 HUP / restart**（参考 TASKS.md 里 S530-B 的部署记录）。

### 备份位置惯例
历史任务（S515/S516/S530-B）的备份都放在 **175 生产机** 的 `backups/` 下
（如 `backups/s530_alipay_pure_mp_2240/static/deposit.html`），建议同步时在 175 也存一份。

---

## 七、相关但本次不改（供参考）

| 项 | 结论 |
|---|---|
| 后端 message | 已是"退至余额"口径（`routes/user.py:1375/2383`），与本次改动一致 ✅ |
| `routes/user.py:6231` | "退款成功，将原路退回支付账户" 属于**提现**场景，措辞正确，**不改** |
| `static/profile.html` / `re-scan.html` / `alipay-guide.html` | 已扫描，**0 处"押金"** ✅ |
| 后台页面（`admin-v2.html`、`user-center.html`）| 有"押金"字样但**管理员可见、微信看不到**，优先级低 |
| 柜体设备屏规则（`usage_rules`）| 微信看不到，**不用改** |
| 支付 body（`helpers.py:1122` `PAY_GOODS_NAME`）| 已是"寄存服务预付款，使用完剩余款项在小程序钱包进行余额提现" ✅ 无需改 |

---

## 八、背景（为什么做这件事）

微信支付通过 6 个信源判定业务形态：进件资料 / 小程序类目与页面 / 支付字段 / 交易特征 / 投诉语义 / 资金流。
其中 **H5 页面文案**属于"小程序页面"信源，微信能看到。
当前页面同时存在两个问题：**① 出现"押金"字样；② 承诺"原路退回"但实际退到余额**（不实告知）。
两者都会强化风控对"押金业务"的判定，并给用户投诉提供依据。

治本方向（不在本任务范围）：取件后**自动原路退款**，用户零操作；
或申请**微信支付分免押**从源头消灭押金。本任务只做文案与系统实际行为对齐。
