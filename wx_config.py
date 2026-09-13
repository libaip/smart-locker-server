# -*- coding: utf-8 -*-
"""
微信账号配置中心（本地开发版）
=========================================================
目标：把「小程序 / 公众号 / 域名 / 消息模板」从代码里搬进数据库，
      做到像切换支付商户号一样：一键切换、实时生效、不重启、不发版；
      账号被封时能立刻切到备用号。

设计照抄 payment_channels 的成功经验：
  1) 配置在库里，业务每次「实时读」→ 改完立刻生效
  2) 同类账号同一时刻只有一个 is_active=1（当前生效），其余是待命备用
  3) auto_disabled = 连续失败自动停用 + 自动切备用（= 支付渠道的 auto_disabled）
  4) 读库失败 / 库为空 → 回落 DEFAULTS（= config.py 现值），保证业务永远不炸
  5) 驱动无关：本地 SQLite 测试，生产 PostgreSQL 复用同一套代码

本地怎么用：
    import wx_config as C
    C.bind(lambda: sqlite3.connect('demo.db'))   # 生产改成 C.bind(helpers.get_db)
    C.init_db()
    cred = C.resolve('mp')       # {'appid':..,'secret':..,'source':'db'|'default'}
    C.get_config('h5_base')
    C.get_template('subscribe_deposit', 'mp')
    C.record_result('mp', ok=False, detail='errcode 43004')   # 连 3 次失败自动切备用
"""

import json
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime

# ============================================================
# 兜底配置（= config.py 现值）。数据库读不到时业务照常跑，绝不因为配置中心炸掉
# ============================================================
DEFAULTS = {
    'mp': {
        'name': '伧置(兜底config.py)',
        'appid': 'wxcabd4cbdb3096c4b',
        'secret': 'f8d9d68772401f4fdda4a2d2d6143988',
        'token': 'smartlocker2024',
        'aes_key': '',
        'subject': '重庆科莱维科技有限公司',
    },
    'oa': {
        'name': '智能寄存柜(兜底config.py)',
        'appid': 'wxd85204d0ec930d46',
        'secret': '552e27fa9a260a6640bf6983bd3470f5',
        'token': 'smartlocker2024',
        'aes_key': '',
        'subject': '重庆科莱维科技有限公司',
    },
    'config': {
        'h5_base': 'https://locker.cqdyxl.com',
        'h5_backup_domains': '',
        'oauth_path': '/api/wx/oauth',
        'pay_notify_url': 'https://locker.cqdyxl.com/api/pay/notify',
        'refund_notify_url': 'https://locker.cqdyxl.com/api/refund/notify',
        'complaint_notify_url': 'https://locker.cqdyxl.com/api/admin_v2/wechat-complaint/notify',
        'mp_entry_path': 'pages/subscribe/subscribe',
        'mp_home_path': 'pages/index/index',
        'mp_mine_path': 'pages/mine/mine',
        'entry_mode': 'h5',
        'oa_subscribe_enabled': 'false',
    },
}

CACHE_TTL = 300          # 生效账号缓存秒数（支付渠道那套也是实时读，这里加个短缓存省查询）
CONFIG_CACHE_TTL = 30    # 配置项缓存秒数。生产走 pgbouncer 连接池，热路径不要每次请求都查库；
                         # 改配置会主动清缓存，所以人工改动是立刻生效的
FAIL_THRESHOLD = 3       # 连续失败几次 → 自动停用并切备用
ACCT_TYPES = ('mp', 'oa')     # mp=小程序(订阅消息)  oa=公众号(模板消息)
CHANNELS = ('mp', 'oa')       # 模板通道：mp=订阅通知  oa=模板消息
ID_TABLES = ('wx_accounts', 'wx_templates', 'wx_switch_log')   # 这三张表有自增 id

# 模板业务码 → 人话（给后台页面用）
BIZ_LABELS = {
    'subscribe_deposit': '小程序·寄存成功订阅',
    'subscribe_refund': '小程序·退款成功订阅',
    'subscribe_general': '小程序·押金退还订阅',
    'oa_deposit_ok': '公众号·存储柜寄存成功通知',
    'oa_deposit_end': '公众号·寄存结束通知',
    'oa_refund_ok': '公众号·退款成功通知',
    'oa_withdraw_ok': '公众号·提现成功通知',
}

# ============================================================
# 数据库绑定（SQLite / PostgreSQL 都能跑）
# ============================================================
_FACTORY = None
_CACHE = {}          # acct_type -> (时间戳, 账号dict|None)
_CONFIG_CACHE = {'ts': 0, 'vals': None}
_TPL_CACHE = {}      # biz|channel|default -> (时间戳, 模板ID)


