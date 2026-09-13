# -*- coding: utf-8 -*-
"""
配置中心本地测试：直接跑，不需要 pytest
    python tests/test_wx_config.py
覆盖：账号池 / 一键切换 / 缓存失效 / 连续失败自动降级 / 全挂兜底 /
      配置项 / 模板 / HTTP 接口 / 占位符兼容 PG
"""

import os
import sqlite3
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import seed as S            # noqa: E402
import wx_config as C       # noqa: E402

try:
    import wx_config_api as A   # 需要 Flask；服务器上没装就跳过 HTTP 用例
except Exception:
    A = None

TESTS = []
_TMP = tempfile.mkdtemp(prefix='wxcfg_test_')
_DB = os.path.join(_TMP, 'test.db')

# 设了 WXCFG_TEST_PG 就跑真 PostgreSQL（例：
#   WXCFG_TEST_PG=postgresql://user:pw@127.0.0.1:5432/wxcfg_test python tests/test_wx_config.py）
_PG_DSN = os.environ.get('WXCFG_TEST_PG') or ''
_TABLES = ('wx_accounts', 'wx_templates', 'wx_switch_log', 'wx_config_items')


def test(fn):
    TESTS.append(fn)
    return fn


def _factory():
    if _PG_DSN:
        import psycopg2
        return psycopg2.connect(_PG_DSN)
    return sqlite3.connect(_DB)


def fresh(with_seed=True):
    """每个用例从一个干净的库开始（SQLite 删文件 / PostgreSQL 清表）"""
    C.bind(_factory)
    C.init_db()
    with C._conn() as (conn, kind):
        for t in _TABLES:
            try:
                C._exec(conn, kind, 'DELETE FROM %s' % t)
            except Exception:
                pass
    C.clear_cache()
    if with_seed:
        S.seed(reset=True)
    return C


def _mp_effective():
    return C.resolve('mp')


# ------------------------------------------------------------
# 基础
# ------------------------------------------------------------
@test
def t01_sql_placeholder_pg_compatible():
    assert C._sql('SELECT a FROM t WHERE b=?', 'pg') == 'SELECT a FROM t WHERE b=%s'
    assert C._sql('SELECT a FROM t WHERE b=?', 'sqlite') == 'SELECT a FROM t WHERE b=?'


@test
def t02_init_db_idempotent():
    fresh(with_seed=False)
    C.init_db()
    C.init_db()
    assert C.list_accounts() == []
    assert C.get_config('h5_base') == 'https://locker.cqdyxl.com'   # 空库也要能取到兜底值


@test
def t03_seed_and_effective():
    fresh()
    accts = C.list_accounts()
    assert len(accts) == 5, '种子应有 5 个账号，实际 %d' % len(accts)
    eff = _mp_effective()
    assert eff['appid'] == 'wxcabd4cbdb3096c4b', eff
    assert eff['source'] == 'db', eff
    assert eff['name'].startswith('伧置'), eff
    oa = C.resolve('oa')
    assert oa['appid'] == 'wxd85204d0ec930d46', oa
    # 同类只有一个生效
    active_mp = [a for a in accts if a['acct_type'] == 'mp' and a['is_active']]
    assert len(active_mp) == 1, active_mp


@test
def t04_unknown_type_never_raises():
    fresh()
    r = C.resolve('nope')
    assert r.get('source') == 'default'
    assert not r.get('appid')


# ------------------------------------------------------------
# 一键切换 + 缓存
# ------------------------------------------------------------
@test
def t05_switch_is_immediate_even_with_cache():
    fresh()
    first = _mp_effective()                      # 写进缓存
    assert first['appid'] == 'wxcabd4cbdb3096c4b'
    # [GUARD-20260913] 种子里的备用位是占位符（现在会被防呆拒绝），先造一个真能用的备用号
    _bid = C.create_account('mp', '备用真号(缓存测试)', 'wxcachetest0001', secret='secret' * 4)
    backup = C.get_account(_bid)
    done, msg = C.switch_to('mp', backup['id'], operator='test', reason='模拟账号被封', probe_first=False)
    assert done, msg
    after = _mp_effective()                      # 必须立刻变，不能等缓存过期
    assert after['appid'] == backup['appid'], (after, backup['appid'])
    assert after['source'] == 'db'
    # 原来那个必须被停用
    old = C.get_account(first['account_id'])
    assert not old['is_active'], old
    # 同类仍然只有一个生效
    live = [a for a in C.list_accounts('mp') if a['is_active']]
    assert len(live) == 1, live
    # 有日志
    log = C.get_log(5)
    assert log and log[0]['reason'] == '模拟账号被封', log


