# -*- coding: utf-8 -*-
"""[修 bug] 区分"真的进了小程序" vs "点了取消"：把小程序上报落库 + 给 H5 一个查询接口。

问题：微信在跳小程序时会弹它自己的确认框（"即将打开小程序"）。用户点取消时，页面会
      "隐藏一下又回来"，我们原来的逻辑（有存档 + 页面可见了）就当成"跳成功"，直接放行到
      支付页 —— 老板真机实测抓到这个 bug。

修法：
  1) /api/user/mp-exit-log 收到小程序上报时，除了写日志，还落一张表 mp_enter_log
     （order_id / phone / phase / created_at）——"用户真的进了小程序"从此有据可查；
  2) 新增 /api/user/mp-entered?order_id=xx —— 查这个订单近 30 分钟内有没有小程序上报；
     查库出错时返回 entered=true（宁可放行，也不把真进去的人卡住）。
  3) 建表语句是幂等的，可重复执行。

用法：python3 patch_mp_entered.py <项目根> [--real|--revert]
     建表另见 create_mp_enter_log.sql（用 psql 执行一次）
"""
import io, os, shutil, subprocess, sys, time

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ROOT = args[0] if args else '/home/ubuntu/smart-locker'
REAL = '--real' in sys.argv
REVERT = '--revert' in sys.argv
PY = os.path.join(ROOT, 'routes', 'user.py')
BKDIR = os.path.join(ROOT, 'backups')

A1_OLD = """        logger.info('[mp_exit_log] ' + json.dumps(payload, ensure_ascii=False))
        return json_response(message='ok')
    except Exception as e:
        logger.error(f'[mp_exit_log] 错误: {e}')
        return json_response(message=str(e), code=500)"""
A1_NEW = """        logger.info('[mp_exit_log] ' + json.dumps(payload, ensure_ascii=False))
        # [A1-20260914] 把"用户真的进了小程序"落库：H5 用它区分"取消跳转"和"真的进去了"
        try:
            if payload.get('order_id'):
                _mc = get_db()
                _mcur = _mc.cursor()
                _mcur.execute("INSERT INTO mp_enter_log (order_id, phone, phase) VALUES (%s, %s, %s)",
                              (payload.get('order_id'), payload.get('phone') or '', payload.get('phase') or ''))
                _mc.commit()
                _mc.close()
        except Exception as _me:
            logger.warning('[mp_exit_log] 落库失败(不影响主流程): %s' % _me)
        return json_response(message='ok')
    except Exception as e:
        logger.error(f'[mp_exit_log] 错误: {e}')
        return json_response(message=str(e), code=500)


@bp.route('/user/mp-entered', methods=['GET'])
def user_mp_entered():
    \"\"\"[A1-20260914] 这个订单刚才有没有真的进过小程序（区分"取消跳转"与"真的进去了"）。

    小程序每次进入都会 POST /api/user/mp-exit-log（phase=attempt），那条会落库；
    H5 从"跳转返回"时先问这里：entered=true 才放行进支付页，否则弹回"请跳转小程序"。
    查库出错时返回 entered=true（宁可放行，也不把真的进去了的人卡住）。
    \"\"\"
    try:
        order_id = str(request.args.get('order_id') or '')[:40]
        if not order_id:
            return json_response(data={'entered': False})
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM mp_enter_log WHERE order_id = %s AND created_at >= NOW() - INTERVAL '30 minutes' LIMIT 1", (order_id,))
        r = cur.fetchone()
        conn.close()
        return json_response(data={'entered': bool(r)})
    except Exception as e:
        logger.warning('[mp_entered] 查询失败(按已进入放行): %s', e)
        return json_response(data={'entered': True})"""


def newest_backup():
    if not os.path.isdir(BKDIR):
        return None
    c = [(os.path.getmtime(os.path.join(BKDIR, d)), os.path.join(BKDIR, d, 'user.py'))
         for d in os.listdir(BKDIR) if d.startswith('mpent_') and os.path.isfile(os.path.join(BKDIR, d, 'user.py'))]
    return sorted(c)[-1][1] if c else None


if REVERT:
    src = newest_backup()
    if not src:
        raise SystemExit('[中止] 找不到备份')
    shutil.copy2(src, PY)
    subprocess.run(['python3', '-m', 'py_compile', PY], check=True)
    print('  已还原自 %s' % src)
    raise SystemExit(0)

t = io.open(PY, encoding='utf-8').read()
n = t.count(A1_OLD)
print('  %s 接口改造锚点 命中 %d/1' % ('✓' if n == 1 else '✗', n))
if n != 1:
    raise SystemExit('[中止] 锚点不对，不写')
new = t.replace(A1_OLD, A1_NEW, 1)
try:
    compile(new, PY, 'exec')
except SyntaxError as e:
    raise SystemExit('[中止] 语法不过: %s' % e)
print('  语法检查通过')

if not REAL:
    print('  干跑结束，什么都没写。加 --real 执行。')
    raise SystemExit(0)

ts = time.strftime('%Y%m%d_%H%M%S')
d = os.path.join(BKDIR, 'mpent_%s' % ts)
os.makedirs(d, exist_ok=True)
shutil.copy2(PY, os.path.join(d, 'user.py'))
io.open(PY, 'w', encoding='utf-8').write(new)
subprocess.run(['python3', '-m', 'py_compile', PY], check=True)
print('  已写入 routes/user.py; 备份 %s' % os.path.join(d, 'user.py'))
