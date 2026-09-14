# -*- coding: utf-8 -*-
"""[正式改动] 存包弹窗不再请求"寄存成功"模板 + 服务端不再发送"寄存成功"通知。

背景（已验证）: 实验证明小程序弹窗用的就是服务端 /api/user/subscribe-templates 下发的列表
（把返回里的"寄存成功"替换掉 → 弹窗立刻从 3 条变 2 条）。所以这件事**不用改小程序**。

改动两处:
  1) routes/user.py  get_subscribe_templates(): deposit 系列不再返回"寄存成功"的 ID，
     而是指向"押金退还"（保留字段名，避免前端按旧字段名取到空值导致弹窗报错——实验中已证明这样弹窗正常显示 2 条）
  2) routes/payment.py 支付回调里那条"寄存成功"发送：默认关掉（用户不会再被请求该模板，发送必然 43101）。
     想恢复：把 _SEND_STORAGE_SUCCESS_NOTIFY 改成 True 即可（一个字）。

用法:
  python3 patch_popup_tpl.py <项目根目录>            # 干跑
  python3 patch_popup_tpl.py <项目根目录> --real     # 真改（带备份）
  python3 patch_popup_tpl.py <项目根目录> --revert   # 从最近一次备份还原
"""
import io, os, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
BKDIR = os.path.join(ROOT, 'backups')

P_OLD = ("    _deposit = _wx_tpl('subscribe_deposit', 'mp', 'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk')     # 寄存成功")
P_NEW = ("    # [2026-09-14] 弹窗不再请求\"寄存成功\"（老板要求：把用户的一次性授权集中在跟钱相关的两条上）\n"
         "    # 字段名保留、指向\"押金退还\"，避免前端按旧字段名取到空值把弹窗搞挂（实验已验证这样弹窗正常显示 2 条）\n"
         "    _deposit = _general")

S_OLD = ("                    # S119 2026-09-06: 启用寄存成功通知(新模板已加入小程序订阅授权列表)\n"
         "                    send_wx_subscribe_message(openid, _wx_tpl('subscribe_deposit', 'mp', 'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk'), subscribe_data, phone=order.get('user_phone'), page='pages/mine/mine', unionid=_pay_unionid)")
S_NEW = ("                    # S119 2026-09-06: 启用寄存成功通知(新模板已加入小程序订阅授权列表)\n"
         "                    # [2026-09-14] 弹窗已不再请求该模板 -> 用户没有额度, 发送必然 43101(白打接口+刷日志) -> 默认关闭\n"
         "                    # 想恢复发送: 把下面这行改成 True 即可\n"
         "                    _SEND_STORAGE_SUCCESS_NOTIFY = False\n"
         "                    if _SEND_STORAGE_SUCCESS_NOTIFY:\n"
         "                        send_wx_subscribe_message(openid, _wx_tpl('subscribe_deposit', 'mp', 'Q3Fts5C64Zcz81EZk0t7KUTcGtVA-Itt0alm1YWtxMk'), subscribe_data, phone=order.get('user_phone'), page='pages/mine/mine', unionid=_pay_unionid)")

FILES = ['routes/user.py', 'routes/payment.py']


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = []
    for d in os.listdir(BKDIR):
        if d.startswith('popup_'):
            f = os.path.join(BKDIR, d, 'snapshot')
            if os.path.isdir(f):
                c.append((os.path.getmtime(f), f))
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    for rel in FILES:
        shutil.copy2(os.path.join(src, rel.replace('/', '__')), os.path.join(ROOT, rel))
    print('  已还原自 %s' % src)
    for rel in FILES:
        subprocess.run(['python3', '-m', 'py_compile', os.path.join(ROOT, rel)], check=True)
        print('   %s md5=%s' % (rel, subprocess.run(['md5sum', os.path.join(ROOT, rel)], capture_output=True, text=True).stdout.split()[0]))
    raise SystemExit(0)

texts, ok = {}, True
for rel, (o, n) in (('routes/user.py', (P_OLD, P_NEW)), ('routes/payment.py', (S_OLD, S_NEW))):
    p = os.path.join(ROOT, rel)
    txt = io.open(p, encoding='utf-8').read()
    cnt = txt.count(o)
    print('  %s 锚点命中: %d/1' % (rel, cnt))
    if cnt != 1:
        ok = False
        continue
    texts[p] = txt.replace(o, n)
if not ok:
    raise SystemExit('[中止] 锚点不对, 一个文件都不写')
for p, t in texts.items():
    try:
        compile(t, p, 'exec')
    except SyntaxError as e:
        raise SystemExit('[中止] 语法不过: %s' % e)
print('  语法检查通过')

if not REAL:
    print('  干跑结束, 什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'popup_%s' % ts, 'snapshot')
os.makedirs(d, exist_ok=True)
for p, t in texts.items():
    rel = os.path.relpath(p, ROOT)
    shutil.copy2(p, os.path.join(d, rel.replace('/', '__')))
    io.open(p, 'w', encoding='utf-8').write(t)
    compile(io.open(p, encoding='utf-8').read(), p, 'exec')
    print('  已写入 %s md5=%s' % (rel, subprocess.run(['md5sum', p], capture_output=True, text=True).stdout.split()[0]))
print('  备份: %s' % d)
print('  还原: python3 %s %s --revert' % (os.path.abspath(__file__), ROOT))