@test
def t06_manual_switch_back_clears_auto_disabled():
    fresh()
    acct = C.list_accounts('mp')[0]
    C.switch_to('mp', [a for a in C.list_accounts('mp') if a['id'] != acct['id']][0]['id'])
    # 再切回来：auto_disabled / fail_count 必须清零，否则以后再也选不上
    C.mark_health(acct['id'], False, 'x')
    C.switch_to('mp', acct['id'], reason='切回主号')
    a = C.get_account(acct['id'])
    assert a['is_active'] == 1 and a['auto_disabled'] == 0 and a['fail_count'] == 0, a


# ------------------------------------------------------------
# 自动降级（= 支付渠道的 auto_disabled）
# ------------------------------------------------------------
@test
def t07_three_failures_auto_disable_and_failover():
    fresh()
    primary = [a for a in C.list_accounts('mp') if a['is_active']][0]
    assert primary['appid'] == 'wxcabd4cbdb3096c4b'
    # [GUARD-20260913] 新规则：只有"探活通过"的备用号才有资格被自动切上来
    _sid = C.create_account('mp', '备用真号(降级测试)', 'wxfailover0001', secret='secret' * 4)
    C.mark_health(_sid, True, '先体检通过')
    r1 = C.record_result('mp', False, 'errcode 43004 require subscribe')
    r2 = C.record_result('mp', False, 'errcode 43004 require subscribe')
    assert not r1.get('auto_disabled') and not r2.get('auto_disabled'), (r1, r2)
    r3 = C.record_result('mp', False, 'errcode 43004 require subscribe')
    assert r3.get('auto_disabled') is True, r3
    assert r3.get('switched') is True, r3
    p = C.get_account(primary['id'])
    assert p['auto_disabled'] == 1 and p['is_active'] == 0, p
    new = _mp_effective()
    assert new['appid'] != primary['appid'], new
    assert new['account_id'] == r3['to_id'], (new, r3)
    log = C.get_log(3)
    assert log[0]['operator'] == 'auto' and '自动降级' in log[0]['reason'], log


@test
def t08_success_resets_counter():
    fresh()
    C.record_result('mp', False, 'fail-1')
    C.record_result('mp', False, 'fail-2')
    C.record_result('mp', True, 'ok')
    row = [a for a in C.list_accounts('mp') if a['is_active']][0]
    assert row['fail_count'] == 0 and row['health_status'] == 'ok', row
    r = C.record_result('mp', False, 'fail-again')     # 重新从 1 开始数
    assert r.get('fail_count') == 1, r


@test
def t09_all_accounts_dead_falls_back_to_config_py():
    fresh()
    for a in C.list_accounts('mp'):
        C.mark_health(a['id'], False, 'x')   # 逐个打死
        C.mark_health(a['id'], False, 'x')
        C.mark_health(a['id'], False, 'x')
    eff = _mp_effective()
    assert eff['source'] == 'default', eff
    assert eff['appid'] == 'wxcabd4cbdb3096c4b', eff   # 回落 config.py 现值，业务不断
    assert all(a['auto_disabled'] or not a['is_active'] for a in C.list_accounts('mp'))


@test
def t10_delete_active_refused():
    fresh()
    act = [a for a in C.list_accounts('mp') if a['is_active']][0]
    ok, msg = C.delete_account(act['id'])
    assert not ok and '生效' in msg, msg
    standby = [a for a in C.list_accounts('mp') if not a['is_active']][0]
    ok2, _ = C.delete_account(standby['id'])
    assert ok2
    assert len(C.list_accounts('mp')) == 2


