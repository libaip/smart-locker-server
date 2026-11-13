# 智能寄存柜 - 用户端微信小程序

智能寄存柜用户端微信小程序，提供便捷的物品寄存和取物服务。

## 功能特性

### 核心功能
- **扫码存包**：扫描柜体二维码，快速选择柜格并完成寄存
- **快速取物**：输入手机号和取物码，一键验证取物
- **订单管理**：查询手机号下所有存取记录
- **保证金管理**：微信支付保证金，取物后自动退还

### 业务流程

#### 存物流程
1. 扫描柜体二维码
2. 选择柜格大小（S/M/L）
3. 输入手机号
4. 设置4位取物码
5. 支付保证金
6. 开门放入物品

#### 取物流程
1. 输入手机号 + 取物码
2. 验证成功后开门
3. 选择「继续寄存」或「取物结束」
4. 取物结束后退还保证金

## 项目结构

```
smart-locker-user/
├── app.js              # 小程序入口
├── app.json            # 全局配置
├── app.wxss            # 全局样式
├── project.config.json # 项目配置
├── sitemap.json        # sitemap配置
├── utils/
│   ├── api.js          # API请求封装
│   └── util.js         # 工具函数
├── images/             # TabBar图标
└── pages/
    ├── index/          # 首页（扫码存包）
    ├── deposit/        # 存包页（4步流程）
    ├── retrieve/       # 取物页
    ├── orders/         # 订单页
    └── mine/           # 我的页面
```

## 配置说明

### API配置
API基础地址在 `utils/api.js` 中定义：
```javascript
const BASE_URL = 'http://106.55.7.10/api'
```

### AppID配置
在以下文件中替换占位符 `WX_APPID_PLACEHOLDER` 为实际的AppID：
- `project.config.json`
- `app.js` (globalData.appId)

### 支付模式
`app.js` 中的 `payMode` 配置：
- `mock`：模拟支付模式（开发调试用）
- `jsapi`：真实微信支付模式

## 页面说明

### 首页 (pages/index)
- 扫码存包入口
- 快速取物表单
- 使用说明

### 存包页 (pages/deposit)
- 步骤1：选择柜格大小（S/M/L）
- 步骤2：填写手机号和取物码
- 步骤3：支付保证金
- 步骤4：完成，显示取物码

### 取物页 (pages/retrieve)
- 验证手机号+取物码
- 显示订单信息
- 「继续寄存」或「取物结束」选项
- 退还保证金

### 订单页 (pages/orders)
- 手机号查询订单
- 订单列表展示
- 订单状态筛选

### 我的页 (pages/mine)
- 客服电话
- 使用规则
- 常见问题
- 关于我们

## API接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/cabinet/info` | GET | 获取柜体信息 |
| `/cabinet/<id>/slots` | GET | 获取柜格信息 |
| `/deposit/create-order` | POST | 创建存包订单 |
| `/store/pay` | POST | 支付确认 |
| `/deposit/retrieve` | POST | 取物验证 |
| `/deposit/continue-storage` | POST | 继续寄存 |
| `/deposit/end-storage` | POST | 结束寄存（退保证金） |
| `/sms/send` | POST | 发送验证码 |
| `/sms/verify` | POST | 验证短信验证码 |
| `/deposit/orders` | GET | 获取订单列表 |

## 开发说明

### 安装依赖
本项目为原生微信小程序，无需额外安装依赖。

### 编译预览
1. 打开微信开发者工具
2. 导入项目目录
3. 填入 AppID（替换占位符后）
4. 编译预览

### 注意事项
1. 首次使用需在微信公众平台配置服务器域名
2. 支付功能需要申请微信支付商户号
3. 手机号验证需要配置短信服务

## 版本信息
- 版本号：V1.0.0
- 开发日期：2024年

## 许可证
专有项目
