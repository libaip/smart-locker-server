# -*- coding: utf-8 -*-
"""后台页面加上 openid 识别前缀：表格显示 + 表单可填 + 保存带上"""
import os
import sys
import time
import shutil
import subprocess
import py_compile

JS = '/home/ubuntu/smart-locker/static/wx_accounts_page.js'
REAL = '--real' in sys.argv
BK = '/home/ubuntu/smart-locker/backups/pagepfx_' + time.strftime('%Y%m%d_%H%M%S')

E = [
    ('表头加一列',
     '      <th>ID</th><th>名称</th><th>appid</th><th>主体</th><th>优先级</th><th>状态</th>\n',
     '      <th>ID</th><th>名称</th><th>appid</th><th>识别前缀</th><th>主体</th><th>优先级</th><th>状态</th>\n',
     1),
    ('行里显示前缀',
     'style="margin-left:4px">未配好</span></td>',
     'style="margin-left:4px">未配好</span></td>\n        <td class="wx-mono" title="判断某个 openid 是不是这个号下面的（换号后要改这里）">{{a.openid_prefix||\'-\'}}</td>',
     1),
    ('空行 colspan',
     'colspan="10" class="empty-row">暂无账号',
     'colspan="11" class="empty-row">暂无账号',
     1),
    ('表单加一项',
     '<div class="form-group"><label>AppSecret</label><input v-model="form.secret" placeholder="换号必填"></div>',
     '<div class="form-group"><label>AppSecret</label><input v-model="form.secret" placeholder="换号必填"></div>\n'
     '          <div class="form-group"><label>openid 识别前缀（换号必填）</label>'
     '<input v-model="form.openid_prefix" placeholder="例：ooTcRx / oLhbm2 —— 新号第一次授权后，看日志或库里的 openid 前 6 位"></div>',
     1),
    ('openAdd 表单初值',
     "priority: 100, token: '', note: '' };",
     "priority: 100, token: '', note: '', openid_prefix: '' };",
     1),
    ('保存时带上前缀',
     '        token: f.token, note: f.note\n',
     '        token: f.token, note: f.note, openid_prefix: f.openid_prefix\n',
     1),
]


def main():
    print('=' * 70)
    print('[页面] 模式：%s' % ('真改(--real)' if REAL else '干跑'))
    src = open(JS, encoding='utf-8').read()
    out = src
    for tag, old, new, cnt in E:
        n = out.count(old)
        print('  %s %-18s 命中 %d/%d' % ('✓' if n == cnt else '✗', tag, n, cnt))
        if n != cnt:
            raise SystemExit('[中止] %s 锚点数量不对（%d/%d）' % (tag, n, cnt))
        out = out.replace(old, new)
    if not REAL:
        print('\n干跑结束：没有写文件。')
        return 0
    tmp = JS + '.tmp.js'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(out)
    r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
    if r.returncode != 0:
        os.remove(tmp)
        raise SystemExit('[中止] JS 语法检查失败：%s' % (r.stderr or r.stdout)[:300])
    os.makedirs(BK, exist_ok=True)
    shutil.copy2(JS, os.path.join(BK, 'wx_accounts_page.js'))
    os.replace(tmp, JS)
    print('\n✅ 已写入（备份 %s）' % BK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