@test
def t11_probe_uses_injected_prober():
    fresh()
    act = [a for a in C.list_accounts('oa') if a['is_active']][0]
    r = C.probe(act['id'], prober=lambda acct: (True, '模拟OK'))
    assert r['ok'] and r['health'] == 'ok'
    row = C.get_account(act['id'])
    assert row['health_status'] == 'ok', row
    C.probe(act['id'], prober=lambda acct: (False, '模拟被封'))
    assert C.get_account(act['id'])['health_status'] == 'fail'


# ------------------------------------------------------------
# 配置项 / 模板
# ------------------------------------------------------------
@test
def t12_config_switch_domain_without_code_change():
    fresh()
    assert C.get_config('h5_base') == 'https://locker.cqdyxl.com'
    assert C.h5_url('/store') == 'https://locker.cqdyxl.com/store'
    C.set_config('h5_base', 'https://locker2.example.com', group_name='域名')
    assert C.get_config('h5_base') == 'https://locker2.example.com'
    assert C.h5_url('store') == 'https://locker2.example.com/store'
    items = {i['cfg_key']: i for i in C.list_config_items()}
    assert items['h5_base']['from_db'] is True
    assert items['h5_base']['default_value'] == 'https://locker.cqdyxl.com'


@test
def t13_templates_real_ids_and_override():
    fresh()
    t = C.get_template('subscribe_deposit', 'mp')
    assert t and t['template_id'] == 'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk', t
    t2 = C.get_template('oa_refund_ok', 'oa')
    assert t2 and t2['template_id'].startswith('vbVbCS87'), t2
    m = C.get_templates_map('mp')
    assert set(m.keys()) == {'subscribe_deposit', 'subscribe_refund', 'subscribe_general'}, m.keys()
    C.set_template('subscribe_deposit', 'mp', 'NEW_TPL_ID', page='pages/index/index', note='换号后新模板')
    assert C.get_template('subscribe_deposit', 'mp')['template_id'] == 'NEW_TPL_ID'
    assert len([x for x in C.list_templates('mp') if x['biz'] == 'subscribe_deposit']) == 1
    # 账号专属模板优先于通用
    C.set_template('subscribe_deposit', 'mp', 'TPL_FOR_ACCT', account_id=99)
    assert C.get_template('subscribe_deposit', 'mp', account_id=99)['template_id'] == 'TPL_FOR_ACCT'
    assert C.get_template('subscribe_deposit', 'mp', account_id=1)['template_id'] == 'NEW_TPL_ID'


@test
def t14_mask_hides_secret():
    m = C.mask('f8d9d68772401f4fdda4a2d2d6143988')
    assert m.startswith('f8d9d6') and m.endswith('****') and '72401' not in m, m
    assert C.mask('') == '*'


# ------------------------------------------------------------
# HTTP 接口（以后直接挂到服务器后台）
# ------------------------------------------------------------
def _client():
    if A is None:
        raise RuntimeError('SKIP: 没有 Flask，跳过 HTTP 用例')
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(A.bp)
    app.testing = True
    return app.test_client()


@test
def t15_api_snapshot_and_secret_masking():
    fresh()
    c = _client()
    r = c.get('/api/wx-config/snapshot').get_json()
    assert r['code'] == 200
    assert r['data']['effective']['mp']['appid'] == 'wxcabd4cbdb3096c4b'
    secrets = [a['secret'] for a in r['data']['accounts'] if a['secret']]
    assert secrets and all('…' in s or s == '*' for s in secrets), secrets
    r2 = c.get('/api/wx-config/snapshot?reveal=1').get_json()
    full = [a['secret'] for a in r2['data']['accounts'] if a['appid'] == 'wxcabd4cbdb3096c4b'][0]
    assert full == 'f8d9d68772401f4fdda4a2d2d6143988', full


@test
def t16_api_switch_probe_and_failover_flow():
    fresh()
    c = _client()
    # [GUARD3-20260913] 切换现在会先探活，测试里把它换成假的（不然会真连微信）
    A.set_prober(lambda acct: (True, '模拟探活通过'))
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
    assert eff['appid'] == primary['appid'], eff
    # 日志里有 auto
    log = c.get('/api/wx-config/log?limit=10').get_json()['data']
    assert any(l['operator'] == 'auto' for l in log), log
    A.set_prober(None)          # [GUARD3] 别让假探活器漏给后面的用例


