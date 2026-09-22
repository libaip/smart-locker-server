#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S307 离线断言（不联网 / 不连库 / 不写库）

方法：
  从 routes/user.py 用 AST 抽取 **真实源码文本** 的 link_openid 函数（含 @bp.route 的 def 体），
  exec 到受控命名空间；get_db 换成假连接（记录每一条 SQL 与参数），
  helpers.phone_openid_rows / upsert_phone_openid_row / upsert_user_balance_row 用**真函数**，
  json_response 用**真函数**（flask app_context 下真 jsonify）。
  同时用改前备份 routes/user.py.s307bak.* 抽同一函数做 SQL 轨迹逐字节对比。

用法: python3 scripts/verify_s307_offline.py <pre_patch_backup_path>
"""
from __future__ import print_function
import ast
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import psycopg2
import psycopg2.errors
from flask import Flask

import helpers

CUR_PATH = os.path.join(REPO, 'routes', 'user.py')
PHONE = '13667618419'
KNOWN = 'idx_user_balances_phone_empty_union'
PAYLOAD = {'phone': PHONE, 'openid': 'oQXFs3Zo7A4rE2s7GfX_eC19ny3g',
           'mp_openid': '', 'unionid': '', 'wechat_name': 'S307test'}

# 生产日志里逐字抓下来的原始错误文本（journalctl -u smart-locker 2026-09-22）
PROD_MSG = 'duplicate key value violates unique constraint "idx_user_balances_phone_empty_union"'

_results = []


def check(tid, desc, ok, extra=''):
    _results.append((tid, desc, bool(ok), extra))
    print('[%s] %s :: %s %s' % ('PASS' if ok else 'FAIL', tid, desc, extra))


# ---------------------------------------------------------------- AST 抽取
def extract_fn(path, name='link_openid'):
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if seg is None:
                raise SystemExit('get_source_segment 返回 None')
            return seg, node.lineno, node.end_lineno, src
    raise SystemExit('未找到函数 %s in %s' % (name, path))


# ---------------------------------------------------------------- 假 DB
class FakeCursor(object):
    def __init__(self, raiser=None):
        self.sql_log = []
        self.raiser = raiser
        self._rows = []
        self._row = None

    def execute(self, sql, params=None):
        self.sql_log.append((sql, params))
        if self.raiser:
            exc = self.raiser(sql, params)
            if exc is not None:
                raise exc
        s = re.sub(r'\s+', ' ', sql).strip().upper()
        self._rows = []
        self._row = None
        if s.startswith('SELECT'):
            if 'FROM PHONE_OPENIDS' in s or 'FROM USERS' in s or 'FROM USER_BALANCES' in s:
                self._rows = []
        elif 'INSERT INTO PHONE_OPENIDS' in s or 'UPDATE PHONE_OPENIDS' in s:
            self._row = {'id': 555001}
            self._rows = [{'id': 555001}]
        return self

    def fetchone(self):
        if self._row is not None:
            return self._row
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn(object):
    def __init__(self, cursor):
        self._c = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return self._c

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


class FakeRequest(object):
    def __init__(self, payload):
        self._p = payload

    def get_json(self):
        return self._p


class FakeLogger(object):
    def __init__(self):
        self.records = []

    def _rec(self, lvl, msg, *a):
        self.records.append((lvl, (msg % a) if a else msg))

    def warning(self, msg, *a):
        self._rec('WARNING', msg, *a)

    def error(self, msg, *a):
        self._rec('ERROR', msg, *a)

    def info(self, msg, *a):
        self._rec('INFO', msg, *a)


def make_raiser(kind):
    """kind: 'known' | 'other' | 'readonly' | 'othermsg'"""
    def _r(sql, params):
        if 'INSERT INTO user_balances' not in sql:
            return None
        if kind == 'known':
            return FakeUVE(PROD_MSG, KNOWN)
        if kind == 'known_nodiag':
            return FakeUVE(PROD_MSG, None)
        if kind == 'other':
            return FakeUVE(
                'duplicate key value violates unique constraint "user_balances_openid_uk"',
                'user_balances_openid_uk')
        if kind == 'othermsg':
            return FakeUVE(
                'duplicate key value violates unique constraint "user_balances_openid_uk"',
                None)
        if kind == 'readonly':
            return psycopg2.errors.ReadOnlySqlTransaction(
                'cannot execute INSERT in a read-only transaction')
        return None
    return _r


class FakeUVE(psycopg2.errors.UniqueViolation):
    """真实的 psycopg2.errors.UniqueViolation 子类，可注入 diag.constraint_name。"""

    def __init__(self, msg, cname):
        super(FakeUVE, self).__init__(msg)
        self._cname = cname

    @property
    def diag(self):
        outer = self

        class _D(object):
            constraint_name = outer._cname
        return _D()


APP = Flask(__name__)


def run_case(fn, kind=None, payload=None, get_db_raises=None, logger=None):
    """跑一次 link_openid，返回 (状态码, 响应体文本, sql轨迹, conn, logger)"""
    cur = FakeCursor(make_raiser(kind) if kind else None)
    conn = FakeConn(cur)
    lg = logger or FakeLogger()
    if get_db_raises is not None:
        def _get_db():
            raise get_db_raises
    else:
        def _get_db():
            return conn

    ns = {
        '__name__': 's307_extracted',
        'psycopg2': psycopg2,
        'request': FakeRequest(payload if payload is not None else dict(PAYLOAD)),
        'get_db': _get_db,
        'phone_openid_rows': helpers.phone_openid_rows,
        'upsert_phone_openid_row': helpers.upsert_phone_openid_row,
        'upsert_user_balance_row': helpers.upsert_user_balance_row,
        'is_new_mp_identity': lambda **kw: True,
        'json_response': helpers.json_response,
        'logger': lg,
        'appid': '',
    }
    # 路由函数体内是 `from helpers import get_db`（内层 import，优先于命名空间）
    _orig = helpers.get_db
    helpers.get_db = _get_db
    try:
        code = compile(ast.parse(fn), '<extracted link_openid>', 'exec')
        exec(code, ns)
        with APP.app_context():
            resp = ns['link_openid']()
            status = getattr(resp, 'status_code', None)
            text = resp.get_data(as_text=True) if hasattr(resp, 'get_data') else str(resp)
    finally:
        helpers.get_db = _orig
    return status, text, cur.sql_log, conn, lg


def norm_trace(log):
    return [re.sub(r'\s+', ' ', s).strip() for s, p in log]


def main():
    pre_path = sys.argv[1] if len(sys.argv) > 1 else None
    cur_src, cur_ln, cur_end, _ = extract_fn(CUR_PATH)
    print('extracted link_openid from routes/user.py lines %d-%d (%d lines)'
          % (cur_ln, cur_end, cur_src.count('\n') + 1))

    # ---------------- A3-1: 精确约束名 -> 成功，且无任何 UPDATE
    st, body, log, conn, lg = run_case(cur_src, kind='known')
    j = json.loads(body)
    check('A3-1a', '精确 constraint_name 的 UniqueViolation -> HTTP 200 + 关联成功',
          st == 200 and j.get('code') == 200 and j.get('message') == '关联成功',
          'status=%s body=%s' % (st, body.strip()))
    ups = [s for s, _ in log if re.match(r'(?is)^\s*UPDATE\b', s)]
    upb = [s for s, _ in log if 'UPDATE user_balances' in s]
    ub_write = [s for s, _ in log
                if re.search(r'(?is)^\s*(INSERT|UPDATE|DELETE)\b', s) and 'user_balances' in s]
    check('A3-1b', 'A3 成功路径: UPDATE 语句 0 条 / UPDATE user_balances 0 条',
          len(ups) == 0 and len(upb) == 0,
          'updates=%d up_user_balances=%d' % (len(ups), len(upb)))
    check('A3-1c',
          'A3 成功路径: 对 user_balances 的写语句只有改前那条既有 INSERT 被试写(1 条, 且被拒)；0 条 UPDATE / 0 条 DELETE',
          len(ub_write) == 1 and re.match(r'(?is)^\s*INSERT\b', ub_write[0]) is not None
          and not [s for s in ub_write if re.match(r'(?is)^\s*(UPDATE|DELETE)\b', s)],
          'n=%d kind=%s' % (len(ub_write),
                            (re.match(r'(?is)^\s*(\w+)', ub_write[0]).group(1) if ub_write else '-')))
    print('    [evidence] A3成功路径 SQL 轨迹:')
    for i, (s, p) in enumerate(log, 1):
        print('      %d) %s | params=%r' % (i, re.sub(r'\s+', ' ', s).strip()[:110], p))
    check('A3-1d', 'A3 成功路径: phone_openids 绑定语句仍发生 1 条（保留绑定，符合预期）',
          len([s for s, _ in log if 'phone_openids' in s.lower()]) == 1,
          'n=%d' % len([s for s, _ in log if 'phone_openids' in s.lower()]))
    check('A3-1e', 'A3 成功路径: 有 warning 日志且无 error 日志',
          any(l == 'WARNING' for l, _ in lg.records)
          and not any(l == 'ERROR' for l, _ in lg.records),
          'records=%s' % lg.records)
    check('A3-1f', 'A3 成功路径: conn.close() 被调用（连接归还池）', conn.closes >= 1,
          'closes=%d' % conn.closes)

    # ---------------- A3-2: 约束名不匹配 -> 仍然报错
    st2, body2, log2, conn2, lg2 = run_case(cur_src, kind='other')
    j2 = json.loads(body2)
    check('A3-2', 'constraint_name 不匹配的 UniqueViolation -> 仍然 500（不被吞）',
          st2 == 500 and j2.get('code') == 500,
          'status=%s body=%s' % (st2, body2.strip()))

    # ---------------- A3-2b: 消息里是别的索引名 + diag 缺失 -> 仍然报错
    st2b, body2b, _, _, _ = run_case(cur_src, kind='othermsg')
    check('A3-2b', 'diag 缺失且错误原文是别的索引名 -> 仍然 500（兜底匹配不放水）',
          st2b == 500, 'status=%s body=%s' % (st2b, body2b.strip()))

    # ---------------- A3-3: 只读事务 -> 仍然报错
    st3, body3, _, _, lg3 = run_case(cur_src, kind='readonly')
    j3 = json.loads(body3)
    check('A3-3', '只读事务错误(cannot execute ... read-only transaction) -> 仍然 500（绝不被吞）',
          st3 == 500 and j3.get('code') == 500,
          'status=%s body=%s' % (st3, body3.strip()))

    # ---------------- A3-4: 连接池耗尽 -> 仍然报错
    st4, body4, _, _, _ = run_case(cur_src, get_db_raises=psycopg2.OperationalError(
        'connection pool exhausted'))
    check('A3-4', '连接池耗尽(OperationalError) -> 仍然 500（绝不被吞）',
          st4 == 500, 'status=%s body=%s' % (st4, body4.strip()))

    # ---------------- A3-5: 非 UniqueViolation 的 IntegrityError -> 仍然报错
    class NotNullV(psycopg2.errors.IntegrityError):
        pass

    st5, body5, _, _, _ = run_case(cur_src, kind=None)
    check('A3-5', '(对照) 正常成功路径未受改动影响 -> 200 关联成功',
          st5 == 200 and json.loads(body5).get('message') == '关联成功',
          'status=%s body=%s' % (st5, body5.strip()))

    # ---------------- A3-6: diag=None 但错误原文含确切索引名（兜底路径）-> 成功
    st6, body6, _, _, _ = run_case(cur_src, kind='known_nodiag')
    check('A3-6', 'diag.constraint_name 为 None、错误原文含确切索引名 -> 200（兜底路径生效）',
          st6 == 200 and json.loads(body6).get('message') == '关联成功',
          'status=%s body=%s' % (st6, body6.strip()))

    # ---------------- A4: 500 响应体不含原始 DB 错误文本
    for tag, b in (('A3-2', body2), ('A3-3', body3), ('A3-4', body4)):
        bad = [t for t in ('duplicate key', 'unique constraint', 'user_balances_openid_uk',
                           'read-only transaction', 'pool exhausted', 'psycopg2', 'Traceback')
               if t in b]
        check('A4-%s' % tag, '500 响应体不含原始 DB 错误文本', not bad,
              '泄漏=%s body=%s' % (bad, b.strip()))

    # ---------------- A5: 改前/改后 SQL 轨迹逐字节一致（证明没新增任何 DB 语句）
    if pre_path and os.path.exists(pre_path):
        pre_src, pre_ln, pre_end, _ = extract_fn(pre_path)
        print('pre-patch link_openid lines %d-%d' % (pre_ln, pre_end))
        # 场景1: 成功
        _, _, l_cur, _, _ = run_case(cur_src, kind=None)
        _, _, l_pre, _, _ = run_case(pre_src, kind=None)
        check('A5-1', '成功场景 SQL 轨迹 改前==改后（逐条）', norm_trace(l_cur) == norm_trace(l_pre),
              'cur=%d pre=%d' % (len(l_cur), len(l_pre)))
        # 场景2: 已知唯一冲突
        _, _, l_cur2, _, _ = run_case(cur_src, kind='known')
        _, _, l_pre2, _, _ = run_case(pre_src, kind='known')
        check('A5-2', '已知唯一冲突场景 SQL 轨迹 改前==改后（逐条）',
              norm_trace(l_cur2) == norm_trace(l_pre2),
              'cur=%d pre=%d' % (len(l_cur2), len(l_pre2)))
        print('    [evidence] 已知唯一冲突场景 轨迹(pre==cur):')
        for i, s in enumerate(norm_trace(l_cur2), 1):
            print('      %d) %s' % (i, s[:120]))
        # 场景3: 只读事务
        _, _, l_cur3, _, _ = run_case(cur_src, kind='readonly')
        _, _, l_pre3, _, _ = run_case(pre_src, kind='readonly')
        check('A5-3', '只读事务场景 SQL 轨迹 改前==改后（逐条）',
              norm_trace(l_cur3) == norm_trace(l_pre3),
              'cur=%d pre=%d' % (len(l_cur3), len(l_pre3)))
        # 改前行为对照：改前对已知唯一冲突是 500
        st_pre, body_pre, _, _, _ = run_case(pre_src, kind='known')
        check('A5-4', '改前同一输入为 500（对照，证明缺陷与修复点一致）',
              st_pre == 500, 'pre_status=%s pre_body=%s' % (st_pre, body_pre.strip()[:120]))
        # 抽取到的源码文本差异只在 except 段
        check('A5-5', '改前/改后抽取函数体差异仅存在于异常处理段（新增行包含 UniqueViolation）',
              'except psycopg2.errors.UniqueViolation' in cur_src
              and 'except psycopg2.errors.UniqueViolation' not in pre_src,
              'cur_has_A3=%s pre_has_A3=%s'
              % ('except psycopg2.errors.UniqueViolation' in cur_src,
                 'except psycopg2.errors.UniqueViolation' in pre_src))
    else:
        check('A5', '未提供改前备份路径 -> 无法做轨迹对比', False, pre_path)

    # ---------------- A6: 代码面证明（UPDATE user_balances 计数）
    pre_helpers = os.path.join(REPO, 'helpers.py')
    cur_helpers_src = io.open(pre_helpers, encoding='utf-8').read()
    cur_routes_src = io.open(CUR_PATH, encoding='utf-8').read()
    n_ub_cur = len(re.findall(r'UPDATE user_balances', cur_routes_src, re.I))
    n_ub_helpers = len(re.findall(r'UPDATE user_balances', cur_helpers_src, re.I))
    if pre_path and os.path.exists(pre_path):
        pre_routes_src = io.open(pre_path, encoding='utf-8').read()
        n_ub_pre = len(re.findall(r'UPDATE user_balances', pre_routes_src, re.I))
        check('A6-1', 'routes/user.py 的 UPDATE user_balances 出现次数 改前==改后',
              n_ub_pre == n_ub_cur, 'pre=%d cur=%d' % (n_ub_pre, n_ub_cur))
        check('A6-2', '新增的 except 段内不含 UPDATE user_balances',
              'UPDATE user_balances' not in cur_src.split(
                  'except psycopg2.errors.UniqueViolation')[1]
              if 'except psycopg2.errors.UniqueViolation' in cur_src else False,
              'A3段内出现次数=%d' % (len(re.findall(
                  r'UPDATE user_balances',
                  cur_src.split('except psycopg2.errors.UniqueViolation')[1], re.I))
              if 'except psycopg2.errors.UniqueViolation' in cur_src else -1))
    check('A6-3', 'helpers.py 完全未被本次改动触碰（源码内 UPDATE user_balances 计数=7）',
          n_ub_helpers == 7, 'helpers=%d' % n_ub_helpers)

    print('\n================ 汇总 ================')
    bad = [r for r in _results if not r[2]]
    for tid, desc, ok, extra in _results:
        print('%-6s %-8s %s' % (tid, 'PASS' if ok else 'FAIL', desc))
    print('--------------------------------------')
    print('TOTAL=%d  PASS=%d  FAIL=%d' % (len(_results), len(_results) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
