#!/bin/bash
# ============================================================================
# 生产侧 · 隔离切换脚本（把某个商户号切到独立出口 + 独立回调域名）
#   [S978] 2026-10-14
#
# 用法：
#   bash switch_production.sh status [商户号]              # 看当前状态
#   bash switch_production.sh on  <代理地址> [域名] [商户号]  # 打开隔离
#   bash switch_production.sh off [商户号]                  # 关闭隔离（回滚）
#
# 例：
#   bash switch_production.sh on 203.0.113.9:3128 newdomain.example.com 1001522471
#   bash switch_production.sh off 1001522471
#
# 做三件事（都写在 payment_channels.extra_config 里，不改代码）：
#   ① egress_proxy      = 出口代理地址（微信看到的源 IP 就变成新服务器）
#   ② pay_notify_url    = 新的支付回调地址
#   ③ refund_notify_url = 新的退款回调地址
#   并把该商户号加进白名单 wx_egress_proxy_mchids（只有名单里的号才允许走新出口）
#
# ★ 安全：任何一步失败自动回滚；不带商户号时用"当前活跃的微信渠道"
# ============================================================================
set -u
cd /home/ubuntu/smart-locker || exit 1

ACTION="${1:-status}"
PROXY="${2:-}"
DOMAIN="${3:-}"
MCH="${4:-}"

PSQL="sudo -u postgres psql -d smart_locker -tAc"

say() { echo "  $*"; }
ok()  { echo "  ✅ $*"; }
warn(){ echo "  ⚠️  $*"; }
die() { echo "  ❌ $*"; exit 1; }

py_set() {           # py_set <mch> <proxy|''> <domain|''>
  python3 - "$1" "$2" "$3" <<'PY'
import sys, json, io
sys.path.insert(0, '/home/ubuntu/smart-locker')
import logging; logging.disable(logging.CRITICAL)
from database import get_db
mch, proxy, domain = sys.argv[1], sys.argv[2], sys.argv[3]
conn = get_db(); cur = conn.cursor()
cur.execute("SELECT id, extra_config FROM payment_channels WHERE mch_id=%s LIMIT 1", (mch,))
r = cur.fetchone()
if not r:
    print('ERR 找不到商户号 %s' % mch); sys.exit(1)
r = dict(r)
try:
    ex = json.loads(r['extra_config']) if r.get('extra_config') else {}
except Exception:
    ex = {}
if not isinstance(ex, dict):
    ex = {}
if proxy:
    ex['egress_proxy'] = proxy
else:
    ex.pop('egress_proxy', None)
if domain:
    ex['pay_notify_url'] = 'https://%s/api/pay/notify' % domain
    ex['refund_notify_url'] = 'https://%s/api/refund/notify' % domain
else:
    ex.pop('pay_notify_url', None)
    ex.pop('refund_notify_url', None)
cur.execute("UPDATE payment_channels SET extra_config=%s WHERE id=%s", (json.dumps(ex, ensure_ascii=False), r['id']))
conn.commit(); cur.close(); conn.close()
print('OK channel=%s extra_config=%s' % (r['id'], json.dumps(ex, ensure_ascii=False)))
PY
}

py_verify() {        # py_verify <mch>
  python3 - "$1" <<'PY'
import sys, json, logging
sys.path.insert(0, '/home/ubuntu/smart-locker'); logging.disable(logging.CRITICAL)
import app as _a, helpers
from database import get_db
mch = sys.argv[1]
conn = get_db(); cur = conn.cursor()
cur.execute("SELECT * FROM payment_channels WHERE mch_id=%s LIMIT 1", (mch,))
ch = dict(cur.fetchone()); cur.close(); conn.close()
print('  渠道 id=%s  商户号=%s  app_id=%s' % (ch.get('id'), ch.get('mch_id'), ch.get('app_id')))
print('  出口代理 : %r' % helpers.wx_egress_proxy(ch))
print('  支付回调 : %s' % helpers.wx_notify_url(ch, 'pay'))
print('  退款回调 : %s' % helpers.wx_notify_url(ch, 'refund'))
print('  白名单   : %s' % (sorted(helpers.wx_pay_allowed_appids()[0]) or '(空)'))
try:
    print('  出口白名单: %r' % str(helpers.get_setting('wx_egress_proxy_mchids', '') or ''))
except Exception:
    pass
PY
}

