# -*- coding: utf-8 -*-
"""
[1] 前端写死的小程序编号改成服务端下发
[2] "切换/启用"前自动探活：探活不过就不让切

改动文件：
  app.py                     —— 不管网点开不开"允许H5跳小程序"，都注入当前小程序 appid
  static/deposit.html        —— 3 处写死编号去掉；拿不到编号时不跳（不再跳旧号）
  wx_config.py               —— switch_to / set_active 加 probe_first（默认 True）+ prober 注入
  wx_config_api.py           —— 切换/启用把探活器传进去；支持显式 skip_probe 强制切
  static/wx_accounts_page.js —— 切换确认框里说明"会自动探活"
  测试文件                    —— 老用例补 probe_first=False；新增 t44
"""
import os
import sys
import time
import shutil
import subprocess
import py_compile

APP = '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = os.path.join(APP, 'backups', 'probe_' + TS)

APP_PY = APP + '/app.py'
DEP = APP + '/static/deposit.html'
WXC = APP + '/wx_config.py'
WXA = APP + '/wx_config_api.py'
JS = APP + '/static/wx_accounts_page.js'
TST = '/tmp/wxcfg_step0/tests/test_wx_config.py'

E = []

# ---------- [1] 前端编号 ----------
E.append((APP_PY, '总是注入小程序编号',
          '            if row["allow_h5_to_mp"]:\n'
          '                import config as _cfg\n'
          '                _ssr["mp_appid"] = _wx_mp_id()\n'
          '                _ssr["mp_path"] = "pages/subscribe/subscribe"\n',
          '            # [CFG-STEP2E] 不管这个网点开不开"允许H5跳小程序"，都把当前小程序的 appid\n'
          '            #   注入给页面。以前只在 allow_h5_to_mp=1 时注入，页面拿不到就退回页面上写死的\n'
          '            #   旧 appid —— 换小程序之后会把用户送到旧小程序里去。\n'
          '            _ssr["mp_appid"] = _wx_mp_id()\n'
          '            if row["allow_h5_to_mp"]:\n'
          '                _ssr["mp_path"] = "pages/subscribe/subscribe"\n', 1))
E.append((DEP, '去掉 prefetch 里的写死编号',
          '        if (mpSchemeDone) { return; }\n'
          "        if (!mpAppId) { mpAppId = 'wxcabd4cbdb3096c4b'; }\n",
          '        if (mpSchemeDone) { return; }\n'
          '        // [CFG-STEP2E] 原来这里写死了旧小程序编号；本函数用的是服务端接口，不需要它\n', 1))
E.append((DEP, 'launchMp 拿不到编号就不跳',
          '    function launchMp() {\n'
          "        if (!mpAppId) { mpAppId = 'wxcabd4cbdb3096c4b'; }\n",
          '    function launchMp() {\n'
          '        // [CFG-STEP2E] 编号由服务端下发（读配置中心）；拿不到就别跳，绝不能跳旧号\n'
          "        if (!mpAppId) { showToast('小程序编号没拿到，请退出重新进入'); return; }\n", 1))
E.append((DEP, 'mpAutoJump 拿不到编号就不跳',
          '    function mpAutoJump() {\n'
          "        if (!mpAppId) { mpAppId = 'wxcabd4cbdb3096c4b'; }\n",
          '    function mpAutoJump() {\n'
          '        // [CFG-STEP2E] 同上：编号由服务端下发，拿不到就走人工兜底弹窗，不跳旧号\n'
          '        if (!mpAppId) { showMpTipModal(); return; }\n', 1))

# ---------- [2] 切换前自动探活 ----------
E.append((WXC, 'switch_to 支持探活',
          "def switch_to(acct_type, account_id, operator='local-demo', reason='手动切换'):",
          "def switch_to(acct_type, account_id, operator='local-demo', reason='手动切换',\n"
          "              probe_first=True, prober=None):", 1))
E.append((WXC, 'switch_to 探活不过不许切',
          "    _ok, _why = check_usable(row)\n"
          "    if not _ok:\n"
          "        return False, '不能切换「%s」：%s（请先把编号/密钥填成真实值）' % (row.get('name') or account_id, _why)\n"
          "    old = get_effective_account(acct_type, use_cache=False)\n",
          "    _ok, _why = check_usable(row)\n"
          "    if not _ok:\n"
          "        return False, '不能切换「%s」：%s（请先把编号/密钥填成真实值）' % (row.get('name') or account_id, _why)\n"
          "    # [GUARD3-20260913] 切换前先探活：拿新号的编号+密钥真连一次微信。\n"
          "    #   不这么做的话，编号填错的号一切过去就是全线不通（授权/登录/支付/通知全挂）。\n"
          "    #   探活不过 -> 拒绝切换；想过 -> 用 probe_first=False（或后台传 skip_probe）。\n"
          "    if probe_first:\n"
          "        _pr = probe(account_id, prober=prober)\n"
          "        if not _pr.get('ok'):\n"
          "            return False, ('不能切换「%s」：探活没过（%s）。'\n"
          "                           '先在后台点一下探活看清楚，把编号/密钥改对再切。'\n"
          "                           % (row.get('name') or account_id,\n"
          "                              _pr.get('detail') or _pr.get('reason') or ''))\n"
          "    old = get_effective_account(acct_type, use_cache=False)\n", 1))