def bind(factory):
    """绑定取连接的函数。本地: lambda: sqlite3.connect('demo.db')；生产: helpers.get_db"""
    global _FACTORY
    _FACTORY = factory
    clear_cache()


def _conn_kind(conn):
    """判断底层是 PostgreSQL 还是 SQLite。

    注意：本项目 database.py 不是直接用 psycopg2，而是包了一层 _PGConn/_PGCursor
    （类名 _PGConn、模块名 database），所以不能只看 type(conn).__module__。
    """
    cls = type(conn)
    mod = (getattr(cls, '__module__', '') or '')
    name = (getattr(cls, '__name__', '') or '')
    if 'psycopg' in mod or name in ('_PGConn', '_PGConnWrapper'):
        return 'pg'
    inner = getattr(conn, '_conn', None)          # 项目自包装的连接：里面才是 psycopg2 连接
    if inner is not None and 'psycopg' in (getattr(type(inner), '__module__', '') or ''):
        return 'pg'
    return 'sqlite'


@contextmanager
def _conn():
    if _FACTORY is None:
        raise RuntimeError('wx_config 未绑定数据库：请先调用 wx_config.bind(取连接函数)')
    conn = _FACTORY()
    kind = _conn_kind(conn)
    try:
        yield conn, kind
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _sql(sql, kind):
    """本地用 ? 占位，生产 psycopg2 用 %s —— 同一套 SQL 两边通用"""
    return sql.replace('?', '%s') if kind == 'pg' else sql


def _fetch_dicts(cur):
    """把结果集读成 list[dict]，兼容三种情况：
       · sqlite3 / 原生 psycopg2：tuple 行 + 有 description
       · 本项目 database.py 的 _PGCursor：**dict 行、没有 description**（第 0 步冒烟踩到的坑）
       · 其它：tuple 行但没 description（拿不到列名，只能按 _0/_1 编号返回）
    """
    desc = getattr(cur, 'description', None)
    try:
        raw = cur.fetchall()
    except Exception:
        return []
    out = []
    for r in raw or []:
        if isinstance(r, dict):
            out.append({k: r[k] for k in r.keys()})
        elif desc is not None:
            out.append(dict(zip([d[0] for d in desc], r)))
        elif hasattr(r, 'keys'):
            out.append({k: r[k] for k in r.keys()})
        else:
            out.append({'_%d' % i: v for i, v in enumerate(r)})
    return out


def _rows(conn, kind, sql, params=()):
    """统一取行：不依赖 row_factory / description，返回 list[dict]"""
    cur = conn.cursor()
    cur.execute(_sql(sql, kind), params)
    return _fetch_dicts(cur)


def _row(conn, kind, sql, params=()):
    r = _rows(conn, kind, sql, params)
    return r[0] if r else None


def _exec(conn, kind, sql, params=()):
    cur = conn.cursor()
    cur.execute(_sql(sql, kind), params)
    return cur


def _insert(conn, kind, sql, params=()):
    """插入并返回新 id：SQLite 用 lastrowid，PostgreSQL 用 RETURNING"""
    cur = conn.cursor()
    if kind == 'pg':
        cur.execute(_sql(sql, kind) + ' RETURNING id', params)
        r = cur.fetchone()
        if r is None:
            return None
        if isinstance(r, dict):
            return r.get('id')
        try:
            return r['id']
        except Exception:
            return r[0]
    cur.execute(_sql(sql, kind), params)
    return getattr(cur, 'lastrowid', None)


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def clear_cache(acct_type=None):
    global _CONFIG_CACHE
    _CONFIG_CACHE = {'ts': 0, 'vals': None}
    _TPL_CACHE.clear()
    if acct_type:
        _CACHE.pop(acct_type, None)
    else:
        _CACHE.clear()


# ============================================================
# 建表
# ============================================================
_ACCOUNT_COLS = [
    'acct_type', 'name', 'appid', 'secret', 'token', 'aes_key', 'subject',
    'mch_relation', 'is_active', 'priority', 'auto_disabled', 'fail_count',
    'health_status', 'health_detail', 'last_check_at', 'last_used_at',
    'note', 'created_at', 'updated_at',
]


