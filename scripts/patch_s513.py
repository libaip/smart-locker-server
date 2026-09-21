# -*- coding: utf-8 -*-
"""
S513-20260921 H5 落地页支持支付宝（templates/store.html）
=========================================================
流程（老板定的）：支付宝用户扫码进 H5 → 下单 → 【跳支付宝小程序拿订阅】→ 关掉小程序回到 H5
                 → 自动提交支付宝手机网站支付表单（跳收银台）→ 支付完成回 H5 显示成功。

改动 4 处（都是新增/插入，不改既有微信逻辑）：
  1) doPay() 里加 `mode==="alipay"` 分支
  2) 新增 alipayPay() / submitAlipayForm() / isAlipayEnv() 等函数
  3) doStore() 成功后把订单信息存进 sessionStorage（回来自动显示成功用）
  4) 页面加载时：若存在待完成支付 → 轮询状态；若已支付 → 直接显示成功页
"""
import hashlib
import py_compile
import shutil

P = 'templates/store.html'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S513-20260921' not in src, '已打过 S513，中止'

# ---------- 1) doPay 里加支付宝分支 ----------
A1 = 'if(pp.mode==="h5"&&pp.mweb_url){location.href=pp.mweb_url;return;}'
assert src.count(A1) == 1, '锚点1=%d' % src.count(A1)
src = src.replace(A1, 'if(pp.mode==="alipay"){alipayPay(pp);return;}\n' + A1, 1)

# ---------- 2) 新增函数（插在 pollPayStatus 之前） ----------
A2 = 'function pollPayStatus(){'
assert src.count(A2) == 1, '锚点2=%d' % src.count(A2)
ALIPAY_JS = '''/* [S513-20260921] 支付宝 H5 支付：先跳支付宝小程序拿订阅，回来再走手机网站支付 */
var ALIPAY_MP_APPID='2021006199688688';
function isAlipayEnv(){return /AlipayClient/i.test(navigator.userAgent||"");}
function savePendingOrder(){
  try{
    if(!storeData||!storeData.order_id)return;
    sessionStorage.setItem("locker_alipay_pending",JSON.stringify({order_id:storeData.order_id,pwd:storeData.pwd||"",slot_number:storeData.slot_number||""}));
  }catch(e){}
}
function alipayPay(pp){
  /* 已有待提交表单（说明刚从支付宝小程序回来）-> 直接去收银台 */
  var f=sessionStorage.getItem("alipay_form")||"";
  if(f){submitAlipayForm();return;}
  /* 第一次：把表单存好，跳支付宝小程序拿订阅；失败/超时则直接支付（订阅是加分项，不能挡住付款） */
  try{
    sessionStorage.setItem("alipay_form",pp.form||"");
    sessionStorage.setItem("alipay_url",pp.pay_url||"");
  }catch(e){}
  savePendingOrder();
  var phone=(document.getElementById("phone")||{}).value||"";
  var code=(document.getElementById("pwd")||{}).value||"";
  var q="source=h5&order_id="+encodeURIComponent((storeData&&storeData.order_id)||"")+"&phone="+encodeURIComponent(phone)+"&code="+encodeURIComponent(code);
  var u="alipays://platformapi/startapp?appId="+ALIPAY_MP_APPID+"&page="+encodeURIComponent("pages/subscribe/subscribe")+"&query="+encodeURIComponent(q);
  var jumped=false;
  try{location.href=u;jumped=true;}catch(e){}
  if(!jumped){submitAlipayForm();return;}
  setTimeout(function(){ if(!document.hidden){ submitAlipayForm(); } },4000);
}
function submitAlipayForm(){
  var f="",u="";
  try{ f=sessionStorage.getItem("alipay_form")||""; u=sessionStorage.getItem("alipay_url")||""; }catch(e){}
  try{ sessionStorage.removeItem("alipay_form"); sessionStorage.removeItem("alipay_url"); }catch(e){}
  if(f){ document.open(); document.write(f); document.close(); return; }
  if(u){ location.href=u; return; }
  alert("支付宝下单失败，请重试");
}
/* 用户关掉支付宝小程序回到本页 -> 自动继续支付 */
document.addEventListener("visibilitychange",function(){
  if(!document.hidden&&isAlipayEnv()){
    var f="";try{f=sessionStorage.getItem("alipay_form")||"";}catch(e){}
    if(f){submitAlipayForm();}
  }
});
'''
src = src.replace(A2, ALIPAY_JS + A2, 1)

# ---------- 3) doStore 成功后保存订单（回来自动显示成功） ----------
A3 = 'if(d.code==200){storeData=d.data;'
assert src.count(A3) == 1, '锚点3=%d' % src.count(A3)
src = src.replace(A3, A3 + 'savePendingOrder();', 1)

# ---------- 4) 页面加载时：有待完成支付就轮询/直接显示成功 ----------
A4 = 'var _sp = localStorage.getItem(\'locker_saved_phone\');'
assert src.count(A4) == 1, '锚点4=%d' % src.count(A4)
RESUME_JS = '''/* [S513] 从支付宝收银台/小程序回来：有待完成订单就查状态，已支付直接显示成功 */
(function(){
  var raw="";try{raw=sessionStorage.getItem("locker_alipay_pending")||"";}catch(e){}
  if(!raw)return;
  var info={};try{info=JSON.parse(raw)||{};}catch(e){info={};}
  if(!info.order_id)return;
  var tries=0;
  var t=setInterval(function(){
    tries++;
    if(tries>40){clearInterval(t);return;}
    fetch("/api/store/pay",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({order_id:info.order_id})}).then(function(r){return r.json();}).then(function(d){
        if(d&&d.code==200){
          clearInterval(t);
          try{sessionStorage.removeItem("locker_alipay_pending");}catch(e){}
          storeData={order_id:info.order_id,pwd:info.pwd||"",slot_number:info.slot_number||"",pay_params:null};
          var ip=document.getElementById("inputPage"); if(ip){ip.style.display="none";}
          var pp2=document.getElementById("payPage"); if(pp2){pp2.classList.remove("show");pp2.style.display="none";}
          showSuccess();
        }
      }).catch(function(){});
  },3000);
})();
'''
src = src.replace(A4, RESUME_JS + A4, 1)

shutil.copy2(P, P + '.bak_s513')
open(P, 'w', encoding='utf-8').write(src)
after = hashlib.md5(src.encode('utf-8')).hexdigest()
print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('新增标记数: S513=%d alipayPay=%d submitAlipayForm=%d savePendingOrder=%d'
      % (src.count('S513-20260921'), src.count('function alipayPay'), src.count('function submitAlipayForm'), src.count('function savePendingOrder')))