@test
def t17_api_config_write_and_validation():
    fresh()
    c = _client()
    r = c.post('/api/wx-config/config', json={'key': 'h5_backup_domains', 'value': 'https://b1.example.com'}).get_json()
    assert r['code'] == 200 and r['data']['value'] == 'https://b1.example.com', r
    bad = c.post('/api/wx-config/accounts', json={'acct_type': 'mp', 'name': '缺appid'}).get_json()
    assert bad['code'] == 400 and 'appid' in bad['message'], bad
    bad2 = c.post('/api/wx-config/accounts', json={'acct_type': 'xx', 'name': 'n', 'appid': 'a'}).get_json()
    assert bad2['code'] == 400, bad2
    h = c.get('/api/wx-config/health').get_json()
    assert h['data']['db'] == 'ok'


@test
def t18_api_auth_hook_blocks():
    fresh()
    c = _client()

    def deny():
        raise PermissionError('未登录')
    A.set_auth_hook(deny)
    blocked = False
    try:
        resp = c.get('/api/wx-config/effective')
        blocked = resp.status_code >= 400
    except Exception:
        blocked = True          # 异常直接冒出来也算拦住了
    try:
        assert blocked, '鉴权钩子没起作用'
    finally:
        A.set_auth_hook(None)
    assert c.get('/api/wx-config/effective').status_code == 200


@test
def t19_standby_failure_does_not_touch_active():
    """从演示里点出来的坑：对"备用号"模拟失败，绝不能动当前生效的号"""
    fresh()
    active = [a for a in C.list_accounts('mp') if a['is_active']][0]
    standby = [a for a in C.list_accounts('mp') if not a['is_active'] and not a['auto_disabled']][0]
    r = None
    for _ in range(3):
        r = C.mark_health(standby['id'], False, 'standby fail')
    assert r['auto_disabled'] is True and r.get('switched') is False, r
    assert r.get('note') == 'standby_disabled', r
    assert C.get_account(standby['id'])['auto_disabled'] == 1
    a = C.get_account(active['id'])          # 生效号必须纹丝不动
    assert a['is_active'] == 1 and a['auto_disabled'] == 0 and a['fail_count'] == 0, a
    assert C.resolve('mp')['account_id'] == active['id']


@test
def t20_no_repeated_failover_for_already_disabled():
    """已经自动停用的号继续失败：只更新体检，不刷日志、不抢生效位"""
    fresh()
    active = [a for a in C.list_accounts('mp') if a['is_active']][0]
    # [GUARD-20260913] 先造一个体检通过的备用号，否则按新规则不会切
    _sid = C.create_account('mp', '备用真号(重复降级测试)', 'wxrepeat0001', secret='secret' * 4)
    C.mark_health(_sid, True, '先体检通过')
    r = None
    for _ in range(3):
        r = C.mark_health(active['id'], False, 'boom')
    assert r['switched'] is True, r
    log_before = len(C.get_log(50))
    eff_before = C.resolve('mp')['account_id']
    r2 = None
    for _ in range(5):
        r2 = C.mark_health(active['id'], False, 'boom again')
    assert r2.get('note') == 'already_auto_disabled', r2
    assert 'fail_count' not in r2, r2
    assert len(C.get_log(50)) == log_before, '不该再产生切换日志'
    assert C.resolve('mp')['account_id'] == eff_before, '不该再抢生效位'
    assert C.get_account(active['id'])['health_status'] == 'fail'


@test
def t21_probe_success_recovers_standby_without_stealing_slot():
    """探活成功 → 解除停用标记（可以再当备用），但不抢当前生效位"""
    fresh()
    active = [a for a in C.list_accounts('mp') if a['is_active']][0]
    # [GUARD-20260913] 占位符备用号现在探活会被跳过（t39 验证这件事），所以造一个真备用号
    _sid = C.create_account('mp', '备用真号(探活恢复测试)', 'wxrecov0001', secret='secret' * 4)
    standby = C.get_account(_sid)
    for _ in range(3):
        C.mark_health(standby['id'], False, 'x')
    assert C.get_account(standby['id'])['auto_disabled'] == 1
    r = C.probe(standby['id'], prober=lambda acct: (True, '恢复了'))
    assert r.get('recovered') is True, r
    assert C.get_account(standby['id'])['auto_disabled'] == 0
    assert C.resolve('mp')['account_id'] == active['id'], '不该抢生效位'


