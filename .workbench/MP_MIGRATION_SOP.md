# 更换小程序（appid）完整迁移 SOP

> 首次总结：2026-09-10（科莱智 → 伧置 迁移实战）
> 适用场景：把系统从一个微信小程序（appid）整体迁到另一个小程序（同主体，unionid 一致）
> 目标：按本清单从上到下跑完，不漏项、不踩坑

---

## 0. 迁移对象与关键标识（本次实例，下次按实际替换）

| 项 | 旧 | 新 |
|----|----|----|
| 小程序名称 | 科莱智 | 伧置 |
| 小程序 appid | `wx57eaea52dcfff4e8` | `wxcabd4cbdb3096c4b` |
| **mp_openid 前缀** | **`oWrA8`**（实际 `oWrA81...`） | **`ooTcRx`** |
| 公众号 openid 前缀 | `oLhbm2`（**发不了订阅消息，必须排除**） | 同左（不变） |
| 主体 | 重庆科莱维科技有限公司（91500106MACK2R5P8G） | 同左（不变，故 unionid 一致） |

> ⚠️ **openid 前缀是迁移的头号坑**：每个 appid 的 openid 前缀是随机的，迁移后**所有硬编码前缀判断都要改**。

---

## 1. 配置层（改 appid / secret）

- [ ] `config.py`：`WX_MP_APP_ID` / `WX_MP_APP_SECRET` → 新 appid 的
- [ ] `.env`：`WX_MP_APP_ID` / `WX_MP_APP_SECRET` → 新 appid 的
- [ ] **清理缓存的旧 access_token**：`system_settings` 里 `wx_mp_access_token` 之类（**不清会导致 H5 跳转仍进旧小程序**）
- [ ] 验证：`python3 -c "from config import WX_MP_APP_ID; print(WX_MP_APP_ID)"`

```bash
# 检查 access_token 缓存
psql -h 127.0.0.1 -p 6432 -U locker_admin -d smart_locker -c \
 "SELECT key, LEFT(value,40) FROM system_settings WHERE key ILIKE '%access_token%';"
```

---

## 2. openid 前缀迁移（核心，最容易漏）

**涉及文件**：`helpers.py`、`routes/payment.py`、`routes/admin_v2.py`、`routes/user.py`

- [ ] 全局替换旧前缀判断：`oWrA8` → `ooTcRx`（**注意：`oLhbm2` 公众号排除逻辑不要动**）
  ```bash
  grep -rn "oWrA8" helpers.py routes/*.py | grep -v "\.bak"
  # 逐处替换 startswith('oWrA8') → startswith('ooTcRx')、LIKE 'oWrA8%%' → LIKE 'ooTcRx%%'
  ```
- [ ] `helpers.py` 新增集中判断函数：
  ```python
  def is_mp_openid(v):
      """判断是否(新)小程序 openid：新 appid 前缀 ooTcRx；公众号(oLhbm2)返回 False"""
      return bool(v) and str(v).startswith('ooTcRx')
  ```
- [ ] `send_wx_subscribe_message`（helpers.py）加固：
  - 拿到非 `ooTcRx` 前缀 openid（旧 `oWrA8` / 公众号 `oLhbm2`）时 → **尝试换同 unionid 的新 `ooTcRx` openid**
  - 换不到 → **跳过发送（return False）**，避免微信返回 `40003 invalid openid`
- [ ] 兼容 cursor 类型：守卫里取字段要兼容 `RealDictCursor`(dict) 和普通 cursor(tuple)
  ```python
  _gid   = _g['id']    if isinstance(_g, dict) else (_g[0] if _g else None)
  _gphone= _g['phone'] if isinstance(_g, dict) else (_g[1] if _g else None)
  ```

**检查命令**：
```bash
# 应无输出（除 .bak）
grep -rn "oWrA8" helpers.py routes/*.py | grep -v "\.bak" | grep -v "\.syncbak"
```

---

## 3. 订阅消息模板 id（3 个，全部换新）

| 用途 | 模板 id | 字段 |
|------|---------|------|
| **寄存成功** | `Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk` | thing1 / character_string9 / thing2 / amount3 / time4 |
| **押金退还** | `PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA` | amount1 / time2 / thing3 / thing4 |
| **退款成功** | `lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c` | time5 / amount2 / thing4 / thing3 |

- [ ] 后端所有 `send_wx_subscribe_message(...)` 调用点模板 id → 新 id，**字段名要对齐**（字段不符会报错）
  ```bash
  grep -rn "YsfB8FH4\|5OZIN\|nG8Cdhn\|UT0PehBf" routes/ helpers.py | grep -v "\.bak"   # 旧模板id应无残留
  ```
