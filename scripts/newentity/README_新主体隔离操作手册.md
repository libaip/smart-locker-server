# 新主体隔离 · 操作手册（[S978] 2026-10-14）

## 目的
让新主体在微信支付侧与老主体**切断两处可见关联**：
1. **API 调用来源 IP** — 老主体走生产机出口（175.178.156.121），新主体走独立服务器
2. **支付回调域名** — 新主体用独立域名的 `notify_url`

代码侧已就绪（S963/S965/S970）：**填配置就生效，不用改代码**。

---

## 一、买机器（一次性）

| 项目 | 要求 | 说明 |
|---|---|---|
| 服务器 | 腾讯云轻量 2核2G，**地域选中国香港** | 香港免备案；约 40~60 元/月 |
| 系统 | Ubuntu 22.04 LTS | 脚本按 Ubuntu 写 |
| 域名 | 全新域名，别跟 kelaiwei/cqdyxl 有关联感 | 约 50 元/年；香港机器免备案 |

★ **机器必须"干净"**：不装旧代码、不跑企业微信客服、没给任何老商户号做过回调。
   （106 那台跳板机就是"脏"的，不能用来做隔离）

---

## 二、部署新服务器（一条命令）

把 `deploy_new_server.sh` 传到新服务器，然后：

```bash
sudo bash deploy_new_server.sh <生产服务器出口IP> [新域名]
# 例：
sudo bash deploy_new_server.sh 175.178.156.121 newdomain.example.com
```

脚本做四件事：
1. 装 tinyproxy（**只允许生产 IP 连**，且**只放行微信 API 域名**）
2. 装 nginx + Let's Encrypt 证书（只把 `/api/pay/notify`、`/api/refund/notify`
   反代回生产，其余路径 404）
3. 安全检查（不碰 SSH、不开会断连的防火墙）
4. 逐项验证

**演练模式**（只看会做什么，不改系统）：
```bash
sudo bash deploy_new_server.sh 175.178.156.121 test.example.com --dry-run
```

**验证代理可用**（在 172.16.0.2 上跑）：
```bash
curl -s -o /dev/null -w '%{http_code}\n' -x http://<新服务器IP>:3128 https://api.mch.weixin.qq.com/
# 期望 404（通）；403/timeout = 白名单没配对
```

**回滚**：
```bash
systemctl stop tinyproxy-newentity && systemctl disable tinyproxy-newentity
rm -f /etc/nginx/sites-enabled/newentity-gateway && systemctl reload nginx
```

---

## 三、生产侧切换（一条命令）

```bash
bash switch_production.sh status [商户号]              # 看当前状态
bash switch_production.sh on  <新IP>:3128 [新域名] [商户号]   # 打开隔离
bash switch_production.sh off [商户号]                  # 关闭（回滚）
```

**例**：
```bash
bash /home/ubuntu/smart-locker/scripts/newentity/switch_production.sh \
     on 203.0.113.9:3128 newdomain.example.com 1001522471
```

脚本做的事：
1. 把该商户号加进 `wx_egress_proxy_mchids` 白名单
   （★ **只有名单里的商户号才允许走新出口** —— 老主体/被风控的号进不来）
2. 往该渠道的 `extra_config` 写三项：
   - `egress_proxy` = 新服务器代理地址
   - `pay_notify_url` = `https://新域名/api/pay/notify`
   - `refund_notify_url` = `https://新域名/api/refund/notify`
3. 验证 + 打印回滚命令

★ 自动化备份：原 `extra_config` 存到 `/home/ubuntu/_s978/switch_<商户号>_<时间>.json`

---

## 四、验证清单（切完必查）

- [ ] 新服务器上代理已起：`systemctl is-active tinyproxy-newentity`
- [ ] 生产能经代理连微信：上面那条 curl 返回 404
- [ ] 白名单只含新主体商户号：`switch_production.sh status`
- [ ] 真实下一单 → 日志出现：
      `[S963] 渠道 id=xxx mch=<新商户号> 使用出口代理 <新IP>`
- [ ] 微信侧看到的源 IP = 新服务器（在微信商户平台看交易/或抓包确认）
- [ ] 回调域名 = 新域名（商户平台的"退款通知URL"也要手工改成新域名 ★ 这一步只能人工）

---

## 五、必须记住的三条红线

1. **106 不能当出口代理** —— 它跑过老商户号回调 + 企业微信客服，微信侧已有身份
2. **只有白名单里的商户号能走新出口** —— 老主体/被风控的号一律走原出口
3. **代理挂了不会偷偷回落直连** —— 宁可单笔失败，也不从旧 IP 发请求（S970 已验证）
