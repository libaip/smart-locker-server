# -*- coding: utf-8 -*-
"""
[T-1789308447→新] 第二步：openid 前缀改成"跟着当前生效账号走"

背景：代码里 21 处写死了 openid 前缀（ooTcRx=当前小程序 / oLhbm2=公众号），
用来判断"某个 openid 是不是这个号下面的"。换号时这些判断全不认新号 -> 通知静默不发。
WX_PAY_* 一样，这次把前缀挪进配置中心（wx_accounts.openid_prefix）。

改法（机械、可核对）：
  1) wx_config.py   加字段 openid_prefix + 取值函数 mp_openid_prefix()/oa_openid_prefix()
                    （读不到就回落 DEFAULTS = 现在写死的 ooTcRx / oLhbm2，行为不变）
  2) 4 个业务文件把 startswith('ooTcRx') 改成 startswith(mp_openid_prefix())
                    startswith('oLhbm2') 改成 startswith(oa_openid_prefix())
  3) 6 处 SQL 的 LIKE 'ooTcRx%%' 改成绑定参数 LIKE %s
  4) 建表语句补列 + init_db 里补一句幂等 ALTER（老库自动补列）

用法: python3 patch_prefix.py          # 干跑
      python3 patch_prefix.py --real   # 真改（备份 + 语法检查 + 计数校验 + 全有或全无）
"""
import os
import sys
import time
import shutil
import difflib
import py_compile

APP = '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = os.path.join(APP, 'backups', 'prefix_' + TS)
WXC = APP + '/wx_config.py'
WXA = APP + '/wx_config_api.py'
HLP = APP + '/helpers.py'
PAY = APP + '/routes/payment.py'
ADM = APP + '/routes/admin_v2.py'
USR = APP + '/routes/user.py'

IMP_LINE = ('from wx_config import mp_openid_prefix, oa_openid_prefix   '
            '# [CFG-STEP2D] openid 前缀改成跟着当前生效账号走（缺省仍是 ooTcRx / oLhbm2）\n')

ACCESSORS = '''def openid_prefix(acct_type='mp'):
    """[CFG-STEP2D] 当前生效账号的 openid 前缀。

    干嘛用的：微信的 openid 是"跟着某一个号"的（同一个人在小程序A和小程序B里的
    openid 完全不同），代码要靠前缀判断"这个 openid 是不是当前这个号下面的"。
    以前前缀写死在代码里 -> 换号就得改代码；现在挪到 wx_accounts.openid_prefix。

    读不到（库没连上/字段空/老库没这列）就回落 DEFAULTS，也就是**现在写死的值**，
    保证行为与改造前完全一致。
    """
    row = get_effective_account(acct_type)
    p = str((row or {}).get('openid_prefix') or '').strip()
    if p:
        return p
    return str((DEFAULTS.get(acct_type) or {}).get('openid_prefix') or '')


def mp_openid_prefix():
    """当前小程序的 openid 前缀（缺省 ooTcRx）"""
    return openid_prefix('mp')


def oa_openid_prefix():
    """当前公众号的 openid 前缀（缺省 oLhbm2）"""
    return openid_prefix('oa')


def mp_appid():
    return resolve('mp').get('appid') or ''
'''

# (文件, 说明, 旧文本, 新文本, 期望出现次数)
E = []
E.append((WXC, 'DEFAULTS 小程序前缀', "        'appid': 'wxcabd4cbdb3096c4b',\n",
          "        'appid': 'wxcabd4cbdb3096c4b',\n        'openid_prefix': 'ooTcRx',\n", 1))
E.append((WXC, 'DEFAULTS 公众号前缀', "        'appid': 'wxd85204d0ec930d46',\n",
          "        'appid': 'wxd85204d0ec930d46',\n        'openid_prefix': 'oLhbm2',\n", 1))
E.append((WXC, '加取值函数', "def mp_appid():\n    return resolve('mp').get('appid') or ''\n", ACCESSORS, 1))
E.append((WXC, '_ACCOUNT_COLS 补字段',
          "    'acct_type', 'name', 'appid', 'secret', 'token', 'aes_key', 'subject',\n",
          "    'acct_type', 'name', 'appid', 'secret', 'token', 'aes_key', 'subject',\n    'openid_prefix',\n", 1))
