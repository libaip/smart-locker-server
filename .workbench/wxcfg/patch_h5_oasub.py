# -*- coding: utf-8 -*-
"""[功能①] 存包 H5：只给"没跳进小程序、继续走 H5 支付"的用户弹公众号订阅。

现状（改造前）：
  - `<wx-open-subscribe>` 标签以透明覆盖层的方式盖在"下一步"按钮上：用户一点"下一步"
    就被要求订阅公众号（不管进不进小程序）。总开关 OA_SUBSCRIBE_ENABLED 由后台设置
    oa_subscribe_enabled 控制，2026-09-12 起是关的。

改成：
  - 标签不再覆盖"下一步"按钮；
  - 只在【跳小程序失败 / 用户点"继续H5存包" / 25秒自动继续】这条路径上，弹一次
    "订阅通知"小弹层（里面就是那个开放标签 + "暂不订阅，继续支付"）；
  - 进过小程序的用户不会走到这条路径 -> 不弹（符合老板要求）；
  - 服务端新增 /api/user/oa-subscribe-status：查 oa_subscribe_log 里该手机号是否
    已经订阅过（detail 里出现 accept），已订阅过就不再弹，避免反复打扰。

改动文件：static/deposit.html, routes/user.py
用法：python3 patch_h5_oasub.py <项目根> [--real|--revert]
"""
import io, os, re, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
H5 = os.path.join(ROOT, 'static', 'deposit.html')
PY = os.path.join(ROOT, 'routes', 'user.py')
BKDIR = os.path.join(ROOT, 'backups')

# ---------- H5：HTML 结构（把覆盖层换成弹层） ----------
HTML_OLD = '''                <div id="oaSubWrap" style="opacity:0;pointer-events:none;position:absolute;left:0;top:0;right:0;bottom:0;z-index:9">
                    <wx-open-subscribe id="oaSubBtn" template="RyvAHJzC46JDEk_w8nAJHlLppNtNiAgkqP_GdHGWEUg,pG1ieUsHgBg5y0ZJCfjNiUrF7QuftlSWwDYXSpAp6js,nDTX0vT_wODrTnM4A1qSNnSXDUTNgw2Fm0CMF0eZe-o" style="display:block;width:100%">
                        <script type="text/wxtag-template">
                            <style>.oa-hit{display:block;width:100%;padding:14px;background:#FFC107;color:#1a1a1a;border:0;border-radius:24px;font-size:17px;font-weight:700;margin-top:6px;text-align:center;box-sizing:border-box}</style>
                            <button class="oa-hit">下一步</button>
                        </script>
                    </wx-open-subscribe>
                </div>
            </div>
            <div id="oaSubEsc" style="display:none;font-size:13px;color:#576b95;text-align:center;margin-top:4px;text-decoration:underline" onclick="onStep1Next()">点不动？点这里继续存包</div>
            <div id="oaSubHint" style="display:none;font-size:12px;color:#999;text-align:center;margin-top:6px;line-height:1.5">点“下一步”即同时订阅寄存通知（首次会提示，建议勾选“总是保持以上选择”，以后不再打扰）</div>'''

HTML_NEW = '''            </div>
            <!-- [2026-09-14] 公众号订阅弹层：只在"没跳进小程序、继续走 H5 支付"时弹一次 -->
            <div id="oaSubWrap" style="display:none;position:fixed;left:0;top:0;right:0;bottom:0;background:rgba(0,0,0,.55);z-index:99998;align-items:center;justify-content:center;padding:22px;box-sizing:border-box">
                <div style="background:#fff;border-radius:16px;padding:22px 20px;max-width:340px;width:100%;box-sizing:border-box;text-align:center">
                    <div style="font-size:19px;font-weight:700;color:#1a1a1a;margin-bottom:8px">订阅通知</div>
                    <div style="font-size:13px;color:#666;line-height:1.7;margin-bottom:14px">订阅后，“押金退还 / 退款到账”会用微信“服务通知”提醒你<br>只点这一次，以后不再打扰</div>
                    <wx-open-subscribe id="oaSubBtn" template="RyvAHJzC46JDEk_w8nAJHlLppNtNiAgkqP_GdHGWEUg,pG1ieUsHgBg5y0ZJCfjNiUrF7QuftlSWwDYXSpAp6js,nDTX0vT_wODrTnM4A1qSNnSXDUTNgw2Fm0CMF0eZe-o" style="display:block;width:100%">
                        <script type="text/wxtag-template">
                            <style>.oa-hit{display:block;width:100%;padding:14px;background:#FFC107;color:#1a1a1a;border:0;border-radius:24px;font-size:17px;font-weight:700;text-align:center;box-sizing:border-box}</style>
                            <button class="oa-hit">订阅通知</button>
                        </script>
                    </wx-open-subscribe>
                    <div id="oaSubSkip" style="font-size:13px;color:#999;margin-top:16px;text-decoration:underline">暂不订阅，继续支付</div>
                </div>
            </div>'''

