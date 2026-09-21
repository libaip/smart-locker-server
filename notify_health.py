# -*- coding: utf-8 -*-
"""
通知体检（T-1789312040 之后新增）

干两件事：
  1) 每小时看一次异常：通知的"成功/失败/跳过"三个数有没有突然不对，不对就推给你
     （没异常就静默，不打扰）
  2) 每天 22:05 发一份日报：三个数 + 失败按错误码分 + 当天有多少人补上了新小程序 id
     （"尾巴还有多长"）

为什么要它：微信订阅通知是"用户不授权就发不出去"的，出了问题用户不吭声、我们也不知道，
最后只能等投诉。有了它就能从报表先看到。

用法:
  python3 notify_health.py --mode hourly     # 每小时（cron），只报异常
  python3 notify_health.py --mode daily      # 每天 22:05（cron），发完整日报
  python3 notify_health.py --mode print      # 只打印不推送（人工排查）
  python3 notify_health.py --mode print --hours 6
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

APP = '/home/ubuntu/smart-locker'
sys.path.insert(0, APP)
STATE_FILE = os.path.join(APP, 'logs', 'notify_health_state.json')
UNIT = 'smart-locker'

# 阈值（超过就告警）
TH_SKIP = 15            # 一小时内"跳过"超过这个数
TH_FAIL_RATE = 0.70     # 失败率超过这个（且样本>=8）——平时约 0.4
TH_MIN_SAMPLE = 8
TH_SILENT_ORDERS = 5    # 有订单却一条通知日志都没有 -> 可疑
THROTTLE_SEC = 6 * 3600  # 同一个告警 6 小时内只推一次
# [S545b] 新增告警 switch_off 的阈值（原有阈值一个都没动）
TH_SWITCH_OFF = 1       # 订阅总开关/手动应急开关 off：出现 1 条就报（这是"有人主动把通知关了"，一条就该知道）
TH_OA_SILENT = 5        # 入口模式=无小程序场景：跳过 >=5 条且整小时成功+失败都是 0 才报


def sh(cmd, timeout=180):
    """只用来跑固定的命令（不接外部输入）"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or '').strip()
    except Exception as e:
        return '__ERR__%s' % e


def journal_lines(since_str):
    cmd = ("sudo -n journalctl -u %s --since '%s' --no-pager 2>/dev/null "
           "| grep '\\[subscribe_msg\\]' | cut -c1-400" % (UNIT, since_str))
    out = sh(cmd)
    if out.startswith('__ERR__'):
        return None, out[7:]
    return [x for x in out.split('\n') if x.strip()], ''


