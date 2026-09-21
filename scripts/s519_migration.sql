-- [S519-20260921] 支付宝订单桥接：同一张订单上分别落"小程序侧 uid"和"支付侧 uid"
-- 只新增列，不改任何现有列/数据；微信那套完全不读这两列
ALTER TABLE orders ADD COLUMN IF NOT EXISTS alipay_mp_uid varchar(64) DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS alipay_pay_uid varchar(64) DEFAULT '';
COMMENT ON COLUMN orders.alipay_mp_uid IS '[S519] 支付宝小程序侧 uid（my.getAuthCode -> /alipay/login 的 user_id），应用 2021006199688688';
COMMENT ON COLUMN orders.alipay_pay_uid IS '[S519] 支付宝付款人 uid（回调 buyer_id / 查单 buyer_user_id），应用 2021006197675152';
-- 回滚：
-- ALTER TABLE orders DROP COLUMN IF EXISTS alipay_mp_uid;
-- ALTER TABLE orders DROP COLUMN IF EXISTS alipay_pay_uid;
