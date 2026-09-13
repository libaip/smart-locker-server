# -*- coding: utf-8 -*-
"""
[FIX-20260913] 公众号 webhook：不再把"事件"当成投诉

事故根因（老板 13667618419 亲测触发）：
  在存包页点"订阅"提示并选"允许" -> 微信把 subscribe_msg_popup_event(accept) 事件发到公众号
  -> routes/webhook.py 把"收到的任何消息/事件"都 INSERT 成一条投诉(source='wechat_mp')
  -> 那条假投诉触发了"投诉自动原路退款" + 把该身份加进"当天投诉白名单"
  -> 当天结束订单被直接退款（不走余额提现）

同一原因造成的其它现象：
  - 用户点公众号菜单(view_miniprogram)也被记成投诉，投诉表里出现大量
    content='pages/wallet/wallet' / 'pages/mine/mine' 这种"投诉"（近 7 天 837 条事件）
  - 投诉数虚高（近 7 天公众号转来的 550 条里，真正用户主动发文字的只有 51 条）

改法：只有"用户主动发的消息"才算投诉/留言；MsgType=event 的一律只存 wx_oa_messages
当记录，不建投诉工单、不触发自动退款、不加白名单。
"""
import os
import sys
import time
import shutil
import py_compile

WK = '/home/ubuntu/smart-locker/routes/webhook.py'
REAL = '--real' in sys.argv
BK = '/home/ubuntu/smart-locker/backups/whfix_' + time.strftime('%Y%m%d_%H%M%S')

OLD = """            if _phone and _msg_id_val:
                _cur_msg.execute("SELECT id FROM complaints WHERE user_phone = %s AND status = '0' ORDER BY id DESC LIMIT 1", (_phone,))"""

NEW = """            # [FIX-20260913] 只有"用户主动发的消息"(文字/图片/语音/视频/位置/链接)才算投诉/留言。
            #   以前 MsgType=event 也当投诉, 实测事故: 用户在存包页点"订阅"并允许 ->
            #   生成一条假投诉 -> 触发"投诉自动原路退款" + 被加进"当天投诉白名单"
            #   -> 当天结束订单被直接退款(不走余额提现)。点公众号菜单(view_miniprogram)
            #   同理, 也是假投诉, 会让投诉数虚高。
            _is_user_msg = (msg_type or '').strip().lower() in (
                'text', 'image', 'voice', 'video', 'shortvideo', 'location', 'link')
            if _phone and _msg_id_val and _is_user_msg:
                _cur_msg.execute("SELECT id FROM complaints WHERE user_phone = %s AND status = '0' ORDER BY id DESC LIMIT 1", (_phone,))"""


def main():
    print('=' * 70)
    print('[webhook 修复] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    src = open(WK, encoding='utf-8').read()
    n = src.count(OLD)
    print('  锚点命中 %d/1' % n)
    if n != 1:
        raise SystemExit('[中止] 锚点数量不对，不写')
    if '_is_user_msg' in src:
        print('  看起来已经改过了，跳过')
        return 0
    out = src.replace(OLD, NEW, 1)
    if not REAL:
        import difflib
        d = list(difflib.unified_diff(src.split('\n'), out.split('\n'), 'webhook.py(改前)', 'webhook.py(改后)', n=3, lineterm=''))
        for ln in d:
            print('   ' + ln)
        print('\n干跑结束：没有写文件。')
        return 0
    tmp = WK + '.tmp.py'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(out)
    try:
        py_compile.compile(tmp, cfile=tmp + 'c', doraise=True)
        os.remove(tmp + 'c')
    except Exception as e:
        os.remove(tmp)
        raise SystemExit('[中止] 语法检查失败：%s' % e)
    os.makedirs(BK, exist_ok=True)
    shutil.copy2(WK, os.path.join(BK, 'webhook.py'))
    os.replace(tmp, WK)
    print('\n✅ 已写入（备份 %s/webhook.py）' % BK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