# ---------- H5：ready 回调不再覆盖按钮 ----------
READY_OLD = '''            var w = document.getElementById('oaSubWrap');
            var h = document.getElementById('oaSubHint');
            if (w) { w.style.opacity = '1'; w.style.pointerEvents = 'auto'; }
            if (h) { h.style.display = 'block'; }
            var _esc = document.getElementById('oaSubEsc');
            if (_esc) { _esc.style.display = 'block'; }
            var ob = document.getElementById('oaOrigBtn');
            if (ob) { ob.style.display = 'none'; }   // 关键: 藏掉原按钮, 用户只能点到订阅标签'''
READY_NEW = '''            // [2026-09-14] 标签不再盖在"下一步"按钮上：只在走 H5 支付时才弹订阅层
            var w = document.getElementById('oaSubWrap');
            var h = document.getElementById('oaSubHint');
            var ob = document.getElementById('oaOrigBtn');'''

# ---------- H5：oaSubHide 改成 display 控制 ----------
HIDE_OLD = '''    function oaSubHide(reason) {
        var w = document.getElementById('oaSubWrap');
        var h = document.getElementById('oaSubHint');
        if (w) { w.style.opacity = '0'; w.style.pointerEvents = 'none'; }
        if (h) { h.style.display = 'none'; }
        console.log('[oa-subscribe] 覆盖层关闭:', reason || '');
    }'''
HIDE_NEW = '''    function oaSubHide(reason) {
        var w = document.getElementById('oaSubWrap');
        if (w) { w.style.display = 'none'; }
        console.log('[oa-subscribe] 订阅弹层关闭:', reason || '');
    }'''

# ---------- H5：oaSubProceed 改成"订阅完继续支付" ----------
PROC_OLD = '''    function oaSubProceed(detail) {
        if (_oaSubBusy) { return; }
        _oaSubBusy = true;
        oaSubHide('已交互');
        oaSubReport(detail);
        oaSubDiag('interact');
        try { onStep1Next(); } catch (e) {
            console.log('[oa-subscribe] onStep1Next 异常', e); _oaSubBusy = false; return;
        }
        setTimeout(function() { _oaSubBusy = false; }, 1500);
    }'''
PROC_NEW = '''    function oaSubProceed(detail) {
        if (_oaSubBusy) { return; }
        _oaSubBusy = true;
        oaSubReport(detail);
        oaSubDiag('interact');
        oaSubFinish();
        setTimeout(function() { _oaSubBusy = false; }, 1500);
    }
    // ===== [2026-09-14] 公众号订阅弹层（只给走 H5 支付的用户） =====
    var _oaSubCb = null, _oaSubShown = false;
    function oaSubFinish() {
        var w = document.getElementById('oaSubWrap');
        if (w) { w.style.display = 'none'; }
        var cb = _oaSubCb; _oaSubCb = null;
        if (cb) { try { cb(); } catch (e) { console.log('[oa-subscribe] 继续异常', e); } }
    }
    function oaSubPrompt(cb) {
        // 开关关着 / 已经弹过 / 标签不在 -> 直接继续，绝不影响存包
        try {
            if (typeof OA_SUBSCRIBE_ENABLED === 'undefined' || !OA_SUBSCRIBE_ENABLED || _oaSubShown) { cb(); return; }
            var w = document.getElementById('oaSubWrap');
            var tag = document.getElementById('oaSubBtn');
            if (!w || !tag) { cb(); return; }
            _oaSubShown = true; _oaSubCb = cb;
            var sk = document.getElementById('oaSubSkip');
            if (sk && !sk._bound) { sk._bound = 1; sk.onclick = function () { oaSubDiag('skip'); oaSubFinish(); }; }
            var phone = (currentPhone || (document.getElementById('userPhone') ? document.getElementById('userPhone').value : '') || '');
            // 已经订阅过的（服务端 oa_subscribe_log 里有 accept）就不弹了
            fetch('/api/user/oa-subscribe-status?phone=' + encodeURIComponent(phone))
              .then(function (r) { return r.json(); })
              .then(function (d) {
                  if (d && d.data && d.data.subscribed) { oaSubDiag('already_subscribed'); oaSubFinish(); return; }
                  w.style.display = 'flex';
                  oaSubDiag('prompt_show');
              })
              .catch(function () { w.style.display = 'flex'; oaSubDiag('prompt_show_nochk'); });
        } catch (e) { try { cb(); } catch (e2) {} }
    }'''

# ---------- H5：两条"H5 路径"接到弹层 ----------
WIRE1_OLD = '''        if (mpRescueTimer) { clearTimeout(mpRescueTimer); mpRescueTimer = null; }
        mpOpenedTime = 0;
        goToStep2Direct();
    }'''
