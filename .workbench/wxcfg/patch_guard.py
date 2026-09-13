# -*- coding: utf-8 -*-
"""
[T-1789308446] 防呆补丁：不许切到"没配好"的账号 + 自动降级只切体检通过的账号

改 4 个文件：
  1) wx_config.py        —— 核心：新增 check_usable()，给 switch_to / set_active / mark_health /
                            probe / snapshot 加保护
  2) wx_config_api.py    —— 账号列表带上"能不能用"字段；/simulate-fail 不许对着正在生效的号用
  3) static/wx_accounts_page.js  —— 不能用的号把"设为生效/启用/探活"按钮灰掉并显示原因
  4) static/wx_accounts_page.css —— 给灰掉的按钮加个样式
  另外把新测试插进 /tmp/wxcfg_step0/tests/test_wx_config.py（在 main() 之前）

用法:
  python3 patch_guard.py          # 干跑，只看不改
  python3 patch_guard.py --real   # 真改（每个文件先备份 + 语法检查 + 数量校验；任何一处不符就一个都不写）
"""
import os
import sys
import time
import shutil
import difflib
import py_compile
import subprocess

APP = '/home/ubuntu/smart-locker'
TESTDIR = '/tmp/wxcfg_step0'
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = os.path.join(APP, 'backups', 'guard_' + TS)

WXC = os.path.join(APP, 'wx_config.py')
WXA = os.path.join(APP, 'wx_config_api.py')
JS = os.path.join(APP, 'static', 'wx_accounts_page.js')
CSS = os.path.join(APP, 'static', 'wx_accounts_page.css')
TST = os.path.join(TESTDIR, 'tests', 'test_wx_config.py')

# ============================================================
# 1) wx_config.py
# ============================================================
HELPER = '''# ============================================================
# [GUARD-20260913] 账号"能不能真的用"的判定（防呆）
#   为什么要有这个：备用位现在是占位符(REPLACE_ME_*)且密钥是空的，
#   一旦被切为生效，微信那边一律报无效 appid —— 授权/登录/支付/通知全挂。
#   实测踩过：自动降级会挑这个假账号顶上来，等于"从坏直接变成全坏"。
# ============================================================
PLACEHOLDER_MARKERS = ('REPLACE_ME', 'CHANGE_ME', 'PLACEHOLDER', 'TODO', 'XXX', '待填')


def check_usable(row):
    """账号能不能真的用：编号和密钥都要有，编号不能还是占位符。返回 (能不能用, 原因)"""
    if not row:
        return False, '账号不存在'
    appid = str(row.get('appid') or '').strip()
    secret = str(row.get('secret') or '').strip()
    if not appid:
        return False, '编号(appid)是空的'
    if any(m in appid.upper() for m in PLACEHOLDER_MARKERS):
        return False, '编号还是占位符 %s，请先填真实编号' % appid
    if not secret:
        return False, '密钥(secret)是空的，请先填真实密钥'
    return True, ''


def set_active(account_id, active=True):'''

E = []          # (文件标识, 说明, 旧文本, 新文本)
E.append(('wxc', '加 check_usable() 判定函数', 'def set_active(account_id, active=True):', HELPER))

E.append(('wxc', 'set_active 加保护', """    row = get_account(account_id)
    if not row:
        return False, '账号不存在'
    at = row['acct_type']
    with _conn() as (conn, kind):
        if active:""", """    row = get_account(account_id)
    if not row:
        return False, '账号不存在'
    if active:
        _ok, _why = check_usable(row)
        if not _ok:
            return False, '不能启用「%s」：%s' % (row.get('name') or account_id, _why)
    at = row['acct_type']
    with _conn() as (conn, kind):
        if active:"""))

E.append(('wxc', 'switch_to 加保护', """    if row['acct_type'] != acct_type:
        return False, '账号类型不匹配'
    old = get_effective_account(acct_type, use_cache=False)""", """    if row['acct_type'] != acct_type:
        return False, '账号类型不匹配'
    _ok, _why = check_usable(row)
    if not _ok:
        return False, '不能切换「%s」：%s（请先把编号/密钥填成真实值）' % (row.get('name') or account_id, _why)
    old = get_effective_account(acct_type, use_cache=False)"""))

