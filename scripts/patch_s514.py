# -*- coding: utf-8 -*-
"""
S514-20260921 H5 落地页（static/deposit.html）支持支付宝
=======================================================
澄清：扫码用户实际落到的是 static/deposit.html（/store 每次请求读盘渲染它），
      不是 templates/store.html（那个是另一处页面，我 S513 也顺手加了同样支持）。

流程（老板定）：支付宝扫码进 H5 → 下单 → 【跳支付宝小程序拿订阅】→ 关掉小程序回 H5
              → 自动提交支付宝手机网站支付表单（跳收银台）→ 付完回 H5 显示取包页。

改动只有 2 处（都是新增分支/新增函数，不动任何既有逻辑）：
 1) 支付分支里加 `payParams.mode === 'alipay'`
 2) 新增 alipayPay()/submitAlipayForm()/isAlipayEnv() 三个函数
    · 跳小程序前把订单上下文写进 sessionStorage（复用页面已有的「H5支付返回检测」逻辑：
      回来会自动轮询 pay-status 并显示取包页）
    · 从支付宝小程序回来（visibilitychange）自动继续提交支付表单
"""
import hashlib
import shutil

P = 'static/deposit.html'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S514-20260921' not in src, '已打过 S514，中止'

# ---------- 1) 支付分支加支付宝 ----------
A1 = "if (payParams.mode === 'jsapi' && payParams.jsapi_params) { callWxPay(payParams.jsapi_params); }"
assert src.count(A1) == 1, '锚点1=%d' % src.count(A1)
NEW1 = (A1 + "\n            else if (payParams.mode === 'alipay' && (payParams.form || payParams.pay_url)) "
              "{ alipayPay(payParams); }")
src = src.replace(A1, NEW1, 1)

# ---------- 2) 新增函数（插在 startPayPolling 之前） ----------
A2 = 'function startPayPolling() {'
assert src.count(A2) == 1, '锚点2=%d' % src.count(A2)
CODE = '''/* [S514-20260921] 支付宝 H5：先跳支付宝小程序拿订阅，回到本页再走手机网站支付 */
    var ALIPAY_MP_APPID = '2021006199688688';
    function isAlipayEnv() { return /AlipayClient/i.test(navigator.userAgent || ''); }
    function alipayPay(pp) {
        // 订单上下文写进 sessionStorage —— 复用页面已有的「H5支付返回检测」：回来会自动轮询状态并显示取包页
        sessionStorage.setItem('pendingOrderId', String(currentOrderId));
        sessionStorage.setItem('pendingCabinetId', String(currentCabinetId));
        sessionStorage.setItem('pendingOrderNo', currentOrderNo || '');
        sessionStorage.setItem('pendingCompartment', currentCompartmentNumber || '');
        sessionStorage.setItem('pendingAccessCode', currentAccessCode || '');
        var f0 = '';
        try { f0 = sessionStorage.getItem('alipay_form') || ''; } catch (e) {}
        if (f0) { submitAlipayForm(); return; }   // 刚从支付宝小程序回来 → 直接去收银台
        try {
            sessionStorage.setItem('alipay_form', pp.form || '');
            sessionStorage.setItem('alipay_url', pp.pay_url || '');
        } catch (e) {}
        var phoneEl = document.getElementById('userPhone');
        var phone = (phoneEl && phoneEl.value) || '';
        var q = 'source=h5&order_id=' + encodeURIComponent(String(currentOrderId || '')) +
                '&phone=' + encodeURIComponent(phone) +
                '&code=' + encodeURIComponent(currentAccessCode || '');
        var u = 'alipays://platformapi/startapp?appId=' + ALIPAY_MP_APPID +
                '&page=' + encodeURIComponent('pages/subscribe/subscribe') +
                '&query=' + encodeURIComponent(q);
        var jumped = false;
        try { window.location.href = u; jumped = true; } catch (e) { jumped = false; }
        if (!jumped) { submitAlipayForm(); return; }   // scheme 跳不动就直接付款，订阅是加分项不能挡付款
        setTimeout(function () { if (!document.hidden) { submitAlipayForm(); } }, 4000);
    }
    function submitAlipayForm() {
        var f = '', u = '';
        try {
            f = sessionStorage.getItem('alipay_form') || '';
            u = sessionStorage.getItem('alipay_url') || '';
            sessionStorage.removeItem('alipay_form');
            sessionStorage.removeItem('alipay_url');
        } catch (e) {}
        if (f) { document.open(); document.write(f); document.close(); return; }
        if (u) { window.location.href = u; return; }
        showToast('支付宝下单失败，请重试');
    }
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden && isAlipayEnv()) {
            var f = '';
            try { f = sessionStorage.getItem('alipay_form') || ''; } catch (e) {}
            if (f) { submitAlipayForm(); }
        }
    });

    '''
src = src.replace(A2, CODE + A2, 1)

shutil.copy2(P, P + '.bak_s514')
open(P, 'w', encoding='utf-8').write(src)
after = hashlib.md5(src.encode('utf-8')).hexdigest()
print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('新增: S514=%d alipayPay=%d submitAlipayForm=%d isAlipayEnv=%d alipays://=%d'
      % (src.count('S514-20260921'), src.count('function alipayPay'),
         src.count('function submitAlipayForm'), src.count('function isAlipayEnv'), src.count('alipays://')))
