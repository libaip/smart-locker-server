# -*- coding: utf-8 -*-
"""
配置中心后台 API（Flask Blueprint）
=================================================================
本地 demo 直接注册；以后上服务器时，把 _guard 换成 routes.admin_v2.require_auth
就能接进现有后台（返回体沿用 {code,data,message} 信封，与 helpers.json_response 一致）。
"""

from flask import Blueprint, jsonify, request

import wx_config as C

bp = Blueprint('wx_config_center', __name__, url_prefix='/api/wx-config')

_AUTH_HOOK = None
_AUTH_DECORATOR = None
_PROBER = None
_WRAP_CACHE = {}


def use_auth_decorator(dec):
    """【上线推荐】直接拿宿主后台的鉴权装饰器来用。

    例：wx_config_api.use_auth_decorator(helpers.require_auth)
        app.register_blueprint(wx_config_api.bp)

    实现说明（踩过的坑）：**不能**用"改 bp.view_functions"这个办法 —— Flask 在 @bp.route
    装饰的那一刻，就把视图函数收进闭包了，事后再改那个字典完全不生效。
    （第 1 步在 106 上验证时发现：没带 token 竟然返回了 200 和全部账号数据。）
    所以这里改用 blueprint 级 before_request 网关：每个请求先跑一遍"真实装饰器包出来的
    空视图"，它拦下就原样返回它的响应（401）。
    """
    global _AUTH_DECORATOR
    _AUTH_DECORATOR = dec
    _WRAP_CACHE.clear()
    return True

def set_auth_hook(fn):
    """另一种接法：给一个"检查不通过就抛异常"的函数。用 use_auth_decorator 就不用这个。
    本地默认不做鉴权。"""
    global _AUTH_HOOK
    _AUTH_HOOK = fn


def set_prober(fn):
    """自定义探活函数（本地测试用假的，避免真连微信）"""
    global _PROBER
    _PROBER = fn


def _call_decorated_noop():
    """跑一遍"真实鉴权装饰器 + 空视图"：被拦会返回它的响应（401），放行则返回 None"""
    fn = _WRAP_CACHE.get(id(_AUTH_DECORATOR))
    if fn is None:
        def _noop():
            return None
        fn = _AUTH_DECORATOR(_noop)
        _WRAP_CACHE[id(_AUTH_DECORATOR)] = fn
    return fn()          # ← 注意要调用，不是把函数本身返回出去


@bp.before_request
def _auth_gate():
    """统一鉴权网关：放行返回 None；被拦就把后台自己的响应（401）原样返回"""
    if _AUTH_DECORATOR is not None:
        try:
            r = _call_decorated_noop()
        except Exception as e:
            return err('鉴权失败: %s' % e, 401)
        if r is not None:
            return r
        return None
    if _AUTH_HOOK is not None:
        _AUTH_HOOK()
    return None


def _guard():
    """视图里的兜底检查（用 use_auth_decorator 时由 before_request 统一处理）"""
    if _AUTH_HOOK is not None:
        _AUTH_HOOK()


def ok(data=None, message='ok'):
    return jsonify({'code': 200, 'data': data, 'message': message})


def err(message, code=400):
    return jsonify({'code': code, 'data': None, 'message': message}), code


def _body():
    return request.get_json(silent=True) or {}


# ------------------------------------------------------------
# 总览
# ------------------------------------------------------------
@bp.route('/snapshot', methods=['GET'])
def api_snapshot():
    _guard()
    reveal = request.args.get('reveal') in ('1', 'true', 'yes')
    return ok(C.snapshot(reveal=reveal))


@bp.route('/effective', methods=['GET'])
def api_effective():
    """业务真正要用的：当前生效的小程序/公众号凭据（含来源：db 还是兜底）"""
    _guard()
    return ok(C.resolve_all())


# ------------------------------------------------------------
# 账号池
# ------------------------------------------------------------
@bp.route('/accounts', methods=['GET'])
def api_accounts():
    _guard()
    at = request.args.get('type') or None
    reveal = request.args.get('reveal') in ('1', 'true', 'yes')
    rows = C.list_accounts(at)
    # [GUARD-20260913] 带上"能不能真的用"，前端才能把不能用的号灰掉
    rows = [dict(r, usable=C.check_usable(r)[0], usable_reason=C.check_usable(r)[1]) for r in rows]
    if not reveal:
        rows = [dict(r, secret=C.mask(r.get('secret'))) for r in rows]
    return ok(rows)


@bp.route('/accounts', methods=['POST'])
def api_account_create():
    _guard()
    d = _body()
    for f in ('acct_type', 'name', 'appid'):
        if not d.get(f):
            return err('缺少字段 %s' % f)
    try:
        new_id = C.create_account(
            d['acct_type'], d['name'], d['appid'], d.get('secret', ''),
            token=d.get('token', ''), aes_key=d.get('aes_key', ''),
            subject=d.get('subject', ''), mch_relation=d.get('mch_relation', 'none'),
            priority=d.get('priority', 100), is_active=d.get('is_active', False),
            note=d.get('note', ''))
    except Exception as e:
        return err(str(e))
    return ok({'id': new_id}, '已新增账号')


@bp.route('/accounts/<int:account_id>', methods=['POST', 'PUT'])
def api_account_update(account_id):
    _guard()
    d = _body()
    fields = {k: v for k, v in d.items() if k in (
        'name', 'appid', 'secret', 'token', 'aes_key', 'subject',
        'mch_relation', 'priority', 'note')}
    if not fields:
        return err('没有可更新的字段')
    C.update_account(account_id, **fields)
    return ok({'id': account_id}, '已更新')