E.append(('wxc', 'switch_to 返回带提醒', """    clear_cache(acct_type)
    return True, '已切换到 %s' % row['name']""", """    clear_cache(acct_type)
    _warn = []
    if row.get('auto_disabled'):
        _warn.append('这个号之前被自动停用过，确认问题已修复再切')
    if (row.get('mch_relation') or '') != 'ok':
        _warn.append('这个号还没绑定商户号(mch_relation=%s)，支付可能失败' % (row.get('mch_relation') or 'none'))
    if (row.get('health_status') or '') != 'ok':
        _warn.append('还没探活通过，建议立刻点一下探活确认')
    _msg = '已切换到 %s' % row['name']
    if _warn:
        _msg += '（提醒：' + '；'.join(_warn) + '）'
    return True, _msg"""))

E.append(('wxc', 'mark_health 只挑体检通过的备用', """                        nxt = _row(conn, kind, \"\"\"SELECT * FROM wx_accounts
                                                  WHERE acct_type=? AND id<>? AND COALESCE(auto_disabled,0)=0
                                                  ORDER BY is_active DESC, priority, id LIMIT 1\"\"\",
                                   (at, account_id))
                        if nxt:""", """                        # [GUARD-20260913] 只能切到"探活通过"的账号。
                        # 实测教训：备用位是占位符/空密钥时，自动降级不是保命，
                        # 而是从坏直接变成全坏，所以宁可不切。
                        nxt = _row(conn, kind, \"\"\"SELECT * FROM wx_accounts
                                                  WHERE acct_type=? AND id<>? AND COALESCE(auto_disabled,0)=0
                                                    AND COALESCE(health_status,'')='ok'
                                                  ORDER BY is_active DESC, priority, id LIMIT 1\"\"\",
                                   (at, account_id))
                        _nxt_ok = False
                        if nxt:
                            _nxt_ok, _nxt_why = check_usable(nxt)
                            if not _nxt_ok:
                                out['note'] = 'standby_not_usable'
                                out['standby_reason'] = _nxt_why
                        if nxt and _nxt_ok:"""))

E.append(('wxc', '无合格备用的日志/返回值说明回落', """                            _log_switch(conn, kind, at, row['name'], '',
                                        '自动停用但无备用可用: %s' % detail, 'auto')
                            out['reason'] = 'no_standby'""", """                            _log_switch(conn, kind, at, row['name'], '',
                                        '自动停用但无可用的备用账号(备用必须探活通过): %s' % detail, 'auto')
                            out['reason'] = 'no_standby'
                            out['fallback'] = 'config.py 里原本的账号'"""))

E.append(('wxc', 'probe 跳过没配好的账号', """    row = get_account(account_id)
    if not row:
        return {'ok': False, 'detail': '账号不存在'}
    fn = prober or default_prober""", """    row = get_account(account_id)
    if not row:
        return {'ok': False, 'detail': '账号不存在'}
    # [GUARD-20260913] 编号/密钥还没配好的账号（比如占位符备用位）直接跳过：
    # 不然"没配好"会被记成"体检失败"，连点几次还可能把号弄成自动停用
    _ok, _why = check_usable(row)
    if not _ok:
        return {'ok': False, 'skipped': True, 'account_id': account_id,
                'name': row.get('name') or '', 'detail': '账号还没配好（%s），跳过探活' % _why,
                'health': row.get('health_status') or 'unknown'}
    fn = prober or default_prober"""))

E.append(('wxc', 'snapshot 带上能不能用', """def snapshot(reveal=False):
    accts = list_accounts()
    if not reveal:""", """def snapshot(reveal=False):
    accts = list_accounts()
    # [GUARD-20260913] 顺便告诉后台"这个号能不能真的用"，前端就能把按钮灰掉
    accts = [dict(a, usable=check_usable(a)[0], usable_reason=check_usable(a)[1]) for a in accts]
    if not reveal:"""))

# ============================================================
# 2) wx_config_api.py
# ============================================================
E.append(('wxa', '账号列表带上能不能用', """    rows = C.list_accounts(at)
    if not reveal:
        rows = [dict(r, secret=C.mask(r.get('secret'))) for r in rows]
    return ok(rows)""", """    rows = C.list_accounts(at)
    # [GUARD-20260913] 带上"能不能真的用"，前端才能把不能用的号灰掉
    rows = [dict(r, usable=C.check_usable(r)[0], usable_reason=C.check_usable(r)[1]) for r in rows]
    if not reveal:
        rows = [dict(r, secret=C.mask(r.get('secret'))) for r in rows]
    return ok(rows)"""))