@test
def t22_all_disabled_no_standby_logged_once():
    """最后一个号也挂了 → 记一次「无可用备用账号」，不是每次都记"""
    fresh()
    for a in C.list_accounts('mp'):
        pass
    accts = C.list_accounts('mp')
    # 逐个打死
    for a in accts:
        for _ in range(3):
            C.mark_health(a['id'], False, 'all down')
    log = C.get_log(50)
    no_standby = [l for l in log if '无可用的备用账号' in (l['reason'] or '')]
    assert len(no_standby) == 1, no_standby
    assert C.resolve('mp')['source'] == 'default'


# ------------------------------------------------------------
# 兼容本项目 database.py 的包装层（_PGConn / _PGCursor）
# 第 0 步在 106 上冒烟时踩到的坑，全部固化成用例，防止再犯
# ------------------------------------------------------------
class _FakeDictCursor(object):
    """模拟 _PGCursor：dict 行、**没有 description**、参数被转成字符串"""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return [dict(r) for r in self._rows]

    def fetchone(self):
        return dict(self._rows[0]) if self._rows else None

    @property
    def description(self):
        raise AttributeError('description')      # 关键：包装层就是没有这个属性


class _FakeConn(object):
    def __init__(self, rows=None):
        self._c = _FakeDictCursor(rows)

    def cursor(self):
        return self._c

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@test
def t23_conn_kind_detects_project_wrapper():
    """项目的 _PGConn 类名/模块名都不是 psycopg2，必须认得出来，否则会当成 SQLite 用错 SQL"""
    class _Inner(object):
        pass
    _Inner.__module__ = 'psycopg2.extensions'

    class _PGConn(object):
        def __init__(self):
            self._conn = _Inner()

    class _NotAPgConn(object):
        pass

    assert C._conn_kind(_PGConn()) == 'pg', '认不出项目的包装连接'
    assert C._conn_kind(_NotAPgConn()) == 'sqlite'
    assert C._conn_kind(sqlite3.connect(':memory:')) == 'sqlite'


@test
def t24_rows_work_without_description():
    """_PGCursor 没有 description 且返回 dict 行 —— 取行必须照样能工作"""
    conn = _FakeConn([{'id': 1, 'name': '伧置', 'appid': 'wxcabd4cbdb3096c4b'},
                      {'id': 2, 'name': '备用', 'appid': 'wxbackup'}])
    rows = C._rows(conn, 'pg', 'SELECT id, name, appid FROM wx_accounts WHERE acct_type=?', ('mp',))
    assert len(rows) == 2 and rows[0]['name'] == '伧置' and rows[1]['id'] == 2, rows
    assert C._row(conn, 'pg', 'SELECT 1')['id'] == 1
    # 传数字参数（项目包装层会把数字转成字符串）也不该出问题
    assert C._rows(conn, 'pg', 'SELECT * FROM t WHERE id=?', (1,))[0]['id'] == 1


@test
def t25_insert_returns_id_for_both_row_styles():
    """RETURNING id：dict 行（项目包装层）和元组行（原生 psycopg2）都要能取到 id"""
    assert C._insert(_FakeConn([{'id': 42}]), 'pg',
                     'INSERT INTO wx_accounts (name) VALUES (?)', ('x',)) == 42

    class _CurTuple(object):
        def execute(self, sql, params=None):
            return self

        def fetchone(self):
            return (43,)

    class _ConnTuple(object):
        def cursor(self):
            return _CurTuple()

    assert C._insert(_ConnTuple(), 'pg', 'INSERT INTO t (a) VALUES (?)', ('x',)) == 43


@test
def t26_check_schema_and_rebuild():
    """上线前自检：能查出表缺失/自增序列丢失；也能一键删表重建"""
    fresh()
    r = C.check_schema()
    assert r['ok'] is True and not r['warnings'], r
    for t in ('wx_accounts', 'wx_templates', 'wx_config_items', 'wx_switch_log'):
        assert r['tables'][t]['exists'], r
    C.drop_tables()
    bad = C.check_schema()
    assert bad['ok'] is False and len(bad['warnings']) >= 4, bad
    C.init_db()
    S.seed(reset=True)
    assert C.resolve('mp')['appid'] == 'wxcabd4cbdb3096c4b'
    assert C.check_schema()['ok'] is True