E.append((WXC, 'set_active 支持探活',
          "def set_active(account_id, active=True):",
          "def set_active(account_id, active=True, probe_first=True, prober=None):", 1))
E.append((WXC, 'set_active 探活不过不许启用',
          "    if active:\n"
          "        _ok, _why = check_usable(row)\n"
          "        if not _ok:\n"
          "            return False, '不能启用「%s」：%s' % (row.get('name') or account_id, _why)\n",
          "    if active:\n"
          "        _ok, _why = check_usable(row)\n"
          "        if not _ok:\n"
          "            return False, '不能启用「%s」：%s' % (row.get('name') or account_id, _why)\n"
          "        # [GUARD3-20260913] 启用 = 成为当前生效号，同样先探活\n"
          "        if probe_first:\n"
          "            _pr = probe(account_id, prober=prober)\n"
          "            if not _pr.get('ok'):\n"
          "                return False, ('不能启用「%s」：探活没过（%s）'\n"
          "                               % (row.get('name') or account_id,\n"
          "                                  _pr.get('detail') or _pr.get('reason') or ''))\n", 1))
E.append((WXA, '切换接口传探活器',
          "    reason = _body().get('reason') or '后台手动切换'\n"
          "    done, msg = C.switch_to(row['acct_type'], account_id, operator='local-admin', reason=reason)\n",
          "    body = _body()\n"
          "    reason = body.get('reason') or '后台手动切换'\n"
          "    # [GUARD3-20260913] 默认先探活再切；确实要强切就显式传 skip_probe=true（日志会留痕）\n"
          "    _pf = not body.get('skip_probe')\n"
          "    if not _pf:\n"
          "        reason = (reason + '｜已显式跳过探活')[:255]\n"
          "    done, msg = C.switch_to(row['acct_type'], account_id, operator='local-admin', reason=reason,\n"
          "                            probe_first=_pf, prober=_PROBER)\n", 1))
E.append((WXA, '启用接口传探活器',
          "    done, msg = C.set_active(account_id, bool(active))\n",
          "    done, msg = C.set_active(account_id, bool(active),\n"
          "                             probe_first=not _body().get('skip_probe'), prober=_PROBER)\n", 1))
E.append((JS, '确认框说明会自动探活',
          "'？\\n切换立刻生效，不用重启。'",
          "'？\\n切换前会自动探活（拿新号真连一次微信），探活不过不会切；切了立刻生效，不用重启。'", 1))

# ---------- 测试 ----------
E.append((TST, 't05 不探活', "C.switch_to('mp', backup['id'], operator='test', reason='模拟账号被封')",
          "C.switch_to('mp', backup['id'], operator='test', reason='模拟账号被封', probe_first=False)", 1))
E.append((TST, 't37 不探活(切)',
          "    ok1, msg1 = C.switch_to('mp', new_id, reason='测试')",
          "    ok1, msg1 = C.switch_to('mp', new_id, reason='测试', probe_first=False)", 1))
E.append((TST, 't37 不探活(切回)',
          "    ok2, msg2 = C.switch_to('mp', back['id'], reason='测试切回')",
          "    ok2, msg2 = C.switch_to('mp', back['id'], reason='测试切回', probe_first=False)", 1))
E.append((TST, 't42 不探活(切)',
          "    ok1, msg1 = C.switch_to('mp', nid, reason='前缀测试')",
          "    ok1, msg1 = C.switch_to('mp', nid, reason='前缀测试', probe_first=False)", 1))
E.append((TST, 't42 不探活(切回)',
          "    ok2, msg2 = C.switch_to('mp', back['id'], reason='前缀测试切回')",
          "    ok2, msg2 = C.switch_to('mp', back['id'], reason='前缀测试切回', probe_first=False)", 1))
E.append((TST, 't16 HTTP 切换前装假探活器',
          "    c = _client()\n"
          "    # [GUARD-20260913] 备用位是占位符（会被拒），先造真备用号；两边都先体检通过\n",
          "    c = _client()\n"
          "    # [GUARD3-20260913] 切换现在会先探活，测试里把它换成假的（不然会真连微信）\n"
          "    A.set_prober(lambda acct: (True, '模拟探活通过'))\n"
          "    # [GUARD-20260913] 备用位是占位符（会被拒），先造真备用号；两边都先体检通过\n", 1))
