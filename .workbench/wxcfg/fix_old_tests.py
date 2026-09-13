# -*- coding: utf-8 -*-
"""
[T-1789308446] 把 6 个"按旧行为写的"老用例改成新规则下的等价用例

为什么必须改：t05/t07/t16/t20/t21/t22 原来是靠"切到/降级到那个占位符备用号"
来验证缓存失效、自动降级、探活恢复的。防呆上线后那条路被主动堵死了
（这正是我们要的），所以它们得改用"真的能用"的备用号来验证同一件事。
用例的意图一个都没丢，只是把"假备用号"换成"真备用号"。
"""
import os
import sys
import time
import shutil
import difflib

TST = '/tmp/wxcfg_step0/tests/test_wx_config.py'
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = '/tmp/wxcfg_step0/backups_test_' + TS

FIX = [
    ('t05 用真备用号测缓存',
     """    backup = [a for a in C.list_accounts('mp') if not a['is_active'] and not a['auto_disabled']][0]
    done, msg = C.switch_to('mp', backup['id'], operator='test', reason='模拟账号被封')""",
     """    # [GUARD-20260913] 种子里的备用位是占位符（现在会被防呆拒绝），先造一个真能用的备用号
    _bid = C.create_account('mp', '备用真号(缓存测试)', 'wxcachetest0001', secret='secret' * 4)
    backup = C.get_account(_bid)
    done, msg = C.switch_to('mp', backup['id'], operator='test', reason='模拟账号被封')"""),

    ('t07 用真备用号测自动降级',
     """    primary = [a for a in C.list_accounts('mp') if a['is_active']][0]
    assert primary['appid'] == 'wxcabd4cbdb3096c4b'
    r1 = C.record_result('mp', False, 'errcode 43004 require subscribe')""",
     """    primary = [a for a in C.list_accounts('mp') if a['is_active']][0]
    assert primary['appid'] == 'wxcabd4cbdb3096c4b'
    # [GUARD-20260913] 新规则：只有"探活通过"的备用号才有资格被自动切上来
    _sid = C.create_account('mp', '备用真号(降级测试)', 'wxfailover0001', secret='secret' * 4)
    C.mark_health(_sid, True, '先体检通过')
    r1 = C.record_result('mp', False, 'errcode 43004 require subscribe')"""),

    ('t16 走 HTTP 的切换+探活+降级',
     """    c = _client()
    mp = [a for a in c.get('/api/wx-config/accounts?type=mp').get_json()['data']]
    standby = [a for a in mp if not a['is_active'] and not a['auto_disabled']][0]
    r = c.post('/api/wx-config/accounts/%d/switch' % standby['id'], json={'reason': 'HTTP 切换测试'}).get_json()
    assert r['code'] == 200 and r['data']['effective']['appid'] == standby['appid'], r
    # 探活（模拟成功）
    r = c.post('/api/wx-config/accounts/%d/probe' % standby['id'], json={'mode': 'simulate', 'ok': True}).get_json()
    assert r['code'] == 200 and r['data']['ok'] is True, r
    # 失败×3 → 自动降级
    r = c.post('/api/wx-config/accounts/%d/simulate-fail' % standby['id'], json={'times': 3}).get_json()
    assert r['code'] == 200, r
    last = r['data']['results'][-1]
    assert last['auto_disabled'] is True or last['switched'] is True, last
    eff = c.get('/api/wx-config/effective').get_json()['data']['mp']
    assert eff['appid'] != standby['appid'], eff""",
     """    c = _client()
    # [GUARD-20260913] 备用位是占位符（会被拒），先造真备用号；两边都先体检通过
    primary = [a for a in C.list_accounts('mp') if a['is_active']][0]
    C.mark_health(primary['id'], True, '先体检通过')
    sid = C.create_account('mp', 'HTTP备用真号', 'wxhttptest0001', secret='secret' * 4)
    C.mark_health(sid, True, '先体检通过')
    standby = C.get_account(sid)
    r = c.post('/api/wx-config/accounts/%d/switch' % sid, json={'reason': 'HTTP 切换测试'}).get_json()
    assert r['code'] == 200 and r['data']['effective']['appid'] == standby['appid'], r
    # 探活（模拟成功）
    r = c.post('/api/wx-config/accounts/%d/probe' % sid, json={'mode': 'simulate', 'ok': True}).get_json()
    assert r['code'] == 200 and r['data']['ok'] is True, r
    # [GUARD-20260913] 对"当前正在生效"的号模拟失败要被拒，必须显式 allow_active
    r = c.post('/api/wx-config/accounts/%d/simulate-fail' % sid, json={'times': 1}).get_json()
    assert r['code'] == 400 and '拒绝' in (r.get('message') or ''), r
    # 失败×3 → 自动降级（切回体检通过的原生效号）
    r = c.post('/api/wx-config/accounts/%d/simulate-fail' % sid, json={'times': 3, 'allow_active': True}).get_json()
    assert r['code'] == 200, r
    last = r['data']['results'][-1]
    assert last['auto_disabled'] is True and last['switched'] is True, last
    eff = c.get('/api/wx-config/effective').get_json()['data']['mp']
    assert eff['appid'] == primary['appid'], eff"""),

    ('t20 用真备用号测"不重复降级"',
     """    active = [a for a in C.list_accounts('mp') if a['is_active']][0]
    r = None
    for _ in range(3):
        r = C.mark_health(active['id'], False, 'boom')
    assert r['switched'] is True, r""",
     """    active = [a for a in C.list_accounts('mp') if a['is_active']][0]
    # [GUARD-20260913] 先造一个体检通过的备用号，否则按新规则不会切
    _sid = C.create_account('mp', '备用真号(重复降级测试)', 'wxrepeat0001', secret='secret' * 4)
    C.mark_health(_sid, True, '先体检通过')
    r = None
    for _ in range(3):
        r = C.mark_health(active['id'], False, 'boom')
    assert r['switched'] is True, r"""),

    ('t21 用真备用号测探活恢复',
     """    standby = [a for a in C.list_accounts('mp') if not a['is_active']][0]
    for _ in range(3):
        C.mark_health(standby['id'], False, 'x')""",
     """    # [GUARD-20260913] 占位符备用号现在探活会被跳过（t39 验证这件事），所以造一个真备用号
    _sid = C.create_account('mp', '备用真号(探活恢复测试)', 'wxrecov0001', secret='secret' * 4)
    standby = C.get_account(_sid)
    for _ in range(3):
        C.mark_health(standby['id'], False, 'x')"""),

    ('t22 日志措辞跟着改',
     """    \"\"\"最后一个号也挂了 → 记一次「无备用可用」，不是每次都记\"\"\"""",
     """    \"\"\"最后一个号也挂了 → 记一次「无可用备用账号」，不是每次都记\"\"\""""),

    ('t22 匹配新的日志措辞',
     """    no_standby = [l for l in log if '无备用可用' in (l['reason'] or '')]""",
     """    no_standby = [l for l in log if '无可用的备用账号' in (l['reason'] or '')]"""),
]


