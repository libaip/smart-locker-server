#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S233-20260917] ① routes/user.py: 修判定缺陷——"进没进小程序"不能把 H5 自己的"跳转意图"算进来
   ② static/deposit.html: 加埋点，定位"多余的弹层"到底从哪条路来
"""
import io, sys

# ---------- 后端 ----------
UOLD = '        cur.execute("SELECT 1 FROM mp_enter_log WHERE order_id = %s AND created_at >= NOW() - INTERVAL \'30 minutes\' LIMIT 1", (order_id,))'
UNEW = ('        # [S233-20260917] 排除 H5 自己写的"跳转意图"记录：那只是"用户点了跳转"，不等于进了小程序。\n'
        '        # 之前把 jump_intent 也算作"已进入" -> 用户在微信"即将打开小程序"上点取消也会被放行，削弱了 A1 拦截。\n'
        '        cur.execute("SELECT 1 FROM mp_enter_log WHERE order_id = %s "\n'
        '                    "AND phase NOT IN (\'jump_intent\',\'jump_intent_used\') "\n'
        '                    "AND created_at >= NOW() - INTERVAL \'30 minutes\' LIMIT 1", (order_id,))')

# ---------- H5 ----------
H1_OLD = """    function mpEnterPollReport(stage, sec, limit) {
        try {
            fetch('/api/user/oa-subscribe-log', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    phone: (currentPhone || (document.getElementById('userPhone') || {}).value || ''),
                    detail: JSON.stringify({ stage: stage, sec: sec, tries: mpTryCount(), limit: limit })
                })
            }).catch(function () {});
        } catch (e) {}
    }"""
H1_NEW = """    function mpEnterPollReport(stage, sec, limit, extra) {
        // [S233] 多带 oid 和自定义字段，便于定位"多余的弹层"是哪条路弹的
        try {
            var _d = { stage: stage, sec: sec, tries: mpTryCount(), limit: limit, oid: (currentOrderId || '') };
            if (extra) { for (var _k in extra) { try { _d[_k] = extra[_k]; } catch (e2) {} } }
            fetch('/api/user/oa-subscribe-log', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    phone: (currentPhone || (document.getElementById('userPhone') || {}).value || ''),
                    detail: JSON.stringify(_d)
                })
            }).catch(function () {});
        } catch (e) {}
    }"""

H2_OLD = """    function showMpTipModal() {
        var el = document.getElementById('mpTipModal');"""
H2_NEW = """    function showMpTipModal(reason) {
        // [S233] 埋点：弹层是被哪条路弹出来的（no_scheme=没拿到跳转串 / gate=服务端判定没进过 / failsafe=60秒兜底 / test=测试开关）
        try { mpEnterPollReport('mp_modal_show', Math.round((Date.now() - (mpOpenedTime || Date.now())) / 1000), 0, { reason: reason || 'unknown' }); } catch (e) {}
        var el = document.getElementById('mpTipModal');"""

H3_OLD = """            if (!mpScheme) { showMpTipModal(); return; }"""
H3_NEW = """            if (!mpScheme) { showMpTipModal('no_scheme'); return; }"""

H4_OLD = """        try { if (mpTryCount() >= MP_RETRY_LIMIT) { mpGateEscape(); return; } } catch (e) {}
        showMpTipModal();
    }"""
H4_NEW = """        try { if (mpTryCount() >= MP_RETRY_LIMIT) { mpGateEscape(); return; } } catch (e) {}
        showMpTipModal('gate');
    }"""

H5_OLD = """        function _no() { if (_done) { return; } _done = true; try { mpGateOrEscape(); } catch (e) {} }"""
H5_NEW = """        function _no() { if (_done) { return; } _done = true; try { mpEnterPollReport('mp_gate_no', 0, 0, { asks: _askLog.join(''), oid2: _oid() }); } catch (e) {} try { mpGateOrEscape(); } catch (e) {} }"""

H6_OLD = """        function _ask(cb) {
            try {
                fetch('/api/user/mp-entered?order_id=' + encodeURIComponent(_oid()))
                  .then(function (r) { return r.json(); })
                  .then(function (d) { cb(!!(d && d.data && d.data.entered)); })
                  .catch(function () { cb(true); });      // 接口异常 -> 放行，别把真进去的人卡住
            } catch (e) { cb(true); }
        }"""
H6_NEW = """        var _askLog = [];      // [S233] 记录每次询问结果: 1=进过 0=没进过 E=网络错 N=订单号为空
        function _ask(cb) {
            if (!_oid()) { _askLog.push('N'); }
            try {
                fetch('/api/user/mp-entered?order_id=' + encodeURIComponent(_oid()))
                  .then(function (r) { return r.json(); })
                  .then(function (d) { var _ok = !!(d && d.data && d.data.entered); _askLog.push(_ok ? '1' : '0'); cb(_ok); })
                  .catch(function () { _askLog.push('E'); cb(true); });      // 接口异常 -> 放行，别把真进去的人卡住
            } catch (e) { _askLog.push('X'); cb(true); }
        }"""

H7_OLD = """        var t0 = Date.now();
        var gated = 0;"""
H7_NEW = """        var t0 = Date.now();
        var gated = 0;
        var _firstLogged = 0, _noOidLogged = 0;      // [S233] 诊断埋点只各报一次"""