WIRE1_NEW = '''        if (mpRescueTimer) { clearTimeout(mpRescueTimer); mpRescueTimer = null; }
        mpOpenedTime = 0;
        oaSubPrompt(goToStep2Direct);   // [2026-09-14] 用户选了"继续H5存包" -> 先问一次公众号订阅
    }'''
WIRE2_OLD = "        mpGiveupTimer = setTimeout(function() { mpGiveupTimer = null; goToStep2Direct(); }, 25000);"
WIRE2_NEW = "        mpGiveupTimer = setTimeout(function() { mpGiveupTimer = null; oaSubPrompt(goToStep2Direct); }, 25000);"

# ---------- 服务端：订阅状态查询接口 ----------
API_ANCHOR = '''        logger.info('[oa_subscribe_log] phone=%s detail=%s', phone, detail[:300])
        return json_response(message='ok')
    except Exception as e:
        logger.error('[oa_subscribe_log] 错误: %s', e)
        return json_response(message=str(e), code=500)'''
API_NEW = API_ANCHOR + '''


@bp.route('/user/oa-subscribe-status', methods=['GET'])
def user_oa_subscribe_status():
    """[2026-09-14] 查该手机号是否已经订阅过公众号通知（供 H5 决定要不要再弹订阅层）。

    口径：oa_subscribe_log 里最近若干条 detail 中出现 accept 即视为已订阅。
    读不到/出错一律返回 subscribed=False（宁可多弹一次，也不能把用户卡住）。
    """
    try:
        phone = str(request.args.get('phone') or '')[:20]
        if not phone:
            return json_response(data={'subscribed': False})
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT detail FROM oa_subscribe_log WHERE phone = %s ORDER BY id DESC LIMIT 5", (phone,))
        rows = cur.fetchall() or []
        conn.close()
        subscribed = any('accept' in str((r.get('detail') if isinstance(r, dict) else r[0]) or '') for r in rows)
        return json_response(data={'subscribed': bool(subscribed)})
    except Exception as e:
        logger.warning('[oa_subscribe_status] 查询失败(按未订阅处理): %s', e)
        return json_response(data={'subscribed': False})'''


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d))
         for d in os.listdir(BKDIR) if d.startswith('h5oasub_') and os.path.isfile(os.path.join(BKDIR, d, 'deposit.html'))]
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    shutil.copy2(os.path.join(src, 'deposit.html'), H5)
    shutil.copy2(os.path.join(src, 'user.py'), PY)
    for p in (H5, PY):
        if p.endswith('.py'):
            subprocess.run(['python3', '-m', 'py_compile', p], check=True)
    print('  已还原自 %s' % src)
    raise SystemExit(0)

h5 = io.open(H5, encoding='utf-8').read()
py = io.open(PY, encoding='utf-8').read()
edits = [('H5 HTML 弹层结构', h5, HTML_OLD, HTML_NEW),
         ('H5 ready 不再覆盖按钮', None, READY_OLD, READY_NEW),
         ('H5 oaSubHide', None, HIDE_OLD, HIDE_NEW),
         ('H5 oaSubProceed + 弹层函数', None, PROC_OLD, PROC_NEW),
         ('H5 接到 closeMpTipModal', None, WIRE1_OLD, WIRE1_NEW),
         ('H5 接到 25 秒兜底', None, WIRE2_OLD, WIRE2_NEW)]
ok = True
for label, _txt, old, new in edits:
    n = h5.count(old)
    print('  %s %-28s 命中 %d/1' % ('✓' if n == 1 else '✗', label, n))
    if n != 1:
        ok = False
n_api = py.count(API_ANCHOR)
print('  %s %-28s 命中 %d/1' % ('✓' if n_api == 1 else '✗', '服务端状态接口', n_api))
if n_api != 1:
    ok = False
if not ok:
    raise SystemExit('[中止] 锚点不对，一个文件都不写')

new_h5 = h5
for label, _txt, old, new in edits:
    new_h5 = new_h5.replace(old, new, 1)
new_py = py.replace(API_ANCHOR, API_NEW, 1)
try:
    compile(new_py, PY, 'exec')
except SyntaxError as e:
    raise SystemExit('[中止] user.py 语法不过: %s' % e)
# 抽出 H5 里最后一段 <script> 做 JS 语法检查
blocks = re.findall(r'<script>(.*?)</script>', new_h5, re.S)
js = blocks[-1] if blocks else ''
io.open('/tmp/_h5_check.js', 'w', encoding='utf-8').write(js)
print('  已抽出内联 JS %d 字节到 /tmp/_h5_check.js' % len(js.encode('utf-8')))

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'h5oasub_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(H5, os.path.join(d, 'deposit.html'))
shutil.copy2(PY, os.path.join(d, 'user.py'))
io.open(H5, 'w', encoding='utf-8').write(new_h5)
io.open(PY, 'w', encoding='utf-8').write(new_py)
subprocess.run(['python3', '-m', 'py_compile', PY], check=True)
print('  已写入 static/deposit.html 与 routes/user.py')
print('  备份: %s' % d)
