// chk516.js —— 校验 S516 生成的支付宝 scheme 链接格式
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const i = html.indexOf('function alipayJumpSubscribe');
if (i < 0) { console.log('未找到 alipayJumpSubscribe'); process.exit(1); }
// 朴素花括号配平截取函数体
let d = 0, j = html.indexOf('{', i), end = -1;
for (let k = j; k < html.length; k++) {
  const c = html[k];
  if (c === '{') d++;
  else if (c === '}') { d--; if (d === 0) { end = k + 1; break; } }
}
const body = html.slice(i, end);
const ALIPAY_MP_APPID = '2021006199688688';
const currentOrderId = 12345, currentPhone = '13800138000', currentAccessCode = '8888';
const location = { href: 'https://locker.cqdyxl.com/store?device=ABC123&mp_openid=oXyZ' };
const document = { getElementById: () => null, hidden: false };
const window = {};
const sessionStorage = { getItem: () => null, setItem: () => {} };
const src = 'return (' + body.replace('function alipayJumpSubscribe()', 'function()') + ')();';
let captured = null;
const stub = {
  ALIPAY_MP_APPID, currentOrderId, currentPhone, currentAccessCode,
  location: { href: location.href },
  document: { getElementById: () => ({ value: '' }), hidden: false, visibilityState: 'visible' },
  window: { location: { set href(v) { captured = v; } } },
  sessionStorage: { getItem: () => null, setItem: () => {} },
  alipayWatchReturn: () => {}, alipayResumeFromJump: () => {}, setTimeout: () => {}
};
const f = new Function(...Object.keys(stub), src);
f(...Object.values(stub));
console.log('生成的 scheme:');
console.log(captured);
console.log('');
// 用小程序端同样的解析方式反解
const m = /[?&]page=([^&]*)/.exec(captured);
const q = /[?&]query=([^&]*)/.exec(captured);
const pageRaw = decodeURIComponent(m[1]);
const queryRaw = decodeURIComponent(q[1]);
console.log('page 解码后:', pageRaw);
const pOpts = new URLSearchParams(pageRaw.split('?')[1] || '');
const qOpts = new URLSearchParams(queryRaw);
console.log('page 参数:', Object.fromEntries(pOpts));
console.log('query 参数:', Object.fromEntries(qOpts));
console.log('');
const okSlashRaw = captured.includes('page=pages/subscribe/subscribe');
console.log('page 斜杠未编码(官方要求):', okSlashRaw ? 'OK' : 'FAIL');
console.log('小程序会读到的 phone:', qOpts.get('phone') || pOpts.get('phone'));
console.log('小程序会读到的 return_url:', qOpts.get('return_url') || pOpts.get('return_url'));