def ddl(kind):
    pk = 'SERIAL PRIMARY KEY' if kind == 'pg' else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    return [
        """CREATE TABLE IF NOT EXISTS wx_accounts (
            id %s,
            acct_type VARCHAR(8) NOT NULL,
            name VARCHAR(64) NOT NULL,
            appid VARCHAR(64) NOT NULL,
            secret VARCHAR(128) DEFAULT '',
            token VARCHAR(64) DEFAULT '',
            aes_key VARCHAR(64) DEFAULT '',
            subject VARCHAR(128) DEFAULT '',
            mch_relation VARCHAR(16) DEFAULT 'none',
            is_active INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 100,
            auto_disabled INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            health_status VARCHAR(16) DEFAULT 'unknown',
            health_detail VARCHAR(255) DEFAULT '',
            last_check_at VARCHAR(32) DEFAULT '',
            last_used_at VARCHAR(32) DEFAULT '',
            note VARCHAR(255) DEFAULT '',
            created_at VARCHAR(32) DEFAULT '',
            updated_at VARCHAR(32) DEFAULT ''
        )""" % pk,
        """CREATE TABLE IF NOT EXISTS wx_templates (
            id %s,
            biz VARCHAR(48) NOT NULL,
            channel VARCHAR(8) NOT NULL,
            account_id INTEGER DEFAULT 0,
            template_id VARCHAR(96) NOT NULL,
            page VARCHAR(128) DEFAULT '',
            fields TEXT DEFAULT '{}',
            is_active INTEGER DEFAULT 1,
            note VARCHAR(255) DEFAULT '',
            updated_at VARCHAR(32) DEFAULT '',
            UNIQUE (biz, channel, account_id)
        )""" % pk,
        """CREATE TABLE IF NOT EXISTS wx_config_items (
            cfg_key VARCHAR(64) PRIMARY KEY,
            cfg_value TEXT DEFAULT '',
            group_name VARCHAR(32) DEFAULT '',
            note VARCHAR(255) DEFAULT '',
            updated_at VARCHAR(32) DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS wx_switch_log (
            id %s,
            acct_type VARCHAR(8),
            from_name VARCHAR(64),
            to_name VARCHAR(64),
            reason VARCHAR(255),
            operator VARCHAR(32),
            created_at VARCHAR(32)
        )""" % pk,
        """CREATE INDEX IF NOT EXISTS idx_wx_accounts_type ON wx_accounts (acct_type, is_active)""",
    ]


def init_db(verbose=False):
    """建表（幂等，可重复调用）"""
    with _conn() as (conn, kind):
        for st in ddl(kind):
            try:
                _exec(conn, kind, st)
            except Exception as e:
                if verbose:
                    print('  [ddl] %s -> %s' % (type(e).__name__, e))
                # 已存在/索引重名之类的，忽略（SQLite 老版本不支持 IF NOT EXISTS 索引才会走到这里）
                continue
    clear_cache()
    return True


# ============================================================
# 账号池
# ============================================================
def list_accounts(acct_type=None):
    sql = 'SELECT * FROM wx_accounts'
    params = ()
    if acct_type:
        sql += ' WHERE acct_type=?'
        params = (acct_type,)
    sql += ' ORDER BY acct_type, priority, id'
    with _conn() as (conn, kind):
        return _rows(conn, kind, sql, params)


def get_account(account_id):
    with _conn() as (conn, kind):
        return _row(conn, kind, 'SELECT * FROM wx_accounts WHERE id=?', (account_id,))


def create_account(acct_type, name, appid, secret='', **kw):
    if acct_type not in ACCT_TYPES:
        raise ValueError('acct_type 只能是 %s' % (ACCT_TYPES,))
    data = {
        'acct_type': acct_type, 'name': name, 'appid': appid, 'secret': secret,
        'token': kw.get('token', ''), 'aes_key': kw.get('aes_key', ''),
        'subject': kw.get('subject', ''), 'mch_relation': kw.get('mch_relation', 'none'),
        'is_active': 1 if kw.get('is_active') else 0,
        'priority': int(kw.get('priority', 100)),
        'auto_disabled': 0, 'fail_count': 0,
        'health_status': 'unknown', 'health_detail': '',
        'last_check_at': '', 'last_used_at': '',
        'note': kw.get('note', ''),
        'created_at': _now(), 'updated_at': _now(),
    }
    with _conn() as (conn, kind):
        cols = ', '.join(_ACCOUNT_COLS)
        ph = ', '.join(['?'] * len(_ACCOUNT_COLS))
        new_id = _insert(conn, kind,
                         'INSERT INTO wx_accounts (%s) VALUES (%s)' % (cols, ph),
                         tuple(data[c] for c in _ACCOUNT_COLS))
        if data['is_active']:
            _exec(conn, kind, 'UPDATE wx_accounts SET is_active=0 WHERE acct_type=? AND id<>?',
                  (acct_type, new_id))
    clear_cache(acct_type)
    return new_id