E.append((WXC, 'create_account 写入字段',
          "        'subject': kw.get('subject', ''), 'mch_relation': kw.get('mch_relation', 'none'),\n",
          "        'subject': kw.get('subject', ''), 'mch_relation': kw.get('mch_relation', 'none'),\n"
          "        'openid_prefix': kw.get('openid_prefix', ''),\n", 1))
E.append((WXC, 'update_account 允许改',
          "'mch_relation', 'priority', 'note'}",
          "'mch_relation', 'priority', 'note', 'openid_prefix'}", 1))
E.append((WXC, '建表语句补列', "            subject VARCHAR(128) DEFAULT '',\n",
          "            subject VARCHAR(128) DEFAULT '',\n            openid_prefix VARCHAR(16) DEFAULT '',\n", 1))
E.append((WXC, 'init_db 老库补列',
          "                continue\n    clear_cache()\n    return True\n",
          "                continue\n"
          "    # [CFG-STEP2D] 老库补列（幂等）：openid_prefix。新库由建表语句直接带上。\n"
          "    with _conn() as (conn, kind):\n"
          "        try:\n"
          "            _exec(conn, kind, \"ALTER TABLE wx_accounts ADD COLUMN openid_prefix VARCHAR(16) DEFAULT ''\")\n"
          "        except Exception:\n"
          "            pass          # 已经有了 / 老版本不支持 IF NOT EXISTS，都当\"已存在\"\n"
          "    clear_cache()\n"
          "    return True\n", 1))

E.append((HLP, 'helpers 加 import', "from wx_config import (mp_appid as _wx_mp_id, mp_secret as _wx_mp_secret,\n",
          IMP_LINE + "from wx_config import (mp_appid as _wx_mp_id, mp_secret as _wx_mp_secret,\n", 1))
E.append((HLP, 'helpers 小程序前缀动态化', "startswith('ooTcRx')", "startswith(mp_openid_prefix())", 7))
E.append((HLP, 'helpers 公众号前缀动态化', "startswith('oLhbm2')", "startswith(oa_openid_prefix())", 3))
E.append((HLP, 'helpers SQL1 绑定参数',
          '                        SELECT mp_openid, unionid FROM phone_openids\n'
          "                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL\n"
          "                          AND mp_openid NOT LIKE 'oLhbm2%%'\n"
          '                        ORDER BY id ASC\n'
          '                    """, (phone,))',
          '                        SELECT mp_openid, unionid FROM phone_openids\n'
          "                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL\n"
          '                          AND mp_openid NOT LIKE %s\n'
          '                        ORDER BY id ASC\n'
          '                    """, (phone, oa_openid_prefix() + \'%\'))', 1))
E.append((HLP, 'helpers SQL2 绑定参数',
          '                        SELECT mp_openid FROM phone_openids\n'
          "                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE 'ooTcRx%%'\n"
          '                        ORDER BY id ASC LIMIT 1\n'
          '                    """, (phone,))',
          '                        SELECT mp_openid FROM phone_openids\n'
          "                        WHERE phone = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE %s\n"
          '                        ORDER BY id ASC LIMIT 1\n'
          '                    """, (phone, mp_openid_prefix() + \'%\'))', 1))
E.append((HLP, 'helpers SQL3 绑定参数',
          '                        SELECT mp_openid FROM phone_openids\n'
          "                        WHERE unionid = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE 'ooTcRx%%'\n"
          '                        ORDER BY id ASC LIMIT 1\n'
          '                    """, (unionid,))',
          '                        SELECT mp_openid FROM phone_openids\n'
          "                        WHERE unionid = %s AND NULLIF(mp_openid,'') IS NOT NULL AND mp_openid LIKE %s\n"
          '                        ORDER BY id ASC LIMIT 1\n'
          '                    """, (unionid, mp_openid_prefix() + \'%\'))', 1))

E.append((PAY, 'payment 加 import', "from wx_config import template_id as _wx_tpl",
          IMP_LINE.rstrip('\n') + "\nfrom wx_config import template_id as _wx_tpl", 1))
E.append((PAY, 'payment 前缀动态化', "startswith('ooTcRx')", "startswith(mp_openid_prefix())", 3))
E.append((PAY, 'payment SQL 绑定参数',
          "mp_openid LIKE 'ooTcRx%%' LIMIT 1\", (order['user_phone'],))",
          "mp_openid LIKE %s LIMIT 1\", (order['user_phone'], mp_openid_prefix() + '%'))", 1))

