#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S1543] 小程序订阅消息配置自检脚本

背景（2026-10-26）：
  不归物（wx73ed39ee700bc606）只填了 template_id，没配 field_map，
  导致发出的字段名和微信模板要求不一致 -> 全部 47003。
  而微信对 47003【照样扣订阅额度】，240 个用户授权了却一条通知都没收到。
  为防再烧额度，上线前必须先自检。

用法：
  python3 check_mp_subscribe.py                      # 检查全部启用的小程序账号
  python3 check_mp_subscribe.py --appid wx73ed...    # 只查一个
  python3 check_mp_subscribe.py --all                # 同不带参数，但含未启用的
  python3 check_mp_subscribe.py --send --appid wx73ed...   # ★ 额外真发一条验证（会消耗额度）

检查项：
  1) wx_accounts 有记录 / secret 有 / is_active
  2) access_token 能否拿到
  3) 微信后台模板 vs 数据库 template_id 是否一致
  4) field_map 是否为空（空 = 高危，很可能 47003）
  5) 字段映射 dry-run（把代码字段跑一遍 remap，看生成什么）
  6) 最近 24h 发送成功率 / 47003 数量
  7) --send 时：真发一条，看返回码

退出码：0=全部通过；1=有问题
"""
import sys
import os
import json
import time
import urllib.request
import logging

logging.disable(logging.CRITICAL)
sys.path.insert(0, '/home/ubuntu/smart-locker')

from database import get_db
import wx_config as wc

wc.bind(lambda: get_db())


def q(sql, args=None):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, args or ())
    rows = [dict(r) for r in cur.fetchall()]
    db.close()
    return rows


def http_json(url, post_body=None):
    try:
        if post_body is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(post_body, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'errcode': -1, 'errmsg': str(e)[:120]}


def get_token(appid, secret):
    d = http_json('https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential'
                  '&appid=%s&secret=%s' % (appid, secret))
    return d.get('access_token'), d


def check_account(acc, do_send=False, verbose=True):
    """检查一个账号，返回 (problems 列表, 详情 dict)"""
    AID = acc['id']
    APPID = acc.get('appid') or ''
    NAME = acc.get('name') or ''
    problems = []
    info = {'id': AID, 'name': NAME, 'appid': APPID}

    def p(msg):
        problems.append(msg)

    if verbose:
        print()
        print('=' * 100)
        print('  acct %s  %s  (%s)' % (AID, NAME, APPID))
        print('=' * 100)

    # 1) 基础
    if not acc.get('secret'):
        p('secret 为空')
        if verbose:
            print('   ✗ secret 为空')
    else:
        if verbose:
            print('   ✓ secret 已配置（%d 位）' % len(acc['secret']))

    if not APPID:
        p('appid 为空')
        return problems, info

    if verbose:
        print('   %s is_active=%s  openid_prefix=%s'
              % ('✓' if acc.get('is_active') else '·',
                 acc.get('is_active'), acc.get('openid_prefix') or '-'))

    # 2) token
    tk, td = get_token(APPID, acc.get('secret') or '')
    if not tk:
        p('access_token 拿不到：%s' % td.get('errmsg'))
        if verbose:
            print('   ✗ access_token 拿不到: errcode=%s %s'
                  % (td.get('errcode'), td.get('errmsg')))
        return problems, info
    if verbose:
        print('   ✓ access_token 拿到了（%d 位）' % len(tk))

    # 3) 微信后台模板
    wd = http_json('https://api.weixin.qq.com/wxaapi/newtmpl/gettemplate?access_token=' + tk)
    wx_items = {}
    if wd.get('errcode') == 0:
        for it in (wd.get('data') or []):
            tid = it.get('priTmplId') or ''
            if tid:
                wx_items[tid] = it.get('title') or ''
    if verbose:
        print('   微信后台模板 %d 个：' % len(wx_items))
        for tid, t in wx_items.items():
            print('      %s  %s' % (tid, str(t)[:34]))

    # 4) 数据库模板
    tpls = q('SELECT * FROM wx_templates WHERE account_id=%s ORDER BY biz', (AID,))
    if not tpls:
        p('数据库里没有该账号的模板记录')
        if verbose:
            print('   ✗ 数据库里没有模板记录')
        return problems, info

    if verbose:
        print()
        print('   %-20s %-10s %-10s %s' % ('biz', 'ID匹配', 'field_map', '说明'))
        print('   ' + '-' * 88)

    for t in tpls:
        biz = t.get('biz') or ''
        tid = str(t.get('template_id') or '')
        fm = str(t.get('field_map') or '').strip()
        in_wx = tid in wx_items
        has_map = bool(fm) and fm != '{}'

        # [S1543b] field_map 为空不等于有问题：代码里有硬编码映射兜底
        #   _SUBSCRIBE_FIELD_MAP.get((account_id, biz))
        #   只有"两边都没有"才是真高危（不归物就是这么烧掉 240 个用户额度的）
        hard_map = None
        try:
            import helpers as _h
            hard_map = _h._SUBSCRIBE_FIELD_MAP.get((int(AID), biz or ''))
        except Exception:
            hard_map = None
        has_hard = bool(hard_map)

        note = []
        if not in_wx:
            p('%s 的 template_id 在微信后台找不到' % biz)
            note.append('★ID不匹配')
        if not has_map and not has_hard:
            p('%s 的 field_map 为空且无硬编码映射（高危：必然 47003）' % biz)
            note.append('★无映射(高危)')
        elif not has_map and has_hard:
            note.append('硬编码兜底')
        if t.get('is_active') != 1:
            note.append('未启用')

        if verbose:
            print('   %-20s %-10s %-12s %s' % (
                biz, '✓' if in_wx else '✗',
                ('有' if has_map else ('硬编码' if has_hard else '★空')),
                '、'.join(note) if note else 'OK'))

        # 5) dry-run 字段映射
        if has_map and verbose:
            sample = {}
            if biz == 'subscribe_general':
                sample = {'amount1': {'value': '¥20.00'},
                          'time2': {'value': '2026-01-01 00:00'},
                          'thing3': {'value': '温馨提示测试'},
                          'thing4': {'value': '变动原因测试'}}
            elif biz == 'subscribe_refund':
                sample = {'amount2': {'value': '¥20.00'},
                          'time5': {'value': '2026-01-01 00:00'},
                          'thing3': {'value': '备注测试'},
                          'thing4': {'value': '退款方式测试'}}
            if sample:
                try:
                    import helpers
                    out = helpers._remap_subscribe_data(AID, biz, sample)
                    print('      映射后字段: %s' % sorted(out.keys()))
                except Exception as e:
                    print('      ★ 映射测试异常: %s' % str(e)[:80])

    # 6) 最近 24h 发送情况
    r = q("""SELECT COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status='ok') AS ok,
                    COUNT(*) FILTER (WHERE err_code=47003) AS e47003,
                    COUNT(*) FILTER (WHERE err_code=43101) AS e43101
             FROM user_subscribe_status
             WHERE acct_id=%s AND created_at >= NOW() - INTERVAL '24 hours'""", (AID,))
    if r:
        r = r[0]
        tot = int(r['total'] or 0)
        okn = int(r['ok'] or 0)
        e47 = int(r['e47003'] or 0)
        if verbose:
            print()
            print('   最近 24h：总=%s 成功=%s 47003=%s 43101=%s'
                  % (tot, okn, e47, r['e43101']))
        if tot >= 5 and okn == 0:
            p('最近 24h 有 %s 次发送但成功 0 次' % tot)
            if verbose:
                print('   ★ 全部失败，配置有问题')
        if e47 > 0:
            # [S1543b] 提示而非硬失败：可能是修复前的历史数据，也可能仍不对
            if verbose:
                print('   · 最近 24h 有 47003×%s（可能是修复前历史；若持续增长说明仍未修好）' % e47)

        info['last24'] = dict(total=tot, ok=okn, e47003=e47)

    # 7) 可选：真发一条
    if do_send:
        if verbose:
            print()
            print('   ── 实测发送（会消耗 1 次订阅额度）──')
        real = q("""SELECT mp_openid, user_phone, id FROM orders
                    WHERE mp_openid LIKE %s ORDER BY store_time DESC LIMIT 1""",
                 ((acc.get('openid_prefix') or 'xxx') + '%',))
        if not real:
            if verbose:
                print('      · 没有该小程序的用户，跳过实测')
            info['send_test'] = 'no_user'
        else:
            o = real[0]
            import datetime
            import helpers
            for biz in ('subscribe_general', 'subscribe_refund'):
                trow = [x for x in tpls if x.get('biz') == biz]
                if not trow:
                    continue
                tid = trow[0].get('template_id')
                if biz == 'subscribe_general':
                    data = {'amount1': {'value': '¥0.01'},
                            'time2': {'value': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')},
                            'thing3': {'value': '配置自检测试，请忽略'},
                            'thing4': {'value': '自检'}}
                else:
                    data = {'amount2': {'value': '¥0.01'},
                            'time5': {'value': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')},
                            'thing3': {'value': '配置自检测试，请忽略'},
                            'thing4': {'value': '自检'}}
                try:
                    ok = helpers._send_subscribe_for_account(
                        account_id=AID, openid=o['mp_openid'], template_id=tid,
                        data=data, page='pages/mine/mine', phone=o['user_phone'])
                    if verbose:
                        print('      %-20s 结果=%s' % (biz, '✓ 成功' if ok else '✗ 失败'))
                    if not ok:
                        p('%s 实测发送失败' % biz)
                except Exception as e:
                    p('%s 实测异常: %s' % (biz, str(e)[:80]))
                    if verbose:
                        print('      %-20s ★ 异常: %s' % (biz, str(e)[:80]))

    return problems, info


def main():
    args = sys.argv[1:]
    do_send = '--send' in args
    only_appid = ''
    if '--appid' in args:
        i = args.index('--appid')
        if i + 1 < len(args):
            only_appid = args[i + 1]
    show_all = ('--all' in args) or (not only_appid)

    print('=' * 100)
    print('  小程序订阅消息配置自检  %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    print('  模式: %s' % ('★ 真发测试（会消耗订阅额度）' if do_send else '只检查（不发送）'))
    print('=' * 100)

    sql = "SELECT * FROM wx_accounts WHERE acct_type='mp'"
    params = ()
    if only_appid:
        sql += " AND appid=%s"
        params = (only_appid,)
    elif not show_all:
        sql += " AND is_active=1"
    sql += " ORDER BY is_active DESC, id"

    accs = q(sql, params)
    if only_appid:
        accs = [a for a in accs if a.get('appid') == only_appid]
    if not accs:
        print()
        print('  ★ 没有匹配的小程序账号')
        return 1

    all_problems = []
    for acc in accs:
        probs, info = check_account(acc, do_send=do_send, verbose=True)
        if probs:
            all_problems.append((acc, probs))

    print()
    print('=' * 100)
    print('  汇总')
    print('=' * 100)
    print('   检查账号: %d 个' % len(accs))
    if not all_problems:
        print('   ★★★ 全部通过，配置正常')
        return 0
    print('   有问题的账号: %d 个' % len(all_problems))
    for acc, probs in all_problems:
        print()
        print('   acct %s %s (%s)' % (acc['id'], acc.get('name'), acc.get('appid')))
        for x in probs:
            print('      ★ %s' % x)
    print()
    print('   修复建议：')
    print('      1) field_map 为空时，按微信后台模板的真实字段配置：')
    print('         general: {"amount1":"amount1","time2":"time2","thing4":"thing5","thing3":"thing4"}')
    print('         refund : {"time5":"time6"}')
    print('      2) 配完用 --send 实测一条，确认 errcode=0 再上线')
    print('      3) 切勿在未验证前大量发送 —— 47003 也会扣订阅额度')
    return 1


if __name__ == '__main__':
    sys.exit(main())