def update_account(account_id, **kw):
    allowed = {'name', 'appid', 'secret', 'token', 'aes_key', 'subject',
               'mch_relation', 'priority', 'note'}
    if 'is_active' in kw:                 # 生效开关走 set_active，保证"同类只有一个生效"
        set_active(account_id, bool(kw.pop('is_active')))
    sets, params = [], []
    for k, v in kw.items():
        if k in allowed:
            sets.append('%s=?' % k)
            params.append(v)
    if not sets:
        return False
    sets.append('updated_at=?')
    params.append(_now())
    params.append(account_id)
    with _conn() as (conn, kind):
        _exec(conn, kind, 'UPDATE wx_accounts SET %s WHERE id=?' % ', '.join(sets), tuple(params))
    clear_cache()
    return True


def delete_account(account_id):
    """删除账号。当前生效的不给删，防止手滑把线上唯一的号删了"""
    row = get_account(account_id)
    if not row:
        return False, '账号不存在'
    if row.get('is_active'):
        return False, '该账号当前生效中，请先切到别的账号再删除'
    with _conn() as (conn, kind):
        _exec(conn, kind, 'DELETE FROM wx_accounts WHERE id=?', (account_id,))
    clear_cache(row.get('acct_type'))
    return True, '已删除'


def set_active(account_id, active=True):
    """启用/停用一个账号（启用时会自动把同类其它账号停掉，保证只有一个生效）"""
    row = get_account(account_id)
    if not row:
        return False, '账号不存在'
    at = row['acct_type']
    with _conn() as (conn, kind):
        if active:
            _exec(conn, kind, 'UPDATE wx_accounts SET is_active=0 WHERE acct_type=?', (at,))
            _exec(conn, kind, 'UPDATE wx_accounts SET is_active=1, auto_disabled=0, fail_count=0, updated_at=? WHERE id=?',
                  (_now(), account_id))
        else:
            _exec(conn, kind, 'UPDATE wx_accounts SET is_active=0, updated_at=? WHERE id=?', (_now(), account_id))
    clear_cache(at)
    return True, ('已启用' if active else '已停用')


def switch_to(acct_type, account_id, operator='local-demo', reason='手动切换'):
    """一键切换：目标账号设为生效，同类其它账号全部停用，并写切换日志"""
    if acct_type not in ACCT_TYPES:
        return False, 'acct_type 只能是 %s' % (ACCT_TYPES,)
    row = get_account(account_id)
    if not row:
        return False, '账号不存在'
    if row['acct_type'] != acct_type:
        return False, '账号类型不匹配'
    old = get_effective_account(acct_type, use_cache=False)
    with _conn() as (conn, kind):
        _exec(conn, kind, 'UPDATE wx_accounts SET is_active=0 WHERE acct_type=?', (acct_type,))
        _exec(conn, kind, """UPDATE wx_accounts
                             SET is_active=1, auto_disabled=0, fail_count=0,
                                 health_status='unknown', health_detail='', updated_at=?
                             WHERE id=?""", (_now(), account_id))
        _log_switch(conn, kind, acct_type,
                    old['name'] if old else '', row['name'], reason, operator)
    clear_cache(acct_type)
    return True, '已切换到 %s' % row['name']


def _log_switch(conn, kind, acct_type, from_name, to_name, reason, operator):
    _exec(conn, kind, """INSERT INTO wx_switch_log (acct_type, from_name, to_name, reason, operator, created_at)
                         VALUES (?,?,?,?,?,?)""",
          (acct_type, from_name or '', to_name or '', (reason or '')[:255], operator or '', _now()))


def get_log(limit=50):
    with _conn() as (conn, kind):
        return _rows(conn, kind, 'SELECT * FROM wx_switch_log ORDER BY id DESC LIMIT ?', (int(limit),))


def log_switch(acct_type, from_name, to_name, reason='', operator='manual'):
    with _conn() as (conn, kind):
        _log_switch(conn, kind, acct_type, from_name, to_name, reason, operator)


# ============================================================
# 生效账号 + 凭据解析（业务唯一入口）
# ============================================================
def get_effective_account(acct_type, use_cache=True):
    """当前生效账号。同一类只有一个，按 priority 小的优先"""
    now = time.time()
    if use_cache:
        hit = _CACHE.get(acct_type)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    row = None
    try:
        with _conn() as (conn, kind):
            row = _row(conn, kind, """SELECT * FROM wx_accounts
                                      WHERE acct_type=? AND is_active=1 AND COALESCE(auto_disabled,0)=0
                                      ORDER BY priority, id LIMIT 1""", (acct_type,))
    except Exception:
        row = None
    _CACHE[acct_type] = (now, row)
    return row