H8_OLD = """            if (!oid) { return; }"""
H8_NEW = """            if (!oid) {
                if (!_noOidLogged) { _noOidLogged = 1; try { mpEnterPollReport('mp_poll_no_oid', Math.round(el / 1000), 0); } catch (e) {} }
                return;
            }"""

H9_OLD = """              .then(function (d) {
                  if (d && d.data && d.data.entered) {
                      if (mpEnterPoll) { clearInterval(mpEnterPoll); mpEnterPoll = null; }
                      try { mpEnterPollReport('mp_enter_poll_ok', Math.round(el / 1000), 0); } catch (e) {}"""
H9_NEW = """              .then(function (d) {
                  if (!_firstLogged) { _firstLogged = 1; try { mpEnterPollReport('mp_poll_first', Math.round(el / 1000), 0, { res: (d && d.data && d.data.entered) ? 1 : 0 }); } catch (e) {} }
                  if (d && d.data && d.data.entered) {
                      if (mpEnterPoll) { clearInterval(mpEnterPoll); mpEnterPoll = null; }
                      try { mpEnterPollReport('mp_enter_poll_ok', Math.round(el / 1000), 0); } catch (e) {}"""


def patch_file(path, edits, musts):
    s = io.open(path, encoding='utf-8').read()
    before = len(s)
    for tag, old, new in edits:
        n = s.count(old)
        if n != 1:
            print('[FAIL] %s 里 %s 原文出现 %d 次（要求 1）' % (path, tag, n))
            sys.exit(2)
        s = s.replace(old, new, 1)
        print('  [ok] %s: %s' % (path.split('/')[-1], tag))
    for m in musts:
        if m not in s:
            print('[FAIL] %s 缺少标记 %s' % (path, m))
            sys.exit(3)
    io.open(path, 'w', encoding='utf-8', newline='').write(s)
    print('  [DONE] %s: %d -> %d (%+d)' % (path, before, len(s), len(s) - before))


if __name__ == '__main__':
    which = sys.argv[1] if len(sys.argv) > 1 else 'all'
    up = sys.argv[2] if len(sys.argv) > 2 else 'routes/user.py'
    hp = sys.argv[3] if len(sys.argv) > 3 else 'static/deposit.html'
    if which in ('all', 'user'):
        patch_file(up, [('A) mp-entered 排除跳转意图行', UOLD, UNEW)], ["phase NOT IN ('jump_intent','jump_intent_used')"])
    if which in ('all', 'h5'):
        patch_file(hp, [
            ('1) 报告函数支持附加字段', H1_OLD, H1_NEW),
            ('2) 弹层埋点带 reason', H2_OLD, H2_NEW),
            ('3) no_scheme 原因', H3_OLD, H3_NEW),
            ('4) gate 原因', H4_OLD, H4_NEW),
            ('5) 询问全空时上报原因', H5_OLD, H5_NEW),
            ('6) 记录每次询问结果', H6_OLD, H6_NEW),
            ('7) 诊断标志变量', H7_OLD, H7_NEW),
            ('8) 订单号为空只报一次', H8_OLD, H8_NEW),
            ('9) 首次轮询结果', H9_OLD, H9_NEW),
        ], ['mp_modal_show', 'mp_gate_no', 'mp_poll_first', 'mp_poll_no_oid', '_askLog'])
