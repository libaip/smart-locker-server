#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S1495-A] 订单身份健康检查：openid 空占比超阈值就告警

背景（2026-10-20）：
  今天 1,425 笔押金>0 订单里 198 笔（14%）mp_openid 为空，10-11 点集中爆发 123 笔。
  根因：公众号停用 + 用户从 H5 进 → /api/wx/oauth 用空 appid 换 openid 必失败
       → 订单没身份 → 订阅消息发不出去、行为画像失效 → 用户收不到通知就投诉。

本脚本（方案A，零业务风险）：
  每小时跑一次，统计【今天】押金>0 订单的三类占比：
    · mp_openid 为空
    · 六身份字段全空（user_id/unionid/mp_openid/openid/alipay_mp_uid/alipay_pay_uid）
    · 无 user_id
  任一超阈值就 pushplus 告警（同一告警 2 小时内不重复，避免刷屏）。
  只读，不改任何数据。

用法：
  python3 check_openid_health.py          # 正常检查
  python3 check_openid_health.py --dry    # 只打印不推送
"""
import sys
import os
import json
import logging
from datetime import datetime

logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')

DRY = ('--dry' in sys.argv)

# 阈值（百分比）—— 超过就告警
TH_NO_MP = 5.0        # mp_openid 空占比
TH_ALL_EMPTY = 3.0    # 六字段全空占比
TH_NO_UID = 10.0      # 无 user_id 占比

# 最小样本：订单太少时比例没意义，不告警
MIN_SAMPLE = 30

STATE_FILE = '/tmp/openid_health_last_alert.json'
ALERT_GAP_SEC = 2 * 3600      # 同一类告警 2 小时内不重复


def _load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(d):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        print('  保存状态失败(不影响): %s' % e)


def _should_alert(key, state):
    import time
    last = float(state.get(key) or 0)
    return (time.time() - last) > ALERT_GAP_SEC


def _mark_alert(key, state):
    import time
    state[key] = time.time()
    _save_state(state)


def main():
    from database import get_db
    db = get_db()
    cur = db.cursor()

    def q(sql, args=None):
        cur.execute(sql, args or ())
        return [dict(r) for r in cur.fetchall()]

    # [S1495] 动态探测身份列（106 老库没有 alipay_*_uid，不能硬写）
    _cols = {r['column_name'] for r in q(
        "SELECT column_name FROM information_schema.columns WHERE table_name='orders'")}
    _id_cols = ['user_id', 'unionid', 'mp_openid', 'openid']
    for _c in ('alipay_mp_uid', 'alipay_pay_uid'):
        if _c in _cols:
            _id_cols.append(_c)
    _empty_cond = ' AND '.join(["COALESCE(%s,'') = ''" % c for c in _id_cols if c != 'user_id'])
    if 'user_id' in _id_cols:
        _empty_cond = 'COALESCE(user_id,0) = 0 AND ' + _empty_cond

    # 今天（按自然日）押金>0 的订单
    r = q("""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE COALESCE(mp_openid,'') = '') AS no_mp,
          COUNT(*) FILTER (WHERE %s) AS all_empty,
          COUNT(*) FILTER (WHERE COALESCE(user_id,0) = 0) AS no_uid
        FROM orders
        WHERE store_time >= CURRENT_DATE
          AND COALESCE(deposit_amount,0) > 0
    """ % _empty_cond)[0]

    total = int(r['total'] or 0)
    no_mp = int(r['no_mp'] or 0)
    all_empty = int(r['all_empty'] or 0)
    no_uid = int(r['no_uid'] or 0)

    def pct(a):
        return (100.0 * a / total) if total else 0.0

    print('=' * 70)
    print('  订单身份健康检查  %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 70)
    print('  今天押金>0 订单: %d 笔' % total)
    print('')
    print('  %-22s %-10s %-10s %s' % ('指标', '数量', '占比', '阈值'))
    print('  ' + '-' * 56)
    print('  %-22s %-10d %-10s %s' % ('mp_openid 为空', no_mp, '%.1f%%' % pct(no_mp), '%.0f%%' % TH_NO_MP))
    print('  %-22s %-10d %-10s %s' % ('六身份字段全空', all_empty, '%.1f%%' % pct(all_empty), '%.0f%%' % TH_ALL_EMPTY))
    print('  %-22s %-10d %-10s %s' % ('无 user_id', no_uid, '%.1f%%' % pct(no_uid), '%.0f%%' % TH_NO_UID))

    # 小时分布（看是不是某时段集中）
    print('')
    print('  按小时（mp_openid 为空）：')
    for x in q("""
        SELECT to_char(store_time,'HH24') AS h,
               COUNT(*) AS t,
               COUNT(*) FILTER (WHERE COALESCE(mp_openid,'') = '') AS no_mp
        FROM orders
        WHERE store_time >= CURRENT_DATE AND COALESCE(deposit_amount,0) > 0
        GROUP BY 1 ORDER BY 1
    """):
        p = 100.0 * int(x['no_mp']) / max(int(x['t']), 1)
        flag = ' ★★' if p >= TH_NO_MP else ''
        print('     %s 点: %2s/%2s = %.0f%%%s' % (x['h'], x['no_mp'], x['t'], p, flag))

    db.close()

    # 判定
    if total < MIN_SAMPLE:
        print('')
        print('  样本不足 %d 笔，跳过告警' % MIN_SAMPLE)
        return 0

    state = _load_state()
    alerts = []

    if pct(no_mp) >= TH_NO_MP and _should_alert('no_mp', state):
        alerts.append(('no_mp',
                       '订单身份异常：mp_openid 为空 %.1f%%（%d/%d）' % (pct(no_mp), no_mp, total),
                       '今天 %d 笔押金>0 订单里有 %d 笔没有小程序 openid。\n'
                       '后果：订阅消息发不出去、行为画像失效、用户可能查不到订单。\n'
                       '可能原因：公众号停用导致 OAuth 失败 / 用户非小程序入口 / openid 前缀不匹配。\n'
                       '请检查公众号是否启用、近期是否切换过小程序。' % (total, no_mp)))

    if pct(all_empty) >= TH_ALL_EMPTY and _should_alert('all_empty', state):
        alerts.append(('all_empty',
                       '订单身份严重异常：六字段全空 %.1f%%（%d/%d）' % (pct(all_empty), all_empty, total),
                       '今天 %d 笔订单里 %d 笔【六个身份字段全空】（user_id/unionid/mp_openid/openid/alipay 全无）。\n'
                       '这些订单无法归属到任何用户，无法发通知、无法画像。\n'
                       '请立即排查下单接口与 OAuth。' % (total, all_empty)))

    if pct(no_uid) >= TH_NO_UID and _should_alert('no_uid', state):
        alerts.append(('no_uid',
                       '订单无 user_id：%.1f%%（%d/%d）' % (pct(no_uid), no_uid, total),
                       '今天 %d 笔订单里 %d 笔没关联到用户（user_id=0）。\n'
                       '这些订单在用户端可能看不到，也无法发订阅消息。' % (total, no_uid)))

    if not alerts:
        print('')
        print('  ✓ 一切正常（或 2 小时内已告警过，不重复推）')
        return 0

    print('')
    print('  ★ 需要告警 %d 条' % len(alerts))
    for key, title, body in alerts:
        print('    - %s' % title)

    if DRY:
        print('')
        print('  [--dry] 只打印，不推送')
        return 0

    # 推送
    try:
        import helpers
        ok_n = 0
        for key, title, body in alerts:
            try:
                helpers.send_pushplus('【寄存柜】' + title, body, 'txt')
                _mark_alert(key, state)
                ok_n += 1
                print('    ✓ 已推送: %s' % title)
            except Exception as e:
                print('    ✗ 推送失败: %s' % str(e)[:80])
        return 0 if ok_n == len(alerts) else 1
    except Exception as e:
        print('  ✗ 推送模块异常: %s' % str(e)[:100])
        return 1


if __name__ == '__main__':
    sys.exit(main())
