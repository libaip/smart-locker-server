# -*- coding: utf-8 -*-
"""
S515-20260921 修正支付宝流程顺序（老板反馈）
============================================
老板反馈：现在是"H5 填完资料 → 直接跳支付 → 支付没成又跳小程序订阅"，顺序错了。
正确应为：**H5 填完手机号/密码 → 点"下一步" → 跳支付宝小程序拿订阅 → 回 H5 → 再进支付（收银台）**。

改法（3 处）：
 1) `reserveDoorAndProceed()` 里，在 `allowH5ToMp === 1`（微信那套）之前加**支付宝分支**：
    存档（复用 mpSavePendingJump）→ 跳支付宝小程序订阅页 → 回来直接进支付页
 2) 新增 alipayJumpSubscribe() / alipayWatchReturn() / alipayResumeFromJump()：
    · 回来检测用"页面隐藏→再次可见"（**不走微信那套 /api/user/mp-entered 门禁**，否则支付宝用户会被拦回第一步）
    · 回来后把 _mpEscaped 置真，确保任何遗留门禁都不再拦
    · 页面若被重载，加载时也会自动接着进支付页
 3) `alipayPay()` 简化：**不再跳小程序**（订阅已在"下一步"拿过），直接提交支付表单去收银台
"""
import hashlib
import shutil

P = 'static/deposit.html'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S515-20260921' not in src, '已打过 S515，中止'
assert 'S514-20260921' in src, '找不到 S514 痕迹，中止'

# ---------- 1) alipayPay 简化：直接提交表单（不再跳小程序） ----------
s_a = src.index('    function alipayPay(pp) {')
s_b = src.index('    function submitAlipayForm() {')
NEW_PAY = '''    function alipayPay(pp) {
        // [S515] 订阅已在"下一步"那一步拿过了，这里只负责去收银台
        sessionStorage.setItem('pendingOrderId', String(currentOrderId));
        sessionStorage.setItem('pendingCabinetId', String(currentCabinetId));
        sessionStorage.setItem('pendingOrderNo', currentOrderNo || '');
        sessionStorage.setItem('pendingCompartment', currentCompartmentNumber || '');
        sessionStorage.setItem('pendingAccessCode', currentAccessCode || '');
        try {
            sessionStorage.setItem('alipay_form', pp.form || '');
            sessionStorage.setItem('alipay_url', pp.pay_url || '');
        } catch (e) {}
        submitAlipayForm();
    }

'''
src = src[:s_a] + NEW_PAY + src[s_b:]

# ---------- 2) 新增"下一步就跳订阅"的一套函数 ----------
A2 = "    function isAlipayEnv() { return /AlipayClient/i.test(navigator.userAgent || ''); }"
assert src.count(A2) == 1, '锚点2=%d' % src.count(A2)
NEW2 = A2 + '''

    // [S515-20260921] 支付宝：点"下一步"就跳小程序拿订阅，回来再进支付页
    var alipayReturnWatch = null;
    function alipayJumpSubscribe() {
        var q = 'source=h5&order_id=' + encodeURIComponent(String(currentOrderId || '')) +
                '&phone=' + encodeURIComponent(currentPhone || '') +
                '&code=' + encodeURIComponent(currentAccessCode || '');
        var u = 'alipays://platformapi/startapp?appId=' + ALIPAY_MP_APPID +
                '&page=' + encodeURIComponent('pages/subscribe/subscribe') +
                '&query=' + encodeURIComponent(q);
        alipayWatchReturn();                      // 回来（隐藏->可见）直接进支付页
        var jumped = false;
        try { window.location.href = u; jumped = true; } catch (e) { jumped = false; }
        if (!jumped) { try { alipayResumeFromJump(); } catch (e) {} return; }
        // 4 秒还没跳走（没装小程序/scheme 被拦）-> 直接进支付页，不能把用户卡住
        setTimeout(function () { if (!document.hidden) { try { alipayResumeFromJump(); } catch (e) {} } }, 4000);
    }
    function alipayWatchReturn() {
        if (alipayReturnWatch) { clearInterval(alipayReturnWatch); }
        var sawHidden = false, ticks = 0;
        alipayReturnWatch = setInterval(function () {
            ticks++;
            if (ticks > 240) { clearInterval(alipayReturnWatch); alipayReturnWatch = null; return; }
            var st = document.visibilityState || (document.hidden ? 'hidden' : 'visible');
            if (st === 'hidden') { sawHidden = true; return; }
            if (st === 'visible' && sawHidden) {
                clearInterval(alipayReturnWatch); alipayReturnWatch = null;
                try { alipayResumeFromJump(); } catch (e) {}
            }
        }, 1000);
    }
    function alipayResumeFromJump() {
        // 还原订单上下文（复用微信那套存档），但**不走**微信的"是否进过小程序"门禁
        try {
            var o = mpReadPendingJump();
            if (o) {
                mpClearPendingJump();
                currentOrderId = o.order_id;
                currentOrderNo = o.order_no || '';
                currentCompartmentNumber = o.compartment || '';
                if (o.access_code) { currentAccessCode = o.access_code; }
                if (o.cabinet_id) { currentCabinetId = o.cabinet_id; }
                if (o.deposit) { currentDepositAmount = o.deposit; }
                if (o.size) { selectedSize = o.size; }
                if (o.phone) {
                    currentPhone = o.phone;
                    var pel = document.getElementById('userPhone');
                    if (pel && !pel.value) { pel.value = o.phone; }
                }
            }
        } catch (e) {}
        try { _mpEscaped = true; } catch (e) {}     // 标记已放行，任何门禁都不再拦
        try { goToStep2Direct(); } catch (e) {}
    }
    // 页面若被重载（有些机型返回时会重载）-> 加载后自动接着进支付页
    (function () {
        if (!isAlipayEnv()) { return; }
        var jumped = '';
        try { jumped = sessionStorage.getItem('alipay_sub_jumped') || ''; } catch (e) {}
        if (!jumped) { return; }
        setTimeout(function () {
            var f = '';
            try { f = sessionStorage.getItem('alipay_form') || ''; } catch (e) {}
            if (f) { submitAlipayForm(); return; }   // 已经在支付环节 -> 继续去收银台
            try { alipayResumeFromJump(); } catch (e) {}
        }, 900);
    })();'''
src = src.replace(A2, NEW2, 1)

# ---------- 3) 下一步流程里插入支付宝分支（在微信 allowH5ToMp 之前） ----------
A3 = '                } else if (allowH5ToMp === 1) {'
assert src.count(A3) == 1, '锚点3=%d' % src.count(A3)
NEW3 = ('''                } else if (isAlipayEnv() && !sessionStorage.getItem('alipay_sub_jumped')) {
                    // [S515] 支付宝：先跳小程序拿订阅，回来再进支付页（与微信同样的顺序）
                    sessionStorage.setItem('alipay_sub_jumped', '1');
                    mpSavePendingJump();
                    alipayJumpSubscribe();
''' + A3)
src = src.replace(A3, NEW3, 1)

shutil.copy2(P, P + '.bak_s515')
open(P, 'w', encoding='utf-8').write(src)
after = hashlib.md5(src.encode('utf-8')).hexdigest()
print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('新增: S515=%d alipayJumpSubscribe=%d alipayWatchReturn=%d alipayResumeFromJump=%d'
      % (src.count('S515-20260921'), src.count('function alipayJumpSubscribe'),
         src.count('function alipayWatchReturn'), src.count('function alipayResumeFromJump')))
print('alipayPay 里是否还跳小程序（应为 0）:', src[src.index('function alipayPay'):src.index('function submitAlipayForm')].count('alipays://'))
