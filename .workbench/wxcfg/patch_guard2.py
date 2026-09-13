# -*- coding: utf-8 -*-
"""
[T-1789308446] 补一个小修：被防呆拒绝的"启用"操作不该写切换日志

怎么发现的：上线验证时我试了"启用占位符号（应该被拒）"，结果生产库里多出一条
"启用（手动）→ 备用公众号-异主体(待注册)" 的日志 —— 但那次操作根本没生效。
日志是给人看"到底有没有切过号"的，留假记录会误导。

改动：
  1) wx_config_api.py: api_account_toggle 只在 done 为真时才记日志
  2) 测试文件加 t41 锁住这个行为
"""
import os
import sys
import time
import shutil

API = '/home/ubuntu/smart-locker/wx_config_api.py'
TST = '/tmp/wxcfg_step0/tests/test_wx_config.py'
REAL = '--real' in sys.argv
TS = time.strftime('%Y%m%d_%H%M%S')
BK = '/home/ubuntu/smart-locker/backups/guard2_' + TS

OLD = """    done, msg = C.set_active(account_id, bool(active))
    C.log_switch(row['acct_type'], '', row['name'],
                 ('启用' if active else '停用') + '（手动）', 'local-admin')
    return ok({'is_active': 1 if active else 0}, msg) if done else err(msg)"""

NEW = """    done, msg = C.set_active(account_id, bool(active))
    if done:
        # [GUARD-20260913] 只有真的改成功了才记日志：被防呆拒绝的操作不能留下
        # "启用（手动）"这种假记录（上线验证时真踩到过，日志里多出一条没发生过的操作）
        C.log_switch(row['acct_type'], '', row['name'],
                     ('启用' if active else '停用') + '（手动）', 'local-admin')
    return ok({'is_active': 1 if active else 0}, msg) if done else err(msg)"""

T_ANCHOR = """# ------------------------------------------------------------
def main():"""

T_NEW = '''@test
def t41_refused_toggle_does_not_log():
    """被防呆拒绝的"启用"操作，不能留下"启用（手动）"这种假日志"""
    fresh()
    c = _client()
    ph = [r for r in C.list_accounts('oa') if str(r['appid']).startswith('REPLACE_ME')][0]
    before = len(C.get_log(50))
    r = c.post('/api/wx-config/accounts/%d/toggle' % ph['id'], json={'active': True}).get_json()
    assert r['code'] == 400, r
    assert len(C.get_log(50)) == before, '被拒绝的操作留下了日志: %s' % C.get_log(3)
    # 真账号正常启用，还是要记一笔
    good = C.create_account('oa', '真公众号(日志测试)', 'wxoalog0001', secret='s' * 20)
    r2 = c.post('/api/wx-config/accounts/%d/toggle' % good, json={'active': True}).get_json()
    assert r2['code'] == 200, r2
    assert len(C.get_log(50)) == before + 1, '正常操作没记日志: %s' % C.get_log(3)


''' + T_ANCHOR


def main():
    print('=' * 72)
    print('[GUARD2] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    print('=' * 72)
    api = open(API, encoding='utf-8').read()
    tst = open(TST, encoding='utf-8').read()
    targets = [('wx_config_api.py', api, OLD, NEW, '只在成功时记日志'),
               ('test_wx_config.py', tst, T_ANCHOR, T_NEW, '加 t41 用例')]
    for name, src, old, new, tag in targets:
        n = src.count(old)
        if n != 1:
            raise SystemExit('[中止] %s / %s ：锚点出现 %d 次（应恰好 1 次）' % (name, tag, n))
        print('  ✓ %-20s %s' % (name, tag))
    if not REAL:
        print('\n干跑结束：没有写任何文件。')
        return 0
    os.makedirs(BK, exist_ok=True)
    for path, src, old, new, tag in [(API, api, OLD, NEW, 'x'), (TST, tst, T_ANCHOR, T_NEW, 'y')]:
        out = src.replace(old, new, 1)
        compile(out, path, 'exec')
        shutil.copy2(path, os.path.join(BK, os.path.basename(path)))
        tmp = path + '.guard2_tmp' + os.path.splitext(path)[1]
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(out)
        os.replace(tmp, path)
        print('  已写入 %s' % path)
    shutil.copy2(API, os.path.join('/tmp/wxcfg_step0', 'wx_config_api.py'))
    print('已同步到测试目录；备份 %s' % BK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
