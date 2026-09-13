# -*- coding: utf-8 -*-
"""给测试文件加两个 openid 前缀的用例，并把新代码同步到测试目录"""
import io
import os
import shutil
import sys

TST = '/tmp/wxcfg_step0/tests/test_wx_config.py'
ANCHOR = """# ------------------------------------------------------------
def main():"""

NEW = '''@test
def t42_openid_prefix_follows_account():
    """openid 前缀要跟着"当前生效账号"走：换号后不用改代码（这是本次改造的目的）"""
    fresh()
    assert C.mp_openid_prefix() == 'ooTcRx', C.mp_openid_prefix()
    assert C.oa_openid_prefix() == 'oLhbm2', C.oa_openid_prefix()
    # 造一个"新小程序"，给它一个不同的前缀，切成生效
    nid = C.create_account('mp', '新号(前缀测试)', 'wxnewpfx0001', secret='s' * 20,
                           openid_prefix='oNEW01')
    ok1, msg1 = C.switch_to('mp', nid, reason='前缀测试')
    assert ok1, msg1
    assert C.mp_openid_prefix() == 'oNEW01', '前缀没跟着换：%s' % C.mp_openid_prefix()
    # 公众号的前缀不受影响
    assert C.oa_openid_prefix() == 'oLhbm2'
    # 切回去，前缀也要跟着回去
    back = [a for a in C.list_accounts('mp') if a['appid'] == 'wxcabd4cbdb3096c4b'][0]
    ok2, msg2 = C.switch_to('mp', back['id'], reason='前缀测试切回')
    assert ok2, msg2
    assert C.mp_openid_prefix() == 'ooTcRx', C.mp_openid_prefix()
    print('      （切换后前缀变化：ooTcRx -> oNEW01 -> ooTcRx）')


@test
def t43_openid_prefix_empty_falls_back():
    """前缀字段是空的（老库没这列 / 没配）→ 回落默认值，行为与改造前完全一致"""
    fresh()
    act = [a for a in C.list_accounts('mp') if a['is_active']][0]
    C.update_account(act['id'], openid_prefix='')
    C.clear_cache()
    assert C.mp_openid_prefix() == 'ooTcRx', '空值时应回落 ooTcRx，实得 %s' % C.mp_openid_prefix()
    oact = [a for a in C.list_accounts('oa') if a['is_active']][0]
    C.update_account(oact['id'], openid_prefix='')
    C.clear_cache()
    assert C.oa_openid_prefix() == 'oLhbm2', '空值时应回落 oLhbm2，实得 %s' % C.oa_openid_prefix()
    # 库里读不到时（bind 一个会炸的连接）也必须回落
    C.bind(lambda: (_ for _ in ()).throw(RuntimeError('模拟库挂了')))
    C.clear_cache()
    assert C.mp_openid_prefix() == 'ooTcRx', C.mp_openid_prefix()
    assert C.oa_openid_prefix() == 'oLhbm2', C.oa_openid_prefix()


''' + ANCHOR


def main():
    tst = io.open(TST, encoding='utf-8').read()
    if 't42_openid_prefix_follows_account' in tst:
        print('已经加过了，跳过')
    else:
        n = tst.count(ANCHOR)
        assert n == 1, '锚点数量不对: %d' % n
        io.open(TST, 'w', encoding='utf-8').write(tst.replace(ANCHOR, NEW, 1))
        print('已加 t42 / t43')
    for name in ('wx_config.py', 'wx_config_api.py'):
        shutil.copy2('/home/ubuntu/smart-locker/' + name, '/tmp/wxcfg_step0/' + name)
    print('已同步 wx_config.py / wx_config_api.py 到测试目录')
    return 0


if __name__ == '__main__':
    sys.exit(main())
