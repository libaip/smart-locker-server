# -*- coding: utf-8 -*-
"""
S508-20260921 小程序客服自动回复文案/规则调整（按老板最新要求）
==============================================================
老板要求：
  1) 用户进入客服会话 → 自动发：
       你好，请描述你的问题，这边加急帮你处理。
       1. 怎么退款
       2. 退款未到账
       3) 提现显示异常
       4. 客服电话4006981080
  2) 选项 1/2/3（以及"退款/未到账/无法到账"等关键词）→ 统一回复：
       "请提供使用时的手机号或订单号，这边加急帮您核实处理"
  3) 选 4 / 问到客服电话 → 回客服电话信息
本补丁只替换 routes/user.py 里 S507 新增的那两个常量块与 _mp_auto_reply() 函数体，
不触碰其它任何逻辑。
"""
import hashlib
import py_compile
import shutil

P = 'routes/user.py'
src = open(P, encoding='utf-8').read()
before = hashlib.md5(src.encode('utf-8')).hexdigest()
assert 'S507-20260921' in src and '_MP_MENU = (' in src and '_MP_RULES' in src, '找不到 S507 补丁痕迹，中止'
assert 'S508-20260921' not in src, '已打过 S508 补丁，中止'

NEW_BLOCK = '''_MP_MENU = ('你好，请描述你的问题，这边加急帮你处理。\\n'
            '1. 怎么退款\\n'
            '2. 退款未到账\\n'
            '3. 提现显示异常\\n'
            '4. 客服电话4006981080')

_MP_NEED_INFO = ('好的，请提供使用时的手机号或订单号，这边加急帮您核实处理。\\n'
                 '（也可以直接拨打客服电话 4006981080，08:30-21:00）')

_MP_NEED_INFO_KEYS = ('怎么退款', '退款', '未到账', '没到账', '无法到账', '不到账', '没退',
                      '退钱', '提现', '异常', '没收到', '钱没', '退一下', '退给我')

_MP_PHONE = ('客服电话：4006981080（08:30-21:00）\\n'
             '如需我们主动联系您，把手机号或订单号发在这里也可以。')

_MP_GOT_INFO = ('已收到，这边加急为您核实处理，请稍候。\\n'
                '如有补充说明，直接回复即可。')

_MP_FALLBACK = ('请描述一下您的具体问题，或回复数字选择：\\n'
                '1. 怎么退款\\n'
                '2. 退款未到账\\n'
                '3. 提现显示异常\\n'
                '4. 客服电话4006981080')
'''

NEW_FUNC = '''def _mp_auto_reply(text):
    """[S508] 按老板定稿的规则回复：编号/关键词 → 请他给手机号或订单号"""
    t = (text or '').strip()
    if not t:
        return _MP_MENU
    # 1) 已经给了手机号/订单号（≥8 位数字）→ 直接说"已收到，加急处理"
    digits = ''.join(ch for ch in t if ch.isdigit())
    if len(digits) >= 8:
        return _MP_GOT_INFO
    # 2) 选项 1/2/3 或 退款/未到账/提现异常 等关键词 → 统一要手机号/订单号
    if t in ('1', '2', '3') or any(k in t for k in _MP_NEED_INFO_KEYS):
        return _MP_NEED_INFO
    # 3) 选项 4 或问客服电话
    if t == '4' or '客服电话' in t or '电话' in t:
        return _MP_PHONE
    return _MP_FALLBACK
'''

# ---- 替换块 A：_MP_MENU ... 到 def _mp_push_token 之前 ----
a_start = src.index('_MP_MENU = (')
a_end = src.index('def _mp_push_token():')
region_a = src[a_start:a_end]
assert '_MP_RULES' in region_a, '块 A 里没有 _MP_RULES，锚点可能不对'
src2 = src[:a_start] + NEW_BLOCK + '\n\n' + src[a_end:]

# ---- 替换块 B：_mp_auto_reply 函数体 ----
b_start = src2.index('def _mp_auto_reply(text):')
b_end = src2.index('def _mp_kf_send(')
src3 = src2[:b_start] + NEW_FUNC + '\n\n' + src2[b_end:]

src3 = src3.replace('[S507-20260921] 小程序客服自动回复（消息推送 webhook）',
                    '[S507/S508-20260921] 小程序客服自动回复（消息推送 webhook）', 1)

shutil.copy2(P, P + '.bak_s508')
open(P, 'w', encoding='utf-8').write(src3)
after = hashlib.md5(src3.encode('utf-8')).hexdigest()
py_compile.compile(P, doraise=True)

print('文件: %s' % P)
print('改前 md5: %s' % before)
print('改后 md5: %s' % after)
print('py_compile: OK')
print('检查: _MP_RULES 残留=%d（应为0）  _MP_NEED_INFO=%d  _MP_GOT_INFO=%d'
      % (src3.count('_MP_RULES'), src3.count('_MP_NEED_INFO ='), src3.count('_MP_GOT_INFO =')))