def resolve(acct_type):
    """业务取凭据：库里没有就回落 DEFAULTS。永远不抛异常"""
    row = get_effective_account(acct_type)
    if row and row.get('appid'):
        return {
            'appid': row['appid'], 'secret': row.get('secret') or '',
            'token': row.get('token') or '', 'aes_key': row.get('aes_key') or '',
            'name': row.get('name') or '', 'account_id': row['id'],
            'subject': row.get('subject') or '', 'source': 'db',
            'mch_relation': row.get('mch_relation') or 'none',
        }
    d = dict(DEFAULTS.get(acct_type) or {})
    d.update({'account_id': 0, 'source': 'default'})
    return d


def resolve_all():
    return {t: resolve(t) for t in ACCT_TYPES}


# ---- 给业务用的"无引号"取值函数 -------------------------------------------------
# 为什么要单独做这四个：业务代码里很多地方是 f-string，形如 f'...appid={WX_APP_ID}&...'
# 如果替换成 appid('oa')（带引号）就会把 f-string 的引号搞乱（实测踩过：app.py 语法错误）。
# 这四个包装函数调用时**不带任何引号**，可以安全地塞进任何引号环境里。
def mp_appid():
    return resolve('mp').get('appid') or ''


def mp_secret():
    return resolve('mp').get('secret') or ''


def oa_appid():
    return resolve('oa').get('appid') or ''


def oa_secret():
    return resolve('oa').get('secret') or ''


def touch_used(acct_type):
    """记录一次使用（便于后台看"最后使用时间"）"""
    row = get_effective_account(acct_type)
    if not row:
        return
    try:
        with _conn() as (conn, kind):
            _exec(conn, kind, 'UPDATE wx_accounts SET last_used_at=? WHERE id=?', (_now(), row['id']))
    except Exception:
        pass


# ============================================================
# 健康 / 自动降级（= 支付渠道的 auto_disabled）
# ============================================================
def mark_health(account_id, ok, detail=''):
    """回报某账号一次调用结果。连续失败达阈值 → 自动停用；只有"当前生效"的号被停用时才切备用。

    三条规矩（都是从演示里点出来的坑）：
      1) 已经 auto_disabled 的号再失败，只更新体检信息，不重复触发降级、不抢生效位
      2) 备用号失败被停用时，绝不动"当前生效"的账号
      3) 失败过的号后来探活成功 → 解除停用标记，可以重新当备用（但不会自己去抢生效位）
    """
    out = {'account_id': account_id, 'ok': bool(ok), 'switched': False}
    row = get_account(account_id)
    if not row:
        out['error'] = '账号不存在'
        return out
    at = row['acct_type']
    was_active = bool(row.get('is_active'))
    already = bool(row.get('auto_disabled'))
    detail = str(detail)[:250]
    with _conn() as (conn, kind):
        if ok:
            _exec(conn, kind, """UPDATE wx_accounts
                                 SET fail_count=0, health_status='ok', health_detail=?,
                                     last_check_at=?, auto_disabled=0, updated_at=?
                                 WHERE id=?""", (detail, _now(), _now(), account_id))
            out['health'] = 'ok'
            if already:
                out['recovered'] = True
        else:
            if already:
                _exec(conn, kind, """UPDATE wx_accounts
                                     SET health_status='fail', health_detail=?, last_check_at=?
                                     WHERE id=?""", (detail, _now(), account_id))
                out['note'] = 'already_auto_disabled'
            else:
                fc = int(row.get('fail_count') or 0) + 1
                _exec(conn, kind, """UPDATE wx_accounts
                                     SET fail_count=?, health_status='fail', health_detail=?, last_check_at=?
                                     WHERE id=?""", (fc, detail, _now(), account_id))
                out['fail_count'] = fc
                if fc >= FAIL_THRESHOLD:
                    _exec(conn, kind, """UPDATE wx_accounts
                                         SET auto_disabled=1, is_active=0, updated_at=?
                                         WHERE id=?""", (_now(), account_id))
                    out['auto_disabled'] = True
                    if not was_active:
                        # 挂掉的是备用号：当前生效的号不受任何影响
                        _log_switch(conn, kind, at, row['name'], '',
                                    '备用号自动停用（当前生效账号不受影响）: %s' % detail, 'auto')
                        out['note'] = 'standby_disabled'
                    else:
                        nxt = _row(conn, kind, """SELECT * FROM wx_accounts
                                                  WHERE acct_type=? AND id<>? AND COALESCE(auto_disabled,0)=0
                                                  ORDER BY is_active DESC, priority, id LIMIT 1""",
                                   (at, account_id))
                        if nxt:
                            _exec(conn, kind, """UPDATE wx_accounts
                                                 SET is_active=1, auto_disabled=0, fail_count=0,
                                                     health_status='unknown', health_detail='', updated_at=?
                                                 WHERE id=?""", (_now(), nxt['id']))
                            _log_switch(conn, kind, at, row['name'], nxt['name'],
                                        '自动降级(%d次失败): %s' % (fc, detail), 'auto')
                            out.update({'switched': True, 'to': nxt['name'], 'to_id': nxt['id']})
                        else:
                            _log_switch(conn, kind, at, row['name'], '',
                                        '自动停用但无备用可用: %s' % detail, 'auto')
                            out['reason'] = 'no_standby'
    clear_cache(at)
    return out