def main():
    print('=' * 72)
    print('[测试改写] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    print('=' * 72)
    src = open(TST, encoding='utf-8').read()
    for tag, old, new in FIX:
        n = src.count(old)
        if n != 1:
            raise SystemExit('[中止] %s ：锚点出现 %d 次（应恰好 1 次）' % (tag, n))
        print('  ✓ %s' % tag)
    new_src = src
    for tag, old, new in FIX:
        new_src = new_src.replace(old, new, 1)

    d = list(difflib.unified_diff(src.split('\n'), new_src.split('\n'), 'test(改前)', 'test(改后)', lineterm=''))
    add = len([x for x in d if x.startswith('+') and not x.startswith('+++')])
    dele = len([x for x in d if x.startswith('-') and not x.startswith('---')])
    print('\n改动: +%d/-%d 行' % (add, dele))
    for ln in d:
        if ln.startswith('+') and not ln.startswith('+++'):
            print('   ' + ln)

    if not REAL:
        print('\n干跑结束：没有写任何文件。')
        return 0

    compile(new_src, TST, 'exec')
    os.makedirs(BK, exist_ok=True)
    shutil.copy2(TST, os.path.join(BK, 'test_wx_config.py'))
    tmp = TST + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new_src)
    compile(open(tmp, encoding='utf-8').read(), tmp, 'exec')
    os.replace(tmp, TST)
    print('\n✅ 已改写（备份 %s）' % BK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