E.append(('wxa', 'simulate-fail 不许对着生效号用', """def api_account_simulate_fail(account_id):
    \"\"\"模拟调用失败 N 次 → 验证"连续失败自动停用 + 自动切备用"这条链路\"\"\"
    _guard()
    body = _body()
    times = int(body.get('times') or 1)""", """def api_account_simulate_fail(account_id):
    \"\"\"模拟调用失败 N 次 → 验证"连续失败自动停用 + 自动切备用"这条链路。

    注意：这是给开发/演示用的接口。如果对着"当前正在生效"的账号用，会真的把它
    自动停用（业务会回落到 config.py 原本的账号），生产上属于误伤，所以默认拦住；
    确实要测就显式传 allow_active=true。
    \"\"\"
    _guard()
    body = _body()
    _row = C.get_account(account_id)
    if _row:
        _eff = C.get_effective_account(_row['acct_type'], use_cache=False)
        if _eff and _eff.get('id') == account_id and not body.get('allow_active'):
            return err('拒绝：这是当前正在生效的%s账号。要模拟它失败请显式传 allow_active=true'
                       % ('小程序' if _row['acct_type'] == 'mp' else '公众号'))
    times = int(body.get('times') or 1)"""))

# ============================================================
# 3) 后台页面
# ============================================================
E.append(('js', 'appid 旁边标注未配好', '<td class="wx-mono">{{a.appid}}</td>',
          '<td class="wx-mono">{{a.appid}}<span v-if="a.usable===false" class="wx-tag off" '
          ':title="a.usable_reason" style="margin-left:4px">未配好</span></td>'))

E.append(('js', '设为生效按钮灰掉',
          '<button class="btn-text green" v-if="!a.is_active" @click="switchAccount(a)">设为生效</button>',
          '<button class="btn-text green" v-if="!a.is_active" :disabled="a.usable===false" '
          ':title="a.usable_reason" @click="switchAccount(a)">设为生效</button>'))

E.append(('js', '启用按钮灰掉',
          '<button class="btn-text blue" v-if="!a.is_active" @click="toggleAccount(a,true)">启用</button>',
          '<button class="btn-text blue" v-if="!a.is_active" :disabled="a.usable===false" '
          ':title="a.usable_reason" @click="toggleAccount(a,true)">启用</button>'))

E.append(('js', '探活按钮灰掉',
          '<button class="btn-text blue" @click="probe(a)">探活</button>',
          '<button class="btn-text blue" :disabled="a.usable===false" :title="a.usable_reason" '
          '@click="probe(a)">探活</button>'))

CSS_ADD = '''
/* [GUARD-20260913] 不能用的账号（编号/密钥还没填）按钮灰掉，鼠标移上去有原因 */
.btn-text[disabled]{opacity:.4;cursor:not-allowed;pointer-events:auto}
'''