def record_result(acct_type, ok, detail=''):
    """业务调用后回报结果：内部记在"当前生效"账号上，失败自动降级"""
    row = get_effective_account(acct_type, use_cache=False)
    if not row:
        return {'acct_type': acct_type, 'ok': bool(ok), 'reason': 'no_active_account'}
    return mark_health(row['id'], ok, detail)


def default_prober(account, timeout=8):
    """真探活：取 access_token。能取到 = 密钥有效、账号没被完全封。
    注意：被限制的号（比如只能收不能发）取 token 仍可能成功，这只是第一道体检。"""
    url = ('https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s'
           % (urllib.parse.quote(account.get('appid') or ''), urllib.parse.quote(account.get('secret') or '')))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return False, '网络异常: %s' % e
    if d.get('access_token'):
        return True, 'access_token 正常'
    return False, 'errcode=%s errmsg=%s' % (d.get('errcode'), d.get('errmsg'))


def probe(account_id, prober=None):
    """探测一个账号：探活 + 更新健康状态（失败会走自动降级逻辑）"""
    row = get_account(account_id)
    if not row:
        return {'ok': False, 'detail': '账号不存在'}
    fn = prober or default_prober
    try:
        ok, detail = fn(row)
    except Exception as e:
        ok, detail = False, '探测异常: %s' % e
    res = mark_health(account_id, ok, detail)
    res.update({'detail': detail, 'name': row['name']})
    return res


# ============================================================
# 可切换配置项（域名等）
# ============================================================
def all_config(use_cache=True):
    """合并「默认值 + 库里覆盖」的配置。加 30 秒缓存，避免热路径反复查库（生产走 pgbouncer 连接池）"""
    global _CONFIG_CACHE
    now = time.time()
    if use_cache and _CONFIG_CACHE['vals'] is not None and now - _CONFIG_CACHE['ts'] < CONFIG_CACHE_TTL:
        return dict(_CONFIG_CACHE['vals'])
    vals = dict(DEFAULTS.get('config') or {})
    try:
        with _conn() as (conn, kind):
            for r in _rows(conn, kind, 'SELECT cfg_key, cfg_value FROM wx_config_items'):
                vals[r['cfg_key']] = r['cfg_value']
    except Exception:
        pass
    _CONFIG_CACHE = {'ts': now, 'vals': dict(vals)}
    return vals


def get_config(key=None, default=None):
    vals = all_config()
    if key is None:
        return vals
    return vals.get(key, default)


def list_config_items():
    """带分组/备注的配置项（后台页面用）"""
    db = {}
    try:
        with _conn() as (conn, kind):
            for r in _rows(conn, kind, 'SELECT * FROM wx_config_items'):
                db[r['cfg_key']] = r
    except Exception:
        pass
    out = []
    for k, v in (DEFAULTS.get('config') or {}).items():
        row = db.get(k, {})
        out.append({'cfg_key': k, 'cfg_value': row.get('cfg_value', v),
                    'default_value': v, 'group_name': row.get('group_name', ''),
                    'note': row.get('note', ''), 'from_db': bool(row),
                    'updated_at': row.get('updated_at', '')})
    for k, row in db.items():
        if k not in (DEFAULTS.get('config') or {}):
            out.append({'cfg_key': k, 'cfg_value': row.get('cfg_value', ''),
                        'default_value': '', 'group_name': row.get('group_name', ''),
                        'note': row.get('note', ''), 'from_db': True,
                        'updated_at': row.get('updated_at', '')})
    return sorted(out, key=lambda x: x['cfg_key'])


