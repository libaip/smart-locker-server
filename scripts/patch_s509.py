# -*- coding: utf-8 -*-
"""
S509-20260921 客服自动回复：新增「5. 柜门打不开」
================================================
老板要求：柜门打不开 → 回复「请联系客服4006981080，需要退款请提供使用时的手机号或订单号」
做法：只替换 S507/S508 新增的常量块与 _mp_auto_reply()，不触碰其它逻辑。
"""
import hashlib
import py_compile
import shutil

P = 'routes/user.py'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert '_MP_NEED_INFO_KEYS' in src and '_MP_MENU = (' in src, '找不到 S508 补丁痕迹，中止'
assert 'S509-20260921' not in src, '已打过 S509 补丁，中止'

NEW_BLOCK = '''_MP_MENU = ('你好，请描述你的问题，这边加急帮你处理。\\n'
            '1. 怎么退款\\n'
            '2. 退款未到账\\n'
            '3. 提现显示异常\\n'
            '4. 客服电话4006981080\\n'
            '5. 柜门打不开')

_MP_NEED_INFO = ('好的，请提供使用时的手机号或订单号，这边加急帮您核实处理。\\n'
                 '（也可以直接拨打客服电话 4006981080，08:30-21:00）')

_MP_NEED_INFO_KEYS = ('怎么退款', '退款', '未到账', '没到账', '无法到账', '不到账', '没退',
                      '退钱', '提现', '异常', '没收到', '钱没', '退一下', '退给我')

_MP_DOOR = '请联系客服4006981080，需要退款请提供使用时的手机号或订单号'

_MP_DOOR_KEYS = ('柜门', '打不开', '开不了', '门开', '门没开', '没弹开', '弹不开', '卡住',
                 '故障', '门锁', '锁住')

_MP_PHONE = ('客服电话：4006981080（08:30-21:00）\\n'
             '如需我们主动联系您，把手机号或订单号发在这里也可以。')

_MP_GOT_INFO = ('已收到，这边加急为您核实处理，请稍候。\\n'
                '如有补充说明，直接回复即可。')

_MP_FALLBACK = ('请描述一下您的具体问题，或回复数字选择：\\n'
                '1. 怎么退款\\n'
                '2. 退款未到账\\n'
                '3. 提现显示异常\\n'
                '4. 客服电话4006981080\\n'
                '5. 柜门打不开')
'''

NEW_FUNC = '''def _mp_auto_reply(text):
    """[S509] 编号/关键词自动回复（含「柜门打不开」）"""
    t = (text or '').strip()
    if not t:
        return _MP_MENU
    # 1) 已给手机号/订单号（≥8 位数字）
    digits = ''.join(ch for ch in t if ch.isdigit())
    if len(digits) >= 8:
        return _MP_GOT_INFO
    # 2) 柜门打不开（放在"退款"之前：这条回复本身已含退款指引）
    if t == '5' or any(k in t for k in _MP_DOOR_KEYS):
        return _MP_DOOR
    # 3) 选项 1/2/3 或退款/未到账/提现等关键词
    if t in ('1', '2', '3') or any(k in t for k in _MP_NEED_INFO_KEYS):
        return _MP_NEED_INFO
    # 4) 选项 4 或问客服电话
    if t == '4' or '客服电话' in t or '电话' in t:
        return _MP_PHONE
    return _MP_FALLBACK
'''

a_start = src.index('_MP_MENU = (')
a_end = src.index('def _mp_push_token():')
src2 = src[:a_start] + NEW_BLOCK + '\n\n' + src[a_end:]

b_start = src2.index('def _mp_auto_reply(text):')
b_end = src2.index('def _mp_kf_send(')
src3 = src2[:b_start] + NEW_FUNC + '\n\n' + src2[b_end:]

src3 = src3.replace('def _mp_auto_reply(text):\n    """[S508]',
                    'def _mp_auto_reply(text):\n    """[S509]', 1)

shutil.copy2(P, P + '.bak_s509')
open(P, 'w', encoding='utf-8').write(src3)
after = hashlib.md5(src3.encode('utf-8')).hexdigest()
py_compile.compile(P, doraise=True)

print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('检查: _MP_DOOR=%d  _MP_DOOR_KEYS=%d  菜单含柜门=%d'
      % (src3.count('_MP_DOOR ='), src3.count('_MP_DOOR_KEYS ='), src3.count('5. 柜门打不开')))