E.append((TST, 't16 用完清掉探活器',
          "        A.use_auth_decorator(None)          # 清掉，别影响后面的用例",
          "        A.use_auth_decorator(None)          # 清掉，别影响后面的用例\n"
          "        try:\n"
          "            A.set_prober(None)\n"
          "        except Exception:\n"
          "            pass", 1))

E.append((TST, 't16 用完立刻清掉探活器',
          "    log = c.get('/api/wx-config/log?limit=10').get_json()['data']\n"
          "    assert any(l['operator'] == 'auto' for l in log), log\n",
          "    log = c.get('/api/wx-config/log?limit=10').get_json()['data']\n"
          "    assert any(l['operator'] == 'auto' for l in log), log\n"
          "    A.set_prober(None)          # [GUARD3] 别让假探活器漏给后面的用例\n", 1))

T44 = '''@test
def t44_switch_requires_probe_pass():
    """切换/启用前必须探活通过：填了但填错的号不许切过去（切完会全线不通）"""
    fresh()
    nid = C.create_account('mp', '密钥填错的号', 'wxwrongkey01', secret='wrongsecret')
    # 探活失败 -> 拒绝切换，且生效号不能被动
    done, msg = C.switch_to('mp', nid, reason='测试', prober=lambda acct: (False, 'errcode=40013 invalid appid'))
    assert done is False, '探活没过居然让切了！%s' % msg
    assert '探活' in msg, msg
    assert _mp_effective()['appid'] == 'wxcabd4cbdb3096c4b', '生效号被改动了'
    # 探活通过 -> 允许切换
    done2, msg2 = C.switch_to('mp', nid, reason='测试', prober=lambda acct: (True, 'access_token 正常'))
    assert done2 is True, msg2
    assert _mp_effective()['appid'] == 'wxwrongkey01'
    # 启用接口同样要探活
    d3, m3 = C.set_active(nid, True, prober=lambda acct: (False, 'boom'))
    assert d3 is False, '启用没做探活: %s' % m3
    # 显式跳过探活时仍然可以切（留个后门）
    back = [a for a in C.list_accounts('mp') if a['appid'] == 'wxcabd4cbdb3096c4b'][0]
    done3, msg3 = C.switch_to('mp', back['id'], reason='切回', probe_first=False)
    assert done3 is True, msg3
    assert _mp_effective()['appid'] == 'wxcabd4cbdb3096c4b'
    print('      （拒绝信息：%s）' % msg)


'''

ANCHOR = """# ------------------------------------------------------------
def main():"""
E.append((TST, '新增 t44', ANCHOR, T44 + ANCHOR, 1))


def syntax_ok(path, tmp):
    if path.endswith('.py'):
        py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
        if os.path.exists(tmp + 'c'):
            os.remove(tmp + 'c')
        return True
    if path.endswith('.js'):
        r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError('node --check 失败: %s' % (r.stderr or r.stdout)[:300])
        return True
    return True


def main():
    print('=' * 74)
    print('[STEP2E/3] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    print('=' * 74)
    files = [APP_PY, DEP, WXC, WXA, JS, TST]
    text = {p: open(p, encoding='utf-8').read() for p in files}
    news = dict(text)
    for path, tag, old, new, cnt in E:
        n = news[path].count(old)
        print('  %s %-26s %-22s 命中 %d/%d' % ('✓' if n == cnt else '✗', tag, os.path.basename(path), n, cnt))
        if n != cnt:
            raise SystemExit('[中止] %s 锚点数量不对（%d/%d），一个文件都不写' % (tag, n, cnt))
        news[path] = news[path].replace(old, new)

    print('\n--- 每个文件改动行数 ---')
    for p in files:
        import difflib
        d = list(difflib.unified_diff(text[p].split('\n'), news[p].split('\n'), lineterm=''))
        print('  %-24s +%d/-%d' % (os.path.basename(p),
                                   len([x for x in d if x.startswith('+') and not x.startswith('+++')]),
                                   len([x for x in d if x.startswith('-') and not x.startswith('---')])))
    if not REAL:
        print('\n干跑结束：没有写文件。')
        return 0
    os.makedirs(BK, exist_ok=True)
    tmps = []
    try:
        for p in files:
            tmp = p + '.pbtmp' + os.path.splitext(p)[1]
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(news[p])
            syntax_ok(p, tmp)
            tmps.append((p, tmp))
        print('\n全部文件语法检查通过')
    except Exception as e:
        for _, tmp in tmps:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise SystemExit('[中止] 语法检查失败，一个文件都没写：%s' % e)
    for p, tmp in tmps:
        shutil.copy2(p, os.path.join(BK, os.path.basename(p)))
        os.replace(tmp, p)
        print('  已写入 %s' % os.path.basename(p))
    print('备份: %s' % BK)
    for name in ('wx_config.py', 'wx_config_api.py'):
        shutil.copy2(os.path.join(APP, name), '/tmp/wxcfg_step0/' + name)
    print('已同步到测试目录')
    return 0


if __name__ == '__main__':
    sys.exit(main())