def parse(lines):
    s = {'成功': 0, '失败': 0, '跳过': 0, '跳过-设计': 0, '跳过-设计-oa': 0,
         '跳过-设计-开关': 0, '空openid': 0, '异常': 0,
         'errcode': {}, '模板失败': {}}
    for ln in lines:
        # [S545] 先判成功：'新模板失败->旧模板发送成功' 里带"失败"两个字，必须先归成功
        if ('发送成功' in ln) or ('] OK ' in ln) or ('新模板失败->旧模板发送成功' in ln):
            s['成功'] += 1
        # [S545] 失败：老措辞 '发送失败' + S523/S533 新措辞 '] 失败 账号=... errcode=...'
        elif ('发送失败' in ln) or ('] 失败 账号=' in ln):
            s['失败'] += 1
            m = re.search(r"'errcode':\s*(\d+)", ln) or re.search(r'errcode=(\d+)', ln)
            if m:
                s['errcode'][m.group(1)] = s['errcode'].get(m.group(1), 0) + 1
            t = re.search(r'template=([A-Za-z0-9_\-]+)', ln)
            if t:
                s['模板失败'][t.group(1)] = s['模板失败'].get(t.group(1), 0) + 1
        # [S545] 空 openid 必须排在"跳过发送"前面 —— 那句话里两个词都有
        elif 'mp_openid为空' in ln:
            s['空openid'] += 1
        # [S545] 问题型跳过（旧号 openid，真会发不出去）—— 计入 TH_SKIP
        elif '跳过公众号openid' in ln:
            s['跳过'] += 1
        # [S545] 设计性跳过（入口无小程序场景 / 订阅总开关 off / 手动应急开关 off）：
        #        这些是按设计或按开关跳过，不是"旧号 openid 发不出去"，单列一桶，
        #        不参与 TH_SKIP 告警（否则 9/21 那种忙碌小时会被顶爆成新的误报）
        elif ('跳过小程序订阅消息' in ln) or ('跳过 openid=' in ln) or ('应急开关=off' in ln):
            s['跳过-设计'] += 1
            # [S545b] 再拆两类：自动联动(入口无小程序场景) / 人为开关(总开关、应急开关 off)
            if '跳过小程序订阅消息' in ln:
                s['跳过-设计-oa'] += 1
            else:
                s['跳过-设计-开关'] += 1
        # [S545] 其余"跳过发送"（兜底，正常情况下已被上面的 mp_openid为空 吃掉）
        elif '跳过发送' in ln:
            s['跳过'] += 1
        elif '异常' in ln:
            s['异常'] += 1
    return s


ERRCODE_MEANING = {
    '43101': '用户没订阅/一次性额度用完（正常现象，靠公众号通道解决）',
    '40003': 'openid 无效（openid 不对，要查）',
    '40037': '模板编号无效（模板填错了）',
    '47003': '模板参数不对（字段值超长或不合法）',
    '41004': '缺密钥（密钥没配）',
    '40013': 'appid 无效（编号填错了）',
    '45009': '接口调用超限',
}

TEMPLATE_NAME = {
    'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk': '寄存成功',
    'PtRJgPDDeP_sXcpMpn_ttqJKiY-C65fe1SL7iNOEQGA': '押金退还',
    'lJpnAUiEKj8FutThHqXZzehBUsXP0DJC6dCtE6x2T_c': '退款成功',
}


