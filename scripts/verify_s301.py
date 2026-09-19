# -*- coding: utf-8 -*-
"""S301 本地验证：1) 抽取 <script> 块做 node --check  2) 用真实模板条件跑免押单/押金单用例"""
import json
import re
import subprocess
import sys
import tempfile
import os

PATH = sys.argv[1] if len(sys.argv) > 1 else 'admin-v2.html'
html = open(PATH, 'rb').read().decode('utf-8')

# ---------- 1) JS 语法检查 ----------
blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
print('内联 <script> 块数 = %d' % len(blocks))
tmp = tempfile.mkdtemp(prefix='s301js_')
fail = 0
for i, b in enumerate(blocks):
    fp = os.path.join(tmp, 'block%d.js' % (i + 1))
    open(fp, 'w', encoding='utf-8').write(b)
    r = subprocess.run(['node', '--check', fp], capture_output=True, text=True, encoding='utf-8', errors='replace')
    flag = 'OK' if r.returncode == 0 else 'FAIL'
    if r.returncode != 0:
        fail += 1
    print('  块%d: %s%s' % (i + 1, flag, '' if r.returncode == 0 else '  ' + r.stderr.strip().splitlines()[0]))
print('语法检查: %s' % ('全部通过' if fail == 0 else '%d 块失败' % fail))

# ---------- 2) 抽出真实条件表达式 ----------
m_refund = re.search(r'v-if="([^"]*)" class="btn-text orange" @click="\$parent\.refundOrder\(o\)">退款</button>', html)
m_refunded = re.search(r'v-if="([^"]*)" class="btn-text" style="color:#bbb[^"]*" disabled>已退款</button>', html)
m_tag = re.search(r'<span v-if="(\$parent\.isNoPrepay\(o\))" class="status-tag gray">无预付款</span>', html)
m_method = re.search(r'(isNoPrepay:function\(o\)\{.*?\},)', html)

assert m_refund, '没找到退款按钮条件'
assert m_refunded, '没找到已退款按钮条件'
assert m_tag, '没找到无预付款标签条件'
assert m_method, '没找到 isNoPrepay 方法'

cond_refund = m_refund.group(1).replace('$parent.', '')
cond_refunded = m_refunded.group(1).replace('$parent.', '')
cond_tag = m_tag.group(1).replace('$parent.', '')
method = m_method.group(1)

print('\n抽取到的条件：')
print('  退款按钮:   %s' % cond_refund)
print('  已退款按钮: %s' % cond_refunded)
print('  无预付款标签: %s' % cond_tag)

js = "var C = " + json.dumps({
    'refund': cond_refund.replace("\\'", "'"),
    'refunded': cond_refunded.replace("\\'", "'"),
    'tag': cond_tag.replace("\\'", "'"),
}, ensure_ascii=False) + ";\n" + """
var M = { %s };
var isNoPrepay = M.isNoPrepay;
function chk(c, o){ try { return eval(c); } catch(e){ return 'ERR:'+e.message; } }
var cases = [
 ['免押单 使用中 status=2 dep=0 ref=0',      {status:2, deposit_amount:0, refund_amount:0, refund_status:'none'}],
 ['免押单 可退   status=3 dep=0 ref=0',      {status:3, deposit_amount:0, refund_amount:0, refund_status:'none'}],
 ['免押单 已完结 status=4 dep=0 ref=0',      {status:4, deposit_amount:0, refund_amount:0, refund_status:'none'}],
 ['押金单 可退   status=3 dep=10 ref=0',     {status:3, deposit_amount:10, refund_amount:0, refund_status:'none'}],
 ['押金单 使用中 status=2 dep=10 ref=0',     {status:2, deposit_amount:10, refund_amount:0, refund_status:'none'}],
 ['押金单 已退款 status=4 dep=10 ref=10',    {status:4, deposit_amount:10, refund_amount:10, refund_status:'refunded'}]
];
var bad = 0;
for (var i=0;i<cases.length;i++){
  var name=cases[i][0], o=cases[i][1];
  var r=chk(C.refund,o), d=chk(C.refunded,o), t=chk(C.tag,o);
  var expect = name.indexOf('免押单')===0
      ? {r:false, d:false, t:true}
      : (name.indexOf('已退款')>0 ? {r:false, d:true, t:false} : {r:true, d:false, t:false});
  var ok = (r===expect.r && d===expect.d && t===expect.t);
  if(!ok) bad++;
  console.log((ok?'  [PASS] ':'  [FAIL] ')+name+
    '  => 退款按钮='+r+' 已退款按钮='+d+' 无预付款标签='+t+
    (ok?'':'  期望: 退款按钮='+expect.r+' 已退款按钮='+expect.d+' 无预付款标签='+expect.t));
}
console.log(bad===0 ? '用例全部通过' : (bad+' 个用例失败'));
""" % (method)

fp = os.path.join(tmp, 'logic.js')
open(fp, 'w', encoding='utf-8').write(js)
r = subprocess.run(['node', fp], capture_output=True, text=True, encoding='utf-8', errors='replace')
print('\n逻辑用例：')
print(r.stdout.strip())
if r.stderr.strip():
    print(r.stderr.strip())