# ============================================================
# 4) 测试
# ============================================================
NEW_TESTS = '''# ------------------------------------------------------------
# [GUARD-20260913] 防呆：不能切到"没配好"的账号 / 自动降级只切体检通过的
# ------------------------------------------------------------
@test
def t35_switch_blocked_when_secret_empty():
    """切到密钥为空的账号必须被拒（备用位现在就是空的，一点就全坏）"""
    fresh()
    bad_id = C.create_account('mp', '空密钥备用号', 'wx1234567890abcdef', secret='')
    done, msg = C.switch_to('mp', bad_id, reason='测试')
    assert done is False, '居然允许切到空密钥账号！msg=%s' % msg
    assert '密钥' in msg, msg
    assert _mp_effective()['appid'] == 'wxcabd4cbdb3096c4b', '生效账号被改动了！'
    done2, msg2 = C.set_active(bad_id, True)
    assert done2 is False, '启用接口没拦住: %s' % msg2
    assert _mp_effective()['appid'] == 'wxcabd4cbdb3096c4b'
    print('      （拒绝信息：%s）' % msg)


@test
def t36_switch_blocked_when_placeholder_appid():
    """切到占位符编号的账号必须被拒（种子里就有 REPLACE_ME_MP_BACKUP）"""
    fresh()
    ph = [r for r in C.list_accounts('mp') if str(r['appid']).startswith('REPLACE_ME')]
    assert ph, '种子里的占位账号不见了'
    done, msg = C.switch_to('mp', ph[0]['id'], reason='测试')
    assert done is False, '居然允许切到占位符账号！msg=%s' % msg
    assert '占位' in msg, msg
    assert _mp_effective()['appid'] == 'wxcabd4cbdb3096c4b'
    print('      （拒绝信息：%s）' % msg)


@test
def t37_switch_ok_for_real_account_and_back():
    """真账号（编号+密钥都有）还应该能正常切，切完能切回来"""
    fresh()
    new_id = C.create_account('mp', '备用真号', 'wxtest1234567890', secret='secret1234567890')
    ok1, msg1 = C.switch_to('mp', new_id, reason='测试')
    assert ok1, msg1
    assert _mp_effective()['appid'] == 'wxtest1234567890'
    assert ('探活' in msg1) or ('商户号' in msg1), '提醒信息没带上: %s' % msg1
    back = [r for r in C.list_accounts('mp') if r['appid'] == 'wxcabd4cbdb3096c4b'][0]
    ok2, msg2 = C.switch_to('mp', back['id'], reason='测试切回')
    assert ok2, msg2
    assert _mp_effective()['appid'] == 'wxcabd4cbdb3096c4b'
    print('      （切换提示：%s）' % msg1)


@test
def t38_auto_degrade_only_to_healthy_standby():
    """自动降级：备用号必须"探活通过"才有资格顶上来；没有合格的就宁可不切"""
    fresh()
    act = C.get_effective_account('mp', use_cache=False)
    C.create_account('mp', '备用没体检', 'wxstandby0001', secret='s' * 16)
    res = None
    for _ in range(C.FAIL_THRESHOLD):
        res = C.mark_health(act['id'], False, 'mock 失败')
    assert res['auto_disabled'] is True, res
    assert res['switched'] is False, '切到了没体检过的备用号！%s' % res
    assert res['reason'] == 'no_standby', res
    assert _mp_effective()['source'] == 'default', '应回落到 config.py 原值: %s' % _mp_effective()

    fresh()
    act = C.get_effective_account('mp', use_cache=False)
    new_id = C.create_account('mp', '备用已体检', 'wxstandby0002', secret='s' * 16)
    C.mark_health(new_id, True, 'mock 探活通过')
    res2 = None
    for _ in range(C.FAIL_THRESHOLD):
        res2 = C.mark_health(act['id'], False, 'mock 失败')
    assert res2['switched'] is True, '体检通过的备用号没被切上来: %s' % res2
    assert res2['to_id'] == new_id, res2
    assert _mp_effective()['appid'] == 'wxstandby0002'


@test
def t39_probe_skips_unconfigured_account():
    """探活遇到没配好的账号要跳过，不能记成"体检失败"（更不能把它弄成自动停用）"""
    fresh()
    ph = [r for r in C.list_accounts('mp') if str(r['appid']).startswith('REPLACE_ME')][0]
    r = C.probe(ph['id'])
    assert r.get('skipped') is True, r
    assert '还没配好' in r['detail'], r
    after = C.get_account(ph['id'])
    assert int(after.get('fail_count') or 0) == 0, 'fail_count 被写脏了: %s' % after.get('fail_count')
    assert (after.get('health_status') or 'unknown') == 'unknown', after.get('health_status')
    print('      （跳过信息：%s）' % r['detail'])


@test
def t40_snapshot_exposes_usable_flag():
    """后台要能看出"这个号不能切"：snapshot 每条账号都带 usable/usable_reason"""
    fresh()
    accts = C.snapshot()['accounts']
    assert accts, '快照里没有账号'
    for a in accts:
        assert 'usable' in a and 'usable_reason' in a, a
    ph = [a for a in accts if str(a['appid']).startswith('REPLACE_ME')][0]
    good = [a for a in accts if a['appid'] == 'wxcabd4cbdb3096c4b'][0]
    assert ph['usable'] is False, ph
    assert good['usable'] is True, good
    assert good['usable_reason'] == '', good


# ------------------------------------------------------------
def main():'''

E.append(('tst', '插入 6 个新用例', """# ------------------------------------------------------------
def main():""", NEW_TESTS))