def db_stats(since_dt):
    """数据库侧：订单量、带新/旧 id 的订单、今天补上新 id 的人数"""
    out = {'订单': 0, '订单带新id': 0, '订单带旧id': 0, '订单无mp_openid': 0,
           '今天补上新id人数': 0, '今天新建身份人数': 0, 'prefix': '', 'err': '',
           '支付宝订单': 0, '订单口径': ''}
    try:
        import psycopg2
        sys.path.insert(0, APP)
        import config
        import wx_config as C
        dsn = getattr(config, 'DATABASE_URL', '') or ''
        if not dsn:
            out['err'] = 'config.DATABASE_URL 没配'
            return out
        C.bind(lambda: psycopg2.connect(dsn))
        pfx = (C.mp_openid_prefix() or 'ooTcRx')
        out['prefix'] = pfx
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        # [S545] "订单"只统计【本该发微信订阅通知】的：支付宝渠道单不发微信订阅通知，
        #        算进来天然制造误报。判定口径与 helpers.order_notify_platform() 一致：
        #        渠道 channel_type='alipay'，或订单自带支付宝身份（alipay_mp_uid/alipay_pay_uid）。
        #        payment_channel_id 为 NULL 的老单按"非支付宝"处理（coalesce 兜住），不丢单。
        _ali = ("(coalesce(pc.channel_type, '') = 'alipay'"
                " or coalesce(o.alipay_mp_uid, '') <> ''"
                " or coalesce(o.alipay_pay_uid, '') <> '')")
        _sql_orders = ("""
            select count(*) filter (where not """ + _ali + """),
                   count(*) filter (where not """ + _ali + """ and o.mp_openid like %s),
                   count(*) filter (where not """ + _ali + """ and coalesce(o.mp_openid,'') <> '' and o.mp_openid not like %s),
                   count(*) filter (where not """ + _ali + """ and coalesce(o.mp_openid,'') = ''),
                   count(*) filter (where """ + _ali + """)
            from orders o left join payment_channels pc on pc.id = o.payment_channel_id
            where o.created_at >= %s
        """)
        try:
            cur.execute(_sql_orders, (pfx + '%', pfx + '%', since_dt))
            r = cur.fetchone()
            out['订单'], out['订单带新id'], out['订单带旧id'], out['订单无mp_openid'], out['支付宝订单'] = \
                r[0], r[1], r[2], r[3], r[4]
        except Exception as _e545:
            # 万一 orders / payment_channels 的列名以后变了，退回老口径，别让整个体检瞎掉
            conn.rollback()
            out['订单口径'] = '回退(未排除支付宝): %s' % str(_e545)[:80]
            cur.execute("""
                select count(*),
                       count(*) filter (where mp_openid like %s),
                       count(*) filter (where coalesce(mp_openid,'') <> '' and mp_openid not like %s),
                       count(*) filter (where coalesce(mp_openid,'') = '')
                from orders where created_at >= %s
            """, (pfx + '%', pfx + '%', since_dt))
            r = cur.fetchone()
            out['订单'], out['订单带新id'], out['订单带旧id'], out['订单无mp_openid'] = \
                r[0], r[1], r[2], r[3]
        cur.execute("select count(distinct phone) from phone_openids "
                    "where mp_openid like %s and updated_at >= %s", (pfx + '%', since_dt))
        out['今天补上新id人数'] = cur.fetchone()[0]
        cur.execute("select count(distinct phone) from phone_openids "
                    "where mp_openid like %s and created_at >= %s", (pfx + '%', since_dt))
        out['今天新建身份人数'] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        out['err'] = str(e)[:200]
    return out


def push(title, content):
    try:
        import requests
        import config
        tok = getattr(config, 'PUSHPLUS_TOKEN', '') or ''
        if not tok:
            print('[push] 没配 PUSHPLUS_TOKEN，跳过推送')
            return False
        r = requests.post('http://www.pushplus.plus/send',
                          json={'token': tok, 'title': title, 'content': content, 'template': 'txt'},
                          timeout=10)
        d = r.json() if r.text else {}
        ok = (d.get('code') == 200)
        print('[push] %s %s' % ('成功' if ok else '失败', d.get('msg') or r.status_code))
        return ok
    except Exception as e:
        print('[push] 异常: %s' % e)
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        print('[state] 保存失败: %s' % e)


def log_line(s):
    try:
        os.makedirs(os.path.join(APP, 'logs'), exist_ok=True)
        with open(os.path.join(APP, 'logs', 'notify_health.log'), 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (datetime.now().strftime('%F %T'), s))
    except Exception:
        pass