def set_config(key, value, group_name='', note=''):
    with _conn() as (conn, kind):
        _exec(conn, kind, """INSERT INTO wx_config_items (cfg_key, cfg_value, group_name, note, updated_at)
                             VALUES (?,?,?,?,?)
                             ON CONFLICT(cfg_key) DO UPDATE SET
                                 cfg_value=excluded.cfg_value,
                                 group_name=excluded.group_name,
                                 note=excluded.note,
                                 updated_at=excluded.updated_at""",
              (key, value, group_name, note, _now()))
    clear_cache()          # 配置改完立刻生效（清掉 30 秒缓存）
    return True


def check_schema():
    """上线前自检：4 张表在不在、自增 id 有没有序列。

    为什么要查这个：本项目的 database.py 会用 _PGCursor 把 SQL 里的 "AUTOINCREMENT"
    直接删掉。如果建表时被判断成了 SQLite 语法（历史坑），PG 里就会建出
    「id integer PRIMARY KEY 但没有序列」的表 —— 建表不报错，插数据才炸。
    """
    out = {'kind': None, 'tables': {}, 'warnings': [], 'ok': True}
    all_tables = ('wx_accounts', 'wx_templates', 'wx_config_items', 'wx_switch_log')
    try:
        with _conn() as (conn, kind):
            out['kind'] = kind
            for t in all_tables:
                if kind == 'pg':
                    ex = _row(conn, kind, "SELECT 1 AS ok FROM information_schema.tables "
                                          "WHERE table_schema='public' AND table_name=?", (t,))
                    info = {'exists': bool(ex), 'auto_id': True, 'checked': t in ID_TABLES}
                    if ex and t in ID_TABLES:
                        cd = _row(conn, kind, "SELECT column_default AS d FROM information_schema.columns "
                                              "WHERE table_name=? AND column_name='id'", (t,))
                        has = bool(cd and cd.get('d') and 'nextval' in str(cd['d']))
                        info['auto_id'] = has
                        if not has:
                            out['ok'] = False
                            out['warnings'].append(
                                '%s.id 没有自增序列（多半是用 SQLite 语法建出来的）：'
                                '建表不报错但插数据会失败，需要 DROP 后重建' % t)
                else:
                    ex = _row(conn, kind, "SELECT 1 AS ok FROM sqlite_master "
                                          "WHERE type='table' AND name=?", (t,))
                    info = {'exists': bool(ex), 'auto_id': bool(ex), 'checked': t in ID_TABLES}
                info['exists'] = bool(info['exists'])
                out['tables'][t] = info
                if not info['exists']:
                    out['ok'] = False
                    out['warnings'].append('%s 不存在（需要 init_db 建表）' % t)
    except Exception as e:
        out['ok'] = False
        out['warnings'].append('自检失败: %s' % e)
    return out


def drop_tables():
    """删掉配置中心的 4 张表（只在自检发现表建错、需要重建时用；不影响其它表）"""
    with _conn() as (conn, kind):
        for t in ('wx_switch_log', 'wx_templates', 'wx_config_items', 'wx_accounts'):
            _exec(conn, kind, 'DROP TABLE IF EXISTS %s' % t)
    clear_cache()
    return True


def h5_url(path='', base=None):
    """拼 H5 地址：换域名时只改配置，不用改代码"""
    b = (base or get_config('h5_base') or '').rstrip('/')
    if not path:
        return b
    return b + ('' if path.startswith('/') else '/') + path


# ---- 给业务用的"无引号"域名函数（f-string 里要用，不能带引号） --------------------
def h5_base():
    return (get_config('h5_base') or (DEFAULTS.get('config') or {}).get('h5_base') or '').rstrip('/')


def h5_store():
    return h5_base() + '/store'


def oauth_callback():
    p = get_config('oauth_path') or '/api/wx/oauth'
    return h5_base() + ('' if str(p).startswith('/') else '/') + str(p)


def ws_base():
    """把 https 换成 ws（柜机/页面用的 WebSocket 地址）"""
    return h5_base().replace('https://', 'ws://').replace('http://', 'ws://')


def pay_notify_url():
    return get_config('pay_notify_url') or (h5_base() + '/api/pay/notify')


def refund_notify_url():
    return get_config('refund_notify_url') or (h5_base() + '/api/refund/notify')