# ============================================================
def syntax_ok(path, tmp):
    """语法检查：.py 用 py_compile，.js 用 node --check"""
    if path.endswith('.py'):
        py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
        if os.path.exists(tmp + 'c'):
            os.remove(tmp + 'c')
        return True
    if path.endswith('.js'):
        r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError('node --check 失败: %s' % (r.stderr or r.stdout).strip()[:400])
        return True
    return True


def main():
    print('=' * 72)
    print('[GUARD] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    print('=' * 72)
    files = {'wxc': WXC, 'wxa': WXA, 'js': JS, 'tst': TST}
    text = {}
    for k, p in files.items():
        if not os.path.exists(p):
            raise SystemExit('[中止] 找不到 %s' % p)
        text[k] = open(p, encoding='utf-8').read()

    # 已经在位就跳过（可重复执行）
    if 'check_usable' in text['wxc'] or 'GUARD-20260913' in text['wxc']:
        print('wx_config.py 看起来已经打过这个补丁了，先确认一下再跑')
        raise SystemExit(1)

    # ---- 校验每个锚点必须唯一 ----
    for k, tag, old, new in E:
        n = text[k].count(old)
        if n != 1:
            raise SystemExit('[中止] %s / %s ：锚点出现 %d 次（应恰好 1 次）' % (k, tag, n))
        print('  ✓ %-28s %s' % (tag, k))
    css_txt = open(CSS, encoding='utf-8').read()
    if 'GUARD-20260913' in css_txt:
        print('  ! css 已加过样式，跳过')

    # ---- 生成新内容 + 预览 ----
    new = {k: v for k, v in text.items()}
    stats = []
    for k, tag, old, newtxt in E:
        new[k] = new[k].replace(old, newtxt, 1)
    new['css'] = css_txt if 'GUARD-20260913' in css_txt else (css_txt.rstrip('\n') + '\n' + CSS_ADD)

    print('\n--- 每个文件的改动行数 ---')
    for k, p in list(files.items()) + [('css', CSS)]:
        a = text.get(k, css_txt).split('\n')
        b = new[k].split('\n')
        d = list(difflib.unified_diff(a, b, lineterm=''))
        add = len([x for x in d if x.startswith('+') and not x.startswith('+++')])
        dele = len([x for x in d if x.startswith('-') and not x.startswith('---')])
        stats.append((p, add, dele))
        print('  %-46s +%d/-%d 行' % (os.path.basename(p), add, dele))

    if not REAL:
        print('\n--- 关键改动预览（wx_config.py 的 mark_health 那段）---')
        a = text['wxc'].split('\n')
        b = new['wxc'].split('\n')
        d = list(difflib.unified_diff(a, b, 'wx_config.py(改前)', 'wx_config.py(改后)', n=2, lineterm=''))
        for ln in d:
            if 'health_status' in ln or 'check_usable' in ln or 'GUARD' in ln:
                print('   ' + ln)
        print('\n干跑结束：没有写任何文件。确认后加 --real。')
        return 0

    # ---- 真写：先全部写入临时文件并做语法检查，全部通过才替换 ----
    os.makedirs(BK, exist_ok=True)
    tmps = []
    try:
        for k, p in list(files.items()) + [('css', CSS)]:
            # 临时文件必须保留原扩展名：node --check 只认 .js，改后缀会报
            # ERR_UNKNOWN_FILE_EXTENSION（踩过一次，好在是全有或全无，没写进去）
            tmp = p + '.guard_tmp' + os.path.splitext(p)[1]
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new[k])
            syntax_ok(p, tmp)
            tmps.append((p, tmp))
        print('\n所有文件语法检查通过')
    except Exception as e:
        for _, tmp in tmps:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise SystemExit('[中止] 语法检查失败，一个文件都没写：%s' % e)

    for p, tmp in tmps:
        shutil.copy2(p, os.path.join(BK, os.path.basename(p)))
        os.replace(tmp, p)
    print('已写入并备份到 %s' % BK)
    print('备份内容: %s' % ', '.join(sorted(os.listdir(BK))))

    # 同步给测试目录（测试跑的是那里的副本）
    for name in ('wx_config.py', 'wx_config_api.py'):
        shutil.copy2(os.path.join(APP, name), os.path.join(TESTDIR, name))
    print('已同步 wx_config.py / wx_config_api.py 到 %s' % TESTDIR)
    return 0


if __name__ == '__main__':
    sys.exit(main())