@test
def t27_config_cache_invalidated_on_write():
    """配置项有 30 秒缓存（生产走 pgbouncer 连接池，热路径不能每次都查库），但改完必须立刻生效"""
    fresh()
    assert C.get_config('h5_base') == 'https://locker.cqdyxl.com'
    C.set_config('h5_base', 'https://new.example.com')
    assert C.get_config('h5_base') == 'https://new.example.com', '改配置后没立刻生效'
    C.set_config('entry_mode', 'mp')
    assert C.get_config('entry_mode') == 'mp'


@test
def t28_auth_decorator_gate_blocks_and_allows():
    """第 1 步在 106 上踩到的漏洞：用 use_auth_decorator 注入后台真实鉴权时，
    没带 token 竟然能拿到全部数据（原因是 Flask 在 @bp.route 时就把视图收进闭包了）。
    这里用一个"跟 require_auth 同样行为"的假装饰器盯住它。"""
    fresh()
    from functools import wraps
    from flask import Flask, jsonify as _j

    def fake_require_auth(f):
        @wraps(f)
        def deco(*a, **kw):
            from flask import request
            if not (request.headers.get('Authorization') or '').startswith('Bearer ok-token'):
                return _j({'code': 401, 'data': None, 'message': '未登录，请先登录'}), 401
            return f(*a, **kw)
        return deco

    A.use_auth_decorator(fake_require_auth)
    try:
        app = Flask(__name__)
        app.register_blueprint(A.bp)
        app.testing = True
        c = app.test_client()

        r1 = c.get('/api/wx-config/snapshot')
        assert r1.status_code == 401, '没拦住！HTTP=%s' % r1.status_code
        assert (r1.get_json() or {}).get('code') == 401, r1.get_json()

        r2 = c.post('/api/wx-config/config', json={'key': 'h5_base', 'value': 'x'})
        assert r2.status_code == 401, '写接口也没拦住！HTTP=%s' % r2.status_code

        r3 = c.get('/api/wx-config/snapshot', headers={'Authorization': 'Bearer ok-token'})
        assert r3.status_code == 200, r3.status_code
        assert (r3.get_json() or {})['data']['effective']['mp']['appid'] == 'wxcabd4cbdb3096c4b'
    finally:
        A.use_auth_decorator(None)          # 清掉，别影响后面的用例
        try:
            A.set_prober(None)
        except Exception:
            pass


# ------------------------------------------------------------
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
    ok1, msg1 = C.switch_to('mp', new_id, reason='测试', probe_first=False)
    assert ok1, msg1
    assert _mp_effective()['appid'] == 'wxtest1234567890'
    assert ('探活' in msg1) or ('商户号' in msg1), '提醒信息没带上: %s' % msg1
    back = [r for r in C.list_accounts('mp') if r['appid'] == 'wxcabd4cbdb3096c4b'][0]
    ok2, msg2 = C.switch_to('mp', back['id'], reason='测试切回', probe_first=False)
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


@test
def t41_refused_toggle_does_not_log():
    """被防呆拒绝的"启用"操作，不能留下"启用（手动）"这种假日志"""
    fresh()
    c = _client()
    ph = [r for r in C.list_accounts('oa') if str(r['appid']).startswith('REPLACE_ME')][0]
    before = len(C.get_log(50))
    r = c.post('/api/wx-config/accounts/%d/toggle' % ph['id'], json={'active': True}).get_json()
    assert r['code'] == 400, r
    assert len(C.get_log(50)) == before, '被拒绝的操作留下了日志: %s' % C.get_log(3)
    # 真账号正常启用，还是要记一笔
    good = C.create_account('oa', '真公众号(日志测试)', 'wxoalog0001', secret='s' * 20)
    # [GUARD3-20260913] 启用现在会先探活；这个假号探活必然不过，所以测试里装个假探活器
    A.set_prober(lambda acct: (True, '模拟探活通过'))
    r2 = c.post('/api/wx-config/accounts/%d/toggle' % good, json={'active': True}).get_json()
    A.set_prober(None)
    assert r2['code'] == 200, r2
    assert len(C.get_log(50)) == before + 1, '正常操作没记日志: %s' % C.get_log(3)