# ============================================================
# 模板（订阅消息 / 模板消息）
# ============================================================
def list_templates(channel=None, active_only=False):
    """列出模板。数据库读不到（比如生产库还没建表）时返回空列表，绝不抛异常。"""
    sql = 'SELECT * FROM wx_templates'
    where, params = [], []
    if channel:
        where.append('channel=?')
        params.append(channel)
    if active_only:
        where.append('is_active=1')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY channel, biz, account_id'
    try:
        with _conn() as (conn, kind):
            rows = _rows(conn, kind, sql, tuple(params))
    except Exception:
        return []
    for r in rows:
        r['biz_label'] = BIZ_LABELS.get(r['biz'], r['biz'])
    return rows


def get_template(biz, channel=None, account_id=None):
    """取模板：优先账号专属，其次通用（account_id=0）。读不到返回 None，绝不抛异常。"""
    try:
        with _conn() as (conn, kind):
            rows = _rows(conn, kind, 'SELECT * FROM wx_templates WHERE biz=? AND is_active=1', (biz,))
    except Exception:
        return None
    if not rows:
        return None
    if channel:
        rows = [r for r in rows if r['channel'] == channel] or rows
    if account_id:
        hit = [r for r in rows if r.get('account_id') == account_id]
        if hit:
            return hit[0]
    gen = [r for r in rows if not r.get('account_id')]
    return gen[0] if gen else rows[0]


TPL_CACHE_TTL = 30      # 模板ID缓存秒数（业务每发一条消息都要取，避免每次都查库）


def template_id(biz, channel='mp', default=''):
    """【第2步·业务统一入口】取模板 ID。

    库里配了就用库里的；库是空的 / 读不到 / 出错 → **返回 default**（= 代码里原来的写死值）。
    这条兜底是关键：生产库还没建表时，行为与改造前完全一致，绝不会把通知打断。
    """
    now = time.time()
    key = '%s|%s|%s' % (biz, channel, default)
    hit = _TPL_CACHE.get(key)
    if hit and now - hit[0] < TPL_CACHE_TTL:
        return hit[1]
    val = default
    try:
        t = get_template(biz, channel)
        if t and t.get('template_id'):
            val = t['template_id']
    except Exception:
        val = default
    _TPL_CACHE[key] = (now, val)
    return val


def get_templates_map(channel, account_id=None):
    """一次性取一个通道的全部模板，业务批量用"""
    out = {}
    for r in list_templates(channel=channel, active_only=True):
        if account_id and r.get('account_id') and r['account_id'] != account_id:
            continue
        prev = out.get(r['biz'])
        if prev is None or (r.get('account_id') and not prev.get('account_id')):
            out[r['biz']] = r
    return out


def set_template(biz, channel, template_id, page='', fields=None, account_id=0, note=''):
    if channel not in CHANNELS:
        raise ValueError('channel 只能是 %s' % (CHANNELS,))
    fj = json.dumps(fields or {}, ensure_ascii=False)
    with _conn() as (conn, kind):
        _exec(conn, kind, """INSERT INTO wx_templates (biz, channel, account_id, template_id, page, fields, is_active, note, updated_at)
                             VALUES (?,?,?,?,?,?,1,?,?)
                             ON CONFLICT(biz, channel, account_id) DO UPDATE SET
                                 template_id=excluded.template_id,
                                 page=excluded.page,
                                 fields=excluded.fields,
                                 is_active=1,
                                 note=excluded.note,
                                 updated_at=excluded.updated_at""",
              (biz, channel, int(account_id or 0), template_id, page, fj, note, _now()))
    _TPL_CACHE.clear()      # 改完模板立刻生效
    return True


def disable_template(biz, channel, account_id=0):
    with _conn() as (conn, kind):
        _exec(conn, kind, 'UPDATE wx_templates SET is_active=0, updated_at=? WHERE biz=? AND channel=? AND account_id=?',
              (_now(), biz, channel, int(account_id or 0)))
    _TPL_CACHE.clear()
    return True


# ============================================================
# 后台页面用的一键快照
# ============================================================
def mask(s, keep=6):
    s = s or ''
    if len(s) <= keep:
        return '*'
    return s[:keep] + '…' + '*' * 4


def snapshot(reveal=False):
    accts = list_accounts()
    if not reveal:
        accts = [dict(a, secret=mask(a.get('secret'))) for a in accts]
    return {
        'effective': {t: resolve(t) for t in ACCT_TYPES},
        'accounts': accts,
        'config': list_config_items(),
        'templates': list_templates(),
        'log': get_log(30),
        'fail_threshold': FAIL_THRESHOLD,
        'cache_ttl': CACHE_TTL,
    }