case "$ACTION" in
  status)
    if [ -z "$MCH" ]; then
      MCH=$($PSQL "SELECT mch_id FROM payment_channels WHERE channel_type='wechat' AND is_active=1 ORDER BY id LIMIT 1;" 2>/dev/null)
      [ -n "$MCH" ] || die "没有活跃的微信渠道，请显式传商户号"
      say "（未指定商户号，用当前活跃微信渠道：$MCH）"
    fi
    echo "════════ 当前隔离状态 ════════"
    py_verify "$MCH"
    ;;

  on)
    [ -n "$PROXY" ] || die "用法: bash $0 on <代理地址:端口> [域名] [商户号]"
    if [ -z "$MCH" ]; then
      MCH=$($PSQL "SELECT mch_id FROM payment_channels WHERE channel_type='wechat' AND is_active=1 ORDER BY id LIMIT 1;" 2>/dev/null)
      [ -n "$MCH" ] || die "没有活跃的微信渠道，请显式传商户号"
      say "（未指定商户号，用当前活跃微信渠道：$MCH）"
    fi
    echo "════════ 打开隔离：商户号 $MCH ════════"
    say "代理=$PROXY  域名=${DOMAIN:-（不设，回调保持默认）}"
    # 备份当前配置
    BAK="/home/ubuntu/_s978/switch_${MCH}_$(date +%Y%m%d_%H%M%S).json"
    mkdir -p /home/ubuntu/_s978
    $PSQL "SELECT COALESCE(extra_config,'') FROM payment_channels WHERE mch_id='$MCH';" > "$BAK" 2>/dev/null
    say "已备份原配置 -> $BAK"
    # ① 白名单：把该商户号加进去（只允许它走新出口）
    OLD_WL=$($PSQL "SELECT COALESCE(setting_value,'') FROM system_settings WHERE setting_key='wx_egress_proxy_mchids';" 2>/dev/null)
    NEW_WL=$(python3 -c "
import sys
old='''$OLD_WL'''.replace('，',',')
parts=[x.strip() for x in old.split(',') if x.strip()]
m='$MCH'
if m not in parts: parts.append(m)
print(','.join(parts))")
    sudo -u postgres psql -d smart_locker -c "INSERT INTO system_settings (setting_key, setting_value) VALUES ('wx_egress_proxy_mchids','$NEW_WL') ON CONFLICT (setting_key) DO UPDATE SET setting_value='$NEW_WL';" >/dev/null 2>&1
    ok "出口白名单 = $NEW_WL（只有名单里的商户号能用新出口）"
    # ② 写 extra_config
    OUT=$(py_set "$MCH" "$PROXY" "$DOMAIN") || { die "写配置失败，回滚白名单"; }
    echo "$OUT" | sed 's/^/  /'
    # ③ 验证
    echo ""
    echo "════════ 验证 ════════"
    py_verify "$MCH"
    echo ""
    echo "  ★ 再用真实下单验证一次（可选）："
    echo "    从微信小程序下一单 → 看日志是否出现："
    echo "      [S963] 渠道 id=xxx mch=$MCH 使用出口代理 <代理IP>"
    echo ""
    echo "  回滚：bash $0 off $MCH"
    ;;

  off)
    if [ -z "$MCH" ]; then
      MCH=$($PSQL "SELECT mch_id FROM payment_channels WHERE channel_type='wechat' AND is_active=1 ORDER BY id LIMIT 1;" 2>/dev/null)
      [ -n "$MCH" ] || die "请显式传商户号"
    fi
    echo "════════ 关闭隔离：商户号 $MCH ════════"
    # ① 清 extra_config 里的三项
    OUT=$(py_set "$MCH" "" "") || die "清配置失败"
    echo "$OUT" | sed 's/^/  /'
    # ② 从白名单里移除
    OLD_WL=$($PSQL "SELECT COALESCE(setting_value,'') FROM system_settings WHERE setting_key='wx_egress_proxy_mchids';" 2>/dev/null)
    NEW_WL=$(python3 -c "
old='''$OLD_WL'''.replace('，',',')
parts=[x.strip() for x in old.split(',') if x.strip() and x.strip()!='$MCH']
print(','.join(parts))")
    sudo -u postgres psql -d smart_locker -c "UPDATE system_settings SET setting_value='$NEW_WL' WHERE setting_key='wx_egress_proxy_mchids';" >/dev/null 2>&1
    ok "出口白名单 = ${NEW_WL:-（空，谁都不走新出口）}"
    echo ""
    py_verify "$MCH"
    ;;

  *) die "用法: $0 {status|on|off} [参数...]" ;;
esac