E.append((ADM, 'admin_v2 加 import', "from wx_config import template_id as _wx_tpl",
          IMP_LINE.rstrip('\n') + "\nfrom wx_config import template_id as _wx_tpl", 1))
E.append((ADM, 'admin_v2 前缀动态化', "startswith('ooTcRx')", "startswith(mp_openid_prefix())", 2))
E.append((ADM, 'admin_v2 SQL1 绑定参数',
          "mp_openid LIKE 'ooTcRx%%' LIMIT 1\", (order_dict['user_phone'],))",
          "mp_openid LIKE %s LIMIT 1\", (order_dict['user_phone'], mp_openid_prefix() + '%'))", 1))
E.append((ADM, 'admin_v2 SQL2 绑定参数',
          "mp_openid LIKE 'ooTcRx%%' ORDER BY updated_at DESC LIMIT 1\", (order_dict['user_phone'],))",
          "mp_openid LIKE %s ORDER BY updated_at DESC LIMIT 1\", (order_dict['user_phone'], mp_openid_prefix() + '%'))", 1))

E.append((USR, 'user 加 import', "from wx_config import template_id as _wx_tpl",
          IMP_LINE.rstrip('\n') + "\nfrom wx_config import template_id as _wx_tpl", 1))
E.append((USR, 'user 公众号前缀动态化', "startswith('oLhbm2')", "startswith(oa_openid_prefix())", 1))

E.append((WXA, 'API create 支持前缀',
          "mch_relation=d.get('mch_relation', 'none'),\n",
          "mch_relation=d.get('mch_relation', 'none'),\n              openid_prefix=d.get('openid_prefix', ''),\n", 1))
E.append((WXA, 'API update 支持前缀',
          "'mch_relation', 'priority', 'note')}",
          "'mch_relation', 'priority', 'note', 'openid_prefix')}", 1))


def main():
    print('=' * 74)
    print('[2D openid前缀] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    print('=' * 74)
    text = {}
    for path in (WXC, WXA, HLP, PAY, ADM, USR):
        text[path] = open(path, encoding='utf-8').read()

    news = dict(text)
    for path, tag, old, new, cnt in E:
        n = news[path].count(old)
        flag = '✓' if n == cnt else '✗'
        print('  %s %-26s %-28s 命中 %d/%d' % (flag, tag, os.path.basename(path), n, cnt))
        if n != cnt:
            raise SystemExit('[中止] %s 的锚点数量不对（期望 %d 实际 %d），一个文件都不写' % (tag, cnt, n))
        news[path] = news[path].replace(old, new)

    print('\n--- 每个文件改动行数 ---')
    for path in (WXC, WXA, HLP, PAY, ADM, USR):
        d = list(difflib.unified_diff(text[path].split('\n'), news[path].split('\n'), lineterm=''))
        add = len([x for x in d if x.startswith('+') and not x.startswith('+++')])
        dele = len([x for x in d if x.startswith('-') and not x.startswith('---')])
        print('  %-24s +%d/-%d' % (os.path.basename(path), add, dele))

    print('\n--- 改完还剩几处写死的前缀（应只剩注释和 DEFAULTS）---')
    for path in (HLP, PAY, ADM, USR):
        rem = [ln.strip() for ln in news[path].split('\n')
               if ('ooTcRx' in ln or 'oLhbm2' in ln)]
        code = [x for x in rem if not x.startswith('#')]
        print('  %-24s 剩 %d 行，其中非注释 %d 行' % (os.path.basename(path), len(rem), len(code)))
        for x in code:
            print('        ! %s' % x[:120])

    if not REAL:
        print('\n干跑结束：没有写任何文件。')
        return 0
    os.makedirs(BK, exist_ok=True)
    tmps = []
    try:
        for path in (WXC, WXA, HLP, PAY, ADM, USR):
            tmp = path + '.pfx_tmp.py'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(news[path])
            py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
            os.remove(tmp + 'c')
            tmps.append((path, tmp))
        print('\n六个文件语法检查全部通过')
    except Exception as e:
        for _, tmp in tmps:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise SystemExit('[中止] 语法检查失败，一个文件都没写：%s' % e)
    for path, tmp in tmps:
        shutil.copy2(path, os.path.join(BK, os.path.basename(path)))
        os.replace(tmp, path)
        print('  已写入 %s' % os.path.basename(path))
    print('备份: %s' % BK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
