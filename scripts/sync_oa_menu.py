#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[S684-20260927] 同步公众号菜单 -> 让「存包 / 钱包 / 个人中心」三项都跳【当前使用的小程序】

本脚本只是 helpers.sync_oa_menus() 的命令行外壳（后台切换小程序时也会自动调它），
逻辑只有一份，避免两处不一致。

菜单结构（老板 2026-09-27 拍定：保持三项）
    存包     -> pages/index/index
    钱包     -> pages/wallet/wallet
    个人中心 -> pages/mine/mine
全部指向 wx_config.mp_appid()（= 当前生效小程序），所以换小程序后重跑一次即可。

用法
    python3 scripts/sync_oa_menu.py --dry-run        # 只看要做什么
    python3 scripts/sync_oa_menu.py                  # 全部公众号都配（每个号先备份原菜单）
    python3 scripts/sync_oa_menu.py --oa-id 8        # 只配某个号
    python3 scripts/sync_oa_menu.py --rollback 4     # 用备份把某号菜单还原
    python3 scripts/sync_oa_menu.py --delete 4       # 删掉某号的菜单

回滚：备份在 /home/ubuntu/oa_menu_backup/oa_menu_<id>_<时间>.json
"""
import sys
import os
import glob
import json
import argparse

sys.path.insert(0, '/home/ubuntu/smart-locker')
from helpers import sync_oa_menus, get_access_token_for       # noqa: E402
from config import DATABASE_URL                               # noqa: E402
import psycopg2                                               # noqa: E402
from psycopg2.extras import RealDictCursor                    # noqa: E402

BACKUP_DIR = '/home/ubuntu/oa_menu_backup'


def _accounts(oa_id=None):
    c = psycopg2.connect(DATABASE_URL)
    cur = c.cursor(cursor_factory=RealDictCursor)
    if oa_id:
        cur.execute("SELECT id,name,appid,secret FROM wx_accounts WHERE acct_type='oa' AND id=%s", (oa_id,))
    else:
        cur.execute("SELECT id,name,appid,secret FROM wx_accounts WHERE acct_type='oa' ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return [r for r in rows if r.get('appid') and not str(r['appid']).startswith('REPLACE')]


def _api(path, token, body=None, method='GET'):
    import urllib.request
    url = 'https://api.weixin.qq.com/cgi-bin/' + path + '?access_token=' + token
    if method == 'GET' and body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(body or {}, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--oa-id', type=int, default=None)
    ap.add_argument('--rollback', type=int, default=None, help='用最近一次备份还原该号菜单')
    ap.add_argument('--delete', type=int, default=None, help='删掉该号菜单')
    args = ap.parse_args()

    if args.rollback:
        for a in _accounts(args.rollback):
            tk = get_access_token_for(a['appid'], a['secret'] or '')
            cands = sorted(glob.glob(os.path.join(BACKUP_DIR, 'oa_menu_%s_*.json' % a['id'])))
            if not cands:
                print('oa id=%s 没有备份，无法回滚' % a['id'])
                continue
            fn = cands[-1]
            print('oa id=%s %s <- 回滚自 %s' % (a['id'], a['name'], fn))
            raw = json.load(open(fn, encoding='utf-8'))
            menu = raw.get('menu') or {}
            if raw.get('errcode') == 46003 or not menu.get('button'):
                print('  备份里本来就没有菜单 -> menu/delete')
                if not args.dry_run:
                    print('  返回:', json.dumps(_api('menu/delete', tk), ensure_ascii=False))
                continue
            body = {"button": menu.get('button')}
            if menu.get('matchrule'):
                body['matchrule'] = menu['matchrule']
            for b in body['button']:
                print('   [%s] %s' % (b.get('type'), b.get('name')))
            if not args.dry_run:
                print('  返回:', json.dumps(_api('menu/create', tk, body, method='POST'), ensure_ascii=False))
        return 0

    if args.delete:
        for a in _accounts(args.delete):
            tk = get_access_token_for(a['appid'], a['secret'] or '')
            print('oa id=%s %s -> menu/delete' % (a['id'], a['name']))
            if not args.dry_run:
                print('  返回:', json.dumps(_api('menu/delete', tk), ensure_ascii=False))
        return 0

    res, txt = sync_oa_menus(oa_id=args.oa_id, dry_run=args.dry_run)
    print()
    for r in res:
        flag = {'ok': '✅ 已配好', 'dry-run': '· dry-run',
                '没关联小程序': '❌ 该公众号没关联当前小程序（去微信后台关联后再跑）'}.get(
                    r['result'], '❌ ' + r['result'])
        print('  oa id=%-3s %-34s %s  %s' % (r['id'], r['name'], flag, r.get('detail') or ''))
    print()
    print(txt)
    print('备份目录: %s' % BACKUP_DIR)
    print('回滚: python3 scripts/sync_oa_menu.py --rollback <账号id>')
    return 0


if __name__ == '__main__':
    sys.exit(main())
