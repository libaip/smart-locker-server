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
    s = {'成功': 0, '失败': 0, '跳过': 0, '空openid': 0, '异常': 0,
         'errcode': {}, '模板失败': {}}
    for ln in lines:
        if '发送成功' in ln:
            s['成功'] += 1
        elif '发送失败' in ln:
            s['失败'] += 1
            m = re.search(r"'errcode':\s*(\d+)", ln)
            if m:
                s['errcode'][m.group(1)] = s['errcode'].get(m.group(1), 0) + 1
            t = re.search(r'template=([A-Za-z0-9_\-]+)', ln)
            if t:
                s['模板失败'][t.group(1)] = s['模板失败'].get(t.group(1), 0) + 1
        elif '跳过公众号openid' in ln:
            s['跳过'] += 1
        elif 'mp_openid为空' in ln:
            s['空openid'] += 1
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
           '今天补上新id人数': 0, '今天新建身份人数': 0, 'prefix': '', 'err': ''}
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
        cur.execute("""
            select count(*),
                   count(*) filter (where mp_openid like %s),
                   count(*) filter (where coalesce(mp_openid,'') <> '' and mp_openid not like %s),
                   count(*) filter (where coalesce(mp_openid,'') = '')
            from orders where created_at >= %s
        """, (pfx + '%', pfx + '%', since_dt))
        r = cur.fetchone()
        out['订单'], out['订单带新id'], out['订单带旧id'], out['订单无mp_openid'] = r[0], r[1], r[2], r[3]
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
    fail_rate = (float(s['失败']) / total) if total else 0.0
    body = []
    body.append('统计窗口: %s ~ %s' % (since.strftime('%m-%d %H:%M'), now.strftime('%m-%d %H:%M')))
    body.append('')
    body.append('【通知】')
    body.append('  发送成功: %d' % s['成功'])
    body.append('  发送失败: %d（失败率 %.0f%%）' % (s['失败'], fail_rate * 100))
    body.append('  失败错误码: %s' % fmt_err(s))
    body.append('  跳过(只有旧号openid): %d' % s['跳过'])
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
        body.append('  今天补上新id的人数: %d（其中新建身份 %d）'
                    % (db['今天补上新id人数'], db['今天新建身份人数']))
        body.append('  （新id前缀 = %s）' % db['prefix'])

    # ---- 异常判定（只对 hourly 告警）----
    alerts = []
    if s['跳过'] >= TH_SKIP:
        alerts.append('跳过数偏高：%d 条（阈值 %d）—— 用户手里是旧号 openid，通知发不出去'
                      % (s['跳过'], TH_SKIP))
    if total >= TH_MIN_SAMPLE and fail_rate >= TH_FAIL_RATE:
        alerts.append('失败率偏高：%.0f%%（%d 失败/%d 总共）—— %s'
                      % (fail_rate * 100, s['失败'], total, fmt_err(s)))
    if s['成功'] == 0 and total + s['跳过'] + s['空openid'] >= TH_MIN_SAMPLE:
        alerts.append('一条都没发成功（总共 %d 次尝试）—— 通知通道可能挂了'
                      % (total + s['跳过'] + s['空openid']))
    if (not db['err']) and db['订单'] >= TH_SILENT_ORDERS and (total + s['跳过'] + s['空openid']) == 0:
        alerts.append('有 %d 笔订单，但一条通知日志都没有 —— 通知可能根本没被调用' % db['订单'])
    if s['异常'] > 0:
        alerts.append('发送代码抛异常 %d 次（看日志 [subscribe_msg] 异常）' % s['异常'])

    if a.mode == 'hourly':
        if alerts:
            body.append('')
            body.append('【⚠️ 发现异常】')
            for x in alerts:
                body.append('  · %s' % x)
            body.append('')
            body.append('（这条是自动告警，同一问题 6 小时内只提醒一次）')
            state = load_state()
            key = '|'.join(sorted(a.split('：')[0] for a in alerts))
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
            log_line('正常 成功%d 失败%d 跳过%d' % (s['成功'], s['失败'], s['跳过']))
        return 0

    # daily / print：直接输出（daily 默认推送）
    txt = '\n'.join(body)
    print(txt)
    log_line('日报 成功%d 失败%d 跳过%d 订单%d' % (s['成功'], s['失败'], s['跳过'], db['订单']))
    if a.mode == 'daily' and not a.no_push:
        push(title, txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