@bp.route('/accounts/<int:account_id>', methods=['DELETE'])
def api_account_delete(account_id):
    _guard()
    done, msg = C.delete_account(account_id)
    return ok({'deleted': done}, msg) if done else err(msg)


@bp.route('/accounts/<int:account_id>/toggle', methods=['POST'])
def api_account_toggle(account_id):
    """启用/停用。启用 = 自动成为该类当前生效账号"""
    _guard()
    row = C.get_account(account_id)
    if not row:
        return err('账号不存在', 404)
    body = _body()
    active = body.get('active')
    if active is None:
        active = not bool(row.get('is_active'))
    done, msg = C.set_active(account_id, bool(active))
    if done:
        # [GUARD-20260913] 只有真的改成功了才记日志：被防呆拒绝的操作不能留下
        # "启用（手动）"这种假记录（上线验证时真踩到过，日志里多出一条没发生过的操作）
        C.log_switch(row['acct_type'], '', row['name'],
                     ('启用' if active else '停用') + '（手动）', 'local-admin')
    return ok({'is_active': 1 if active else 0}, msg) if done else err(msg)


@bp.route('/accounts/<int:account_id>/switch', methods=['POST'])
def api_account_switch(account_id):
    """一键切换：这个号设为生效，同类其它号全部停用"""
    _guard()
    row = C.get_account(account_id)
    if not row:
        return err('账号不存在', 404)
    reason = _body().get('reason') or '后台手动切换'
    done, msg = C.switch_to(row['acct_type'], account_id, operator='local-admin', reason=reason)
    return ok({'effective': C.resolve(row['acct_type'])}, msg) if done else err(msg)


@bp.route('/accounts/<int:account_id>/probe', methods=['POST'])
def api_account_probe(account_id):
    """探活：默认真连微信取 access_token；本地可用 mode=simulate 造结果"""
    _guard()
    body = _body()
    mode = body.get('mode') or 'real'
    if mode == 'simulate':
        good = bool(body.get('ok', True))
        prober = lambda acct: (good, body.get('detail') or ('模拟探活成功' if good else '模拟探活失败'))
    else:
        prober = _PROBER
    return ok(C.probe(account_id, prober=prober))


@bp.route('/accounts/<int:account_id>/simulate-fail', methods=['POST'])
def api_account_simulate_fail(account_id):
    """模拟调用失败 N 次 → 验证"连续失败自动停用 + 自动切备用"这条链路。

    注意：这是给开发/演示用的接口。如果对着"当前正在生效"的账号用，会真的把它
    自动停用（业务会回落到 config.py 原本的账号），生产上属于误伤，所以默认拦住；
    确实要测就显式传 allow_active=true。
    """
    _guard()
    body = _body()
    _row = C.get_account(account_id)
    if _row:
        _eff = C.get_effective_account(_row['acct_type'], use_cache=False)
        if _eff and _eff.get('id') == account_id and not body.get('allow_active'):
            return err('拒绝：这是当前正在生效的%s账号。要模拟它失败请显式传 allow_active=true'
                       % ('小程序' if _row['acct_type'] == 'mp' else '公众号'))
    times = int(body.get('times') or 1)
    detail = body.get('detail') or '模拟失败(errcode 43004 require subscribe)'
    results = [C.mark_health(account_id, False, detail) for _ in range(max(1, min(times, 20)))]
    acct = C.get_account(account_id)
    return ok({
        'results': results,
        'account': acct,
        'effective': C.resolve(acct['acct_type']) if acct else None,
    }, '已模拟 %d 次失败' % times)


# ------------------------------------------------------------
# 可切换配置项
# ------------------------------------------------------------
@bp.route('/config', methods=['GET'])
def api_config_list():
    _guard()
    return ok({'items': C.list_config_items(), 'merged': C.all_config()})


@bp.route('/config', methods=['POST'])
def api_config_set():
    _guard()
    d = _body()
    key = d.get('key') or d.get('cfg_key')
    if not key:
        return err('缺少 key')
    C.set_config(key, d.get('value', ''), group_name=d.get('group_name', ''), note=d.get('note', ''))
    return ok({'key': key, 'value': C.get_config(key)}, '已保存')


# ------------------------------------------------------------
# 模板
# ------------------------------------------------------------
@bp.route('/templates', methods=['GET'])
def api_templates():
    _guard()
    return ok(C.list_templates(channel=request.args.get('channel') or None))


@bp.route('/templates', methods=['POST'])
def api_template_set():
    _guard()
    d = _body()
    for f in ('biz', 'channel', 'template_id'):
        if not d.get(f):
            return err('缺少字段 %s' % f)
    try:
        C.set_template(d['biz'], d['channel'], d['template_id'],
                       page=d.get('page', ''), fields=d.get('fields'),
                       account_id=d.get('account_id', 0), note=d.get('note', ''))
    except Exception as e:
        return err(str(e))
    return ok(C.get_template(d['biz'], d['channel']), '模板已保存')


@bp.route('/templates/disable', methods=['POST'])
def api_template_disable():
    _guard()
    d = _body()
    C.disable_template(d.get('biz'), d.get('channel'), d.get('account_id', 0))
    return ok(None, '模板已停用')


# ------------------------------------------------------------
# 切换日志
# ------------------------------------------------------------
@bp.route('/log', methods=['GET'])
def api_log():
    _guard()
    return ok(C.get_log(int(request.args.get('limit') or 50)))


@bp.route('/health', methods=['GET'])
def api_health():
    """给运维/监控用的最小健康检查：配置中心能不能读库"""
    eff = C.resolve_all()
    return ok({
        'db': 'ok',
        'mp': {'appid': eff['mp'].get('appid'), 'source': eff['mp'].get('source')},
        'oa': {'appid': eff['oa'].get('appid'), 'source': eff['oa'].get('source')},
    })