- [ ] 后端 `/user/subscribe-templates` 返回新 3 个：
  ```python
  _withdraw='lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c'   # 退款成功
  _general ='PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA'   # 押金退还
  _deposit ='Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk'   # 寄存成功
  # 同时返回 templates 数组 + withdraw_notify/general_notify/deposit_notify
  ```

---

## 4. 前端小程序（本地源码目录）

> 本地路径示例：`D:\工具配置迁移包_20260811\小程序_用户2`

- [ ] `app.js` 的 `appId` → 新 appid
- [ ] `project.config.json` 的 `appid` → 新 appid
- [ ] `utils/api.js`：模板常量 → 新 id；所有查询接口**带 `unionid`**：
  ```js
  data.unionid = unionid || wx.getStorageSync('unionid') || ''
  ```
- [ ] **存包订阅授权要包含 3 个模板**（关键坑！）：
  - `pages/deposit/deposit.js` 的 `requestSubscribe()`：
    ```js
    var tmplIds = [TEMPLATE_WITHDRAW, TEMPLATE_GENERAL, TEMPLATE_DEPOSIT]  // 必须含寄存成功
    // 动态读取时也要 push t.deposit_notify
    if (t.deposit_notify) ids.push(t.deposit_notify)
    ```
  - `pages/subscribe/subscribe.js`：读 `res.data.templates`（含 3 个）
  - 微信 `wx.requestSubscribeMessage` 一次最多 3 个模板，正好
- [ ] 登录页**去掉旧小程序品牌字样/logo**（如"微信"字样、微信绿圆图标）
- [ ] 提现规则统一（**微信审核必查项**）：提现页清晰展示 4 项
  ```
  1. 可提现额度为您的账户当前余额
  2. 每日提现次数：不限次数
  3. 提现时间：00:00-24:00，全天可操作
  4. 提现将在0-3个工作日内原路退回支付账户
  5. 如有疑问请联系客服 4006981080
  ```
  - 统一"到账时间"口径（0-3个工作日）、修正客服电话（曾误写成 40006**981**080）
  - **同一文案在多个位置要一致**：`withdraw.wxml` / `mine.wxml` / `wallet.js` / `transactions/withdraw-progress/withdraw-record`
- [ ] 全项目扫旧文案/旧模板id：
  ```powershell
  Get-ChildItem -Recurse -Include "*.js","*.wxml" | Select-String "大于0元|1-3个工作日|40006981080|YsfB8|5OZIN"
  ```

---

## 5. H5 / 静态页面

- [ ] `static/h5/index.html`、`static/deposit.html`、`templates/store.html`：旧 appid → 新 appid
- [ ] **H5 跳小程序必须用官方 scheme**（`weixin://dl/business/?appid=<新>` 会报"当前页面无法访问"）：
  - 走后端 `/api/wx/generate-scheme` 拿 `weixin://dl/business/?t=xxx`
  - 前端 `fetch` 该接口后跳转，失败再回退
- [ ] H5 提现规则/到账文案统一（同上第 4 节口径）
- [ ] 部署后验证：`curl -s https://locker.cqdyxl.com/static/h5/wallet.html | grep 4006981080`

---

## 6. 身份/数据层（unionid）

- [ ] 用户侧查询**按 unionid**（不按手机号）：
  - 前端 `getOrders/getBalance/getUserInfo/getTransactions/getWithdrawals` 传 `unionid`
  - 后端 `get_user_orders` / `get_user_balance` / `/user/info` 等接收并优先用 `unionid` 解析身份
  - 效果：**用户换手机号不影响小程序显示数据**（因为按 unionid 查）
- [ ] `upsert_phone_openid_row`（helpers.py）加守卫 **"UN 只认已绑定的第一个手机号"**：
  - 若该 unionid 已绑定过手机号（`ORDER BY id ASC LIMIT 1`），本次传入的 phone **不再新增行**，只补记 openid/mp_openid
  - 目的：防止"同一 unionid 因换/错手机号复制出新身份"
  - **老数据不动**（仅防新增）
- [ ] 会员管理同一手机号重复行修复（`routes/admin_v2.py` 的 `admin_members`）：
  - 根因：`user_balances LEFT JOIN phone_openids ON phone` 展开成多行（phone_openids 允许同一 phone 多条）
  - 修复：改 JOIN 聚合子查询
    ```sql
    LEFT JOIN (
      SELECT phone,
             MAX(NULLIF(unionid,''))    AS unionid,
             MAX(NULLIF(mp_openid,''))  AS mp_openid,
             MAX(NULLIF(gzh_openid,'')) AS gzh_openid,
             MAX(NULLIF(openid,''))     AS openid
      FROM phone_openids WHERE NULLIF(phone,'') IS NOT NULL GROUP BY phone
    ) mp ON mp.phone=ub.phone
    ```
  - 验证：新查询行数 = `user_balances` 行数