def fmt_err(s):
    if not s['errcode']:
        return '（无）'
    items = sorted(s['errcode'].items(), key=lambda x: -x[1])
    return '、'.join('%s×%d（%s）' % (k, v, ERRCODE_MEANING.get(k, '未知')) for k, v in items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['hourly', 'daily', 'print'], default='print')
    ap.add_argument('--hours', type=float, default=None)
    ap.add_argument('--no-push', action='store_true')
    a = ap.parse_args()

    now = datetime.now()
    if a.mode == 'daily':
        since = datetime(now.year, now.month, now.day)      # 今天 0 点
        title = '【寄存柜】通知日报 %s' % now.strftime('%m-%d')
    elif a.mode == 'hourly':
        since = now - timedelta(hours=1)
        title = '【寄存柜】通知异常提醒 %s' % now.strftime('%m-%d %H:%M')
    else:
        since = now - timedelta(hours=(a.hours if a.hours else 6))
        title = '【寄存柜】通知体检 %s' % now.strftime('%m-%d %H:%M')

    since_str = since.strftime('%Y-%m-%d %H:%M:%S')
    lines, jerr = journal_lines(since_str)
    # [S545] journal 里的原始行数：用来把"真的没日志"和"有日志但认不出来"分开
    raw_lines = len(lines) if lines else 0
    if lines is None:
        msg = '读日志失败：%s' % jerr
        print(msg)
        log_line(msg)
        if a.mode != 'print' and not a.no_push:
            push('【寄存柜】通知体检脚本出错', msg)
        return 1
    s = parse(lines)
    db = db_stats(since)

    total = s['成功'] + s['失败']
    # [S545] "认出来的行数"：只要有任意一条订阅日志被归进任何一类，就不算"监控瞎了"
    recognized = (s['成功'] + s['失败'] + s['跳过'] + s['跳过-设计']
                  + s['空openid'] + s['异常'])
    fail_rate = (float(s['失败']) / total) if total else 0.0
    body = []
    body.append('统计窗口: %s ~ %s' % (since.strftime('%m-%d %H:%M'), now.strftime('%m-%d %H:%M')))
    body.append('')
    body.append('【通知】')
    body.append('  发送成功: %d' % s['成功'])
    body.append('  发送失败: %d（失败率 %.0f%%）' % (s['失败'], fail_rate * 100))
    body.append('  失败错误码: %s' % fmt_err(s))
    body.append('  跳过(只有旧号openid): %d' % s['跳过'])
    body.append('  跳过(设计性：入口无小程序场景 %d / 总开关或应急off %d): 合计 %d'
                % (s['跳过-设计-oa'], s['跳过-设计-开关'], s['跳过-设计']))
    body.append('  跳过(没有openid): %d' % s['空openid'])
    body.append('  发送异常(代码报错): %d' % s['异常'])
    if s['模板失败']:
        tf = '、'.join('%s×%d' % (TEMPLATE_NAME.get(k, k[:8]), v) for k, v in
                       sorted(s['模板失败'].items(), key=lambda x: -x[1]))
        body.append('  失败按通知类型: %s' % tf)
    body.append('')
    body.append('【订单/身份】')
    if db['err']:
        body.append('  数据库读取失败: %s' % db['err'])
    else:
        body.append('  订单: %d（带新id %d / 带旧id %d / 无小程序id %d）'
                    % (db['订单'], db['订单带新id'], db['订单带旧id'], db['订单无mp_openid']))
        body.append('  其中支付宝渠道单 %d 笔（不发微信订阅通知，已从上面的订单数排除）'
                    % db.get('支付宝订单', 0))
        if db.get('订单口径'):
            body.append('  订单口径回退告警: %s' % db['订单口径'])
        body.append('  今天补上新id的人数: %d（其中新建身份 %d）'
                    % (db['今天补上新id人数'], db['今天新建身份人数']))
        body.append('  （新id前缀 = %s）' % db['prefix'])

    # ---- 异常判定（只对 hourly 告警）----
    # [S545b] alerts 每项 = (固定的告警类型 key, 给人看的文案)
    #         固定 key 是为了让 6 小时节流真的生效（以前 key 里带着当时的订单数，每小时都是新 key）
    alerts = []
    if s['跳过'] >= TH_SKIP:
        alerts.append(('skip_high',
                       '跳过数偏高：%d 条（阈值 %d）—— 用户手里是旧号 openid，通知发不出去'
                       % (s['跳过'], TH_SKIP)))
    if total >= TH_MIN_SAMPLE and fail_rate >= TH_FAIL_RATE:
        alerts.append(('fail_rate',
                       '失败率偏高：%.0f%%（%d 失败/%d 总共）—— %s'
                       % (fail_rate * 100, s['失败'], total, fmt_err(s))))
    if s['成功'] == 0 and total + s['跳过'] + s['空openid'] >= TH_MIN_SAMPLE:
        alerts.append(('none_success',
                       '一条都没发成功（总共 %d 次尝试）—— 通知通道可能挂了'
                       % (total + s['跳过'] + s['空openid'])))
    # [S545] 把"真没日志"和"日志认不出"分开：以后日志口径再变，也不会误报成"通知没被调用"
    if (not db['err']) and db['订单'] >= TH_SILENT_ORDERS and raw_lines == 0:
        alerts.append(('silent_orders',
                       '有 %d 笔订单，但一条通知日志都没有 —— 通知可能根本没被调用' % db['订单']))
    if (not db['err']) and db['订单'] >= TH_SILENT_ORDERS and raw_lines > 0 and recognized == 0:
        alerts.append(('parse_blind',
                       '有 %d 笔订单、journal 里也有 %d 条 subscribe_msg 日志，但按当前口径一条都没认出来'
                       ' —— 可能是日志措辞改了，需要更新 notify_health.py 的 parse()'
                       % (db['订单'], raw_lines)))
    if s['异常'] > 0:
        alerts.append(('exception',
                       '发送代码抛异常 %d 次（看日志 [subscribe_msg] 异常）' % s['异常']))
    # [S545b] 新增独立告警：订阅开关被人为关了 / 入口模式导致整小时一条都没发出去
    #         独立 key = switch_off，绝不和"跳过数偏高"(skip_high) 共用节流
    if s['跳过-设计-开关'] >= TH_SWITCH_OFF:
        alerts.append(('switch_off',
                       '订阅消息开关处于关闭状态（总开关/手动应急开关 off，共 %d 条），用户收不到通知'
                       ' —— 请确认是否有意关闭' % s['跳过-设计-开关']))
    elif s['跳过-设计-oa'] >= TH_OA_SILENT and s['成功'] == 0 and s['失败'] == 0:
        alerts.append(('switch_off',
                       '入口模式=无小程序场景连续跳过 %d 条，本小时小程序订阅通知一条都没发出去'
                       ' —— 请确认入口模式是否有意' % s['跳过-设计-oa']))

    if a.mode == 'hourly':
        if alerts:
            body.append('')
            body.append('【⚠️ 发现异常】')
            for _k545, x in alerts:
                body.append('  · %s' % x)
            body.append('')
            body.append('（这条是自动告警，同一问题 6 小时内只提醒一次）')
            state = load_state()
            # [S545b] 节流 key = 固定的告警类型（silent_orders / switch_off / ...），不带当时的数字；
            #         以前 key 里带订单数（"有 194 笔订单…" 和 "有 585 笔订单…" 算两个 key），节流形同虚设
            key = '|'.join(sorted(_k for _k, _x in alerts))
            last = state.get(key, 0)
            import time as _t
            if _t.time() - last < THROTTLE_SEC:
                print('异常存在，但 6 小时内已提醒过，本次静默')
                log_line('异常(已节流): %s' % key)
                print('\n'.join(body))
                return 0
            state[key] = _t.time()
            save_state(state)
            txt = '\n'.join(body)
            print(txt)
            log_line('告警: %s' % key)
            if not a.no_push:
                push(title, txt)
        else:
            print('一小时窗口内正常（成功 %d / 失败 %d / 跳过 %d）'
                  % (s['成功'], s['失败'], s['跳过']))
            log_line('正常 成功%d 失败%d 跳过%d 设计跳过%d 订单%d'
                     % (s['成功'], s['失败'], s['跳过'], s['跳过-设计'], db['订单']))
        return 0

    # daily / print：直接输出（daily 默认推送）
    txt = '\n'.join(body)
    print(txt)
    log_line('日报 成功%d 失败%d 跳过%d 设计跳过%d 订单%d 支付宝单%d'
             % (s['成功'], s['失败'], s['跳过'], s['跳过-设计'], db['订单'], db.get('支付宝订单', 0)))
    if a.mode == 'daily' and not a.no_push:
        push(title, txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
