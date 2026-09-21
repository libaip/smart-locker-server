# -*- coding: utf-8 -*-
"""
S516b-20260921 支付宝回跳兜底：把"已跳小程序"标记同时写 localStorage
==================================================================
原因：S517 让小程序用 my.ap.openURL(return_url) 跳回 H5 —— 那是一个"新开 webview"，
      新 webview 的 sessionStorage 很可能为空（每个 webview 一份）。
      而 S515 的两处判断都只看 sessionStorage：
        ① 加载后自动进支付页：if(!sessionStorage.alipay_sub_jumped) return;  -> 新 webview 里不成立 -> 回到存包第一步，不自动进支付页
        ② reserveDoorAndProceed 里防重复跳：!sessionStorage.alipay_sub_jumped  -> 不成立 -> 用户再点下一步会【又跳一次小程序】（死循环）
      改成同时看 localStorage（同源共享、跨 webview 保留）：
        · alipayJumpSubscribe() 里同时写 localStorage['alipay_sub_jumped_ls']='1'
        · 加载后自动进支付页：sessionStorage 或 localStorage 任一命中就继续
        · 防重复跳：两个都为空才允许跳
        · alipayResumeFromJump() 里把两个标记清掉（消费一次即失效，不影响下次正常流程）
用法：python patch_s516b.py <deposit.html>
"""
import hashlib
import shutil
import sys

P = sys.argv[1] if len(sys.argv) > 1 else 'static/deposit.html'
src = open(P, encoding='utf-8', newline='').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S516b-20260921' not in src, '已打过 S516b，中止'
errors = []


def rep(old, new, tag):
    global src
    n = src.count(old)
    if n != 1:
        errors.append('%s 锚点命中 %d 次（应为1）' % (tag, n))
        return
    src = src.replace(old, new, 1)


# ① 跳转时同时写 localStorage 标记
rep("""        alipayWatchReturn();                      // 回来（隐藏->可见）直接进支付页""",
    """        // [S516b-20260921] 同时写 localStorage：回跳若是新开 webview，sessionStorage 会丢
        try { localStorage.setItem('alipay_sub_jumped_ls', '1'); } catch (e) {}
        alipayWatchReturn();                      // 回来（隐藏->可见）直接进支付页""",
    '跳转写 localStorage 标记')

# ② 防重复跳（reserveDoorAndProceed 的支付宝分支）
rep("""} else if (isAlipayEnv() && !sessionStorage.getItem('alipay_sub_jumped')) {""",
    """} else if (isAlipayEnv() && !sessionStorage.getItem('alipay_sub_jumped') && !(function () { try { return localStorage.getItem('alipay_sub_jumped_ls'); } catch (e) { return ''; } })()) {""",
    '防重复跳加 localStorage')

# ③ 加载后自动进支付页：两个标记任一命中即可
rep("""        var jumped = '';
        try { jumped = sessionStorage.getItem('alipay_sub_jumped') || ''; } catch (e) {}
        if (!jumped) { return; }""",
    """        var jumped = '';
        try { jumped = sessionStorage.getItem('alipay_sub_jumped') || localStorage.getItem('alipay_sub_jumped_ls') || ''; } catch (e) {}
        if (!jumped) { return; }""",
    '加载后自动进支付页')

# ④ 消费后清掉两个标记
rep("""    function alipayResumeFromJump() {""",
    """    function alipayResumeFromJump() {
        // [S516b-20260921] 消费掉标记（含 localStorage 那份），避免下次流程被跳过
        try { sessionStorage.removeItem('alipay_sub_jumped'); } catch (e) {}
        try { localStorage.removeItem('alipay_sub_jumped_ls'); } catch (e) {}""",
    '消费后清标记')

if errors:
    print('❌ 有锚点没命中，未写文件：')
    for e in errors:
        print('  - ' + e)
    sys.exit(1)

shutil.copy2(P, P + '.bak_s516b')
open(P, 'w', encoding='utf-8', newline='').write(src)
after = hashlib.md5(src.encode('utf-8')).hexdigest()
print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print("检查: alipay_sub_jumped_ls 出现 %d 次（应为4）" % src.count('alipay_sub_jumped_ls'))