@test
def t42_openid_prefix_follows_account():
    """openid 前缀要跟着"当前生效账号"走：换号后不用改代码（这是本次改造的目的）"""
    fresh()
    assert C.mp_openid_prefix() == 'ooTcRx', C.mp_openid_prefix()
    assert C.oa_openid_prefix() == 'oLhbm2', C.oa_openid_prefix()
    # 造一个"新小程序"，给它一个不同的前缀，切成生效
    nid = C.create_account('mp', '新号(前缀测试)', 'wxnewpfx0001', secret='s' * 20,
                           openid_prefix='oNEW01')
    ok1, msg1 = C.switch_to('mp', nid, reason='前缀测试', probe_first=False)
    assert ok1, msg1
    assert C.mp_openid_prefix() == 'oNEW01', '前缀没跟着换：%s' % C.mp_openid_prefix()
    # 公众号的前缀不受影响
    assert C.oa_openid_prefix() == 'oLhbm2'
    # 切回去，前缀也要跟着回去
    back = [a for a in C.list_accounts('mp') if a['appid'] == 'wxcabd4cbdb3096c4b'][0]
    ok2, msg2 = C.switch_to('mp', back['id'], reason='前缀测试切回', probe_first=False)
    assert ok2, msg2
    assert C.mp_openid_prefix() == 'ooTcRx', C.mp_openid_prefix()
    print('      （切换后前缀变化：ooTcRx -> oNEW01 -> ooTcRx）')


@test
def t43_openid_prefix_empty_falls_back():
    """前缀字段是空的（老库没这列 / 没配）→ 回落默认值，行为与改造前完全一致"""
    fresh()
    act = [a for a in C.list_accounts('mp') if a['is_active']][0]
    C.update_account(act['id'], openid_prefix='')
    C.clear_cache()
    assert C.mp_openid_prefix() == 'ooTcRx', '空值时应回落 ooTcRx，实得 %s' % C.mp_openid_prefix()
    oact = [a for a in C.list_accounts('oa') if a['is_active']][0]
    C.update_account(oact['id'], openid_prefix='')
    C.clear_cache()
    assert C.oa_openid_prefix() == 'oLhbm2', '空值时应回落 oLhbm2，实得 %s' % C.oa_openid_prefix()
    # 库里读不到时（bind 一个会炸的连接）也必须回落
    C.bind(lambda: (_ for _ in ()).throw(RuntimeError('模拟库挂了')))
    C.clear_cache()
    assert C.mp_openid_prefix() == 'ooTcRx', C.mp_openid_prefix()
    assert C.oa_openid_prefix() == 'oLhbm2', C.oa_openid_prefix()


@test
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


# ------------------------------------------------------------
def main():
    backend = ('PostgreSQL: ' + _PG_DSN) if _PG_DSN else ('SQLite: ' + _DB)
    print('  测试后端 -> %s' % backend)
    passed, failed, skipped = 0, [], []
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
            print('  PASS  %s' % name)
            passed += 1
        except RuntimeError as e:
            if str(e).startswith('SKIP:'):
                print('  SKIP  %s -> %s' % (name, str(e)[5:].strip()))
                skipped.append(name)
            else:
                failed.append((name, __import__('traceback').format_exc()))
                print('  FAIL  %s -> %s' % (name, e))
        except Exception as e:
            import traceback
            print('  FAIL  %s -> %s: %s' % (name, type(e).__name__, e))
            failed.append((name, traceback.format_exc()))
    print('-' * 62)
    print('  通过 %d / %d%s' % (passed, len(TESTS), ('，跳过 %d' % len(skipped)) if skipped else ''))
    if failed:
        for n, tb in failed:
            print('\n' + '=' * 62 + '\n' + n + '\n' + tb)
        return 1
    print('  全部通过 ✔')
    return 0


if __name__ == '__main__':
    sys.exit(main())