---

## 7. 部署（双节点 106 + 175）

> 遵循 `.workbench/WORKFLOW.md`：**代码修复**在主目录改，双节点同步；**数据修复只在 175 生产库**

- [ ] 175（生产 CVM）：`175.178.156.121` / 内网 `172.16.0.2`，服务 = systemd
  - `smart-locker.service`（5001 用户端 8 workers）
  - `smart-locker-admin.service`（5002 管理端 2 workers）
- [ ] 106（旧机/代码源）：`106.55.7.10`，同样 5001/5002 systemd
- [ ] 同步流程（以 175 → 106 为例）：
  ```bash
  # 1) 106 备份
  cp <file> <file>.syncbakN_$(date +%Y%m%d_%H%M%S)
  # 2) 175 拉 → 106 推（本地中转 scp）
  # 3) 验证 md5 一致 + python3 -c "import ast; ast.parse(...)"
  # 4) 重启
  sudo systemctl restart smart-locker.service smart-locker-admin.service
  # 5) 验证服务 active + 只读冒烟
  # 6) 清理本次 .syncbakN_* 备份
  ```
- [ ] 重启命令（106 用 `sudo -n`）：
  ```bash
  sudo systemctl restart smart-locker.service
  sudo systemctl restart smart-locker-admin.service
  systemctl is-active smart-locker.service smart-locker-admin.service
  ```

---

## 8. 验证清单（上线后必做）

- [ ] `/user/info` 返回新提现规则 5 条 + 客服电话 4006981080
  ```bash
  curl -s "http://127.0.0.1:5001/api/user/info?phone=<测试号>"
  ```
- [ ] `/user/subscribe-templates` 返回 3 个新模板 id
- [ ] 存包 → 收到**寄存成功**订阅（Q3Fts5）
- [ ] 取包 → 收到**押金退还**（PtRJgP）/ **退款成功**（lJpnAU）
- [ ] 订阅发送日志检查（**成功日志 openid 只有 8 位截断、且不含 phone**，无法精确关联订单）：
  ```bash
  journalctl -u smart-locker --since "today" --no-pager | grep subscribe_msg
  # errcode 含义：
  #   0     = 成功
  #   43101 = 用户拒绝/未授权订阅（用户行为，正常）
  #   40003 = invalid openid（openid 前缀错/不属于本 appid → 代码或数据未迁移）
  ```
- [ ] 后台会员管理：同一手机号只显示一行
- [ ] H5 跳小程序能正常打开新小程序

---

## 9. 已知坑位速查

| 现象 | 根因 | 处理 |
|------|------|------|
| H5 跳转仍进旧小程序 | `system_settings` 缓存旧 access_token | 清缓存 + 强制刷新 token |
| 订阅报 `40003 invalid openid` | 用了旧 appid 的 openid（`oWrA8`）发新小程序 | 前缀判断改 `ooTcRx` + 旧换新/跳过 |
| 存包后收不到寄存成功 | 前端存包时没请求 `deposit_notify` 模板授权 | `deposit.js` 加 `TEMPLATE_DEPOSIT` |
| 换手机号后小程序空数据 | 查询按手机号 | 改为按 `unionid` 查 |
| 同一手机号两个身份 | `phone_openids` 允许同 phone 多条 + LEFT JOIN 展开 | 加"UN 只认首手机号"守卫 + 后台聚合去重 |
| 微信审核被拒（提现规则） | 提现页未清晰展示额度/次数/时间/到账 | 按第 4 节 5 条规则统一展示 |
| 客服电话显示错 | 误写 40006**981**080 | 统一 `4006981080` |

---

## 10. 绝对不要做

- ❌ 用 `conn.rollback()` 在生产库做"测试不落库"——**该环境连接常是 autocommit，回滚无效会污染真实数据**
  - 正确做法：**只读 SELECT 验证**，或显式 `conn.autocommit=False` + 手动 BEGIN
- ❌ 基于日志重建"哪些订单没发订阅"来做补发（成功日志 openid 截断、无 phone，无法精确关联）
- ❌ 直接改 `phone_openids` 老数据（除非明确要求；优先改查询/加守卫）
- ❌ 在 106 库做数据修复（106 不是生产库）
