# -*- coding: utf-8 -*-
"""patch_mobile_sidebar.py - 手机端侧边栏改为常驻显示，和PC端一致"""
import re

HTML_FILE = '/home/ubuntu/smart-locker/static/admin-v2.html'

with open(HTML_FILE, 'r', encoding='utf-8') as f:
    html = f.read()

# 备份
with open(HTML_FILE + '.bak5', 'w', encoding='utf-8') as f:
    f.write(html)

changes = 0

# 1. 删除media query中的sidebar隐藏/抽屉逻辑
old_css = '''.sidebar{position:fixed;z-index:100;height:100vh;transform:translateX(-100%);transition:transform .3s ease}
.sidebar.mobile-open{transform:translateX(0)}'''
if old_css in html:
    html = html.replace(old_css, '/* sidebar always visible like desktop */')
    changes += 1
    print('[1] Removed mobile sidebar hide CSS')
else:
    print('[1] SKIP - not found')

# 2. toggle按钮统一用isShrink逻辑
old_toggle = '<span class="toggle" @click="isMobile%ssidebarOpen=!sidebarOpen:isShrink=!isShrink">{{isMobile%s(sidebarOpen%s\'\u2715\':\'\u2630\'):(isShrink%s\'\u2630\':\'\u2715\')}}</span>'
new_toggle = '<span class="toggle" @click="isShrink=!isShrink">{{isShrink%s\'\u2630\':\'\u2715\'}}</span>'
if old_toggle in html:
    html = html.replace(old_toggle, new_toggle)
    changes += 1
    print('[2] Unified toggle button')
else:
    print('[2] SKIP - not found')

# 3. 删除overlay遮罩层
old_overlay = '<div v-if="isMobile&&sidebarOpen" class="sidebar-overlay" @click="sidebarOpen=false"></div>\n'
if old_overlay in html:
    html = html.replace(old_overlay, '')
    changes += 1
    print('[3] Removed overlay')
else:
    print('[3] SKIP - not found')

# 4. content区域去掉点击关闭逻辑
old_content = '<div class="content" @click="isMobile&&sidebarOpen&&(sidebarOpen=false)">'
new_content = '<div class="content">'
if old_content in html:
    html = html.replace(old_content, new_content)
    changes += 1
    print('[4] Removed content click-to-close')
else:
    print('[4] SKIP - not found')

# 5. sidebar组件去掉mobile-open class，统一用shrink
old_sidebar_class = '''<div class="sidebar" :class="{shrink:!$parent.isMobile&&$parent.isShrink,'mobile-open':$parent.isMobile&&$parent.sidebarOpen}">'''
new_sidebar_class = '''<div class="sidebar" :class="{shrink:$parent.isShrink}">'''
if old_sidebar_class in html:
    html = html.replace(old_sidebar_class, new_sidebar_class)
    changes += 1
    print('[5] Simplified sidebar class')
else:
    print('[5] SKIP - not found')

# 6. 菜单项点击去掉关闭侧边栏逻辑
old_menu_click = '$parent.isMobile&&($parent.sidebarOpen=false)'
if old_menu_click in html:
    html = html.replace(';$parent.isMobile&&($parent.sidebarOpen=false)', '')
    changes += 1
    print('[6] Removed menu click close')
else:
    print('[6] SKIP - not found')

with open(HTML_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'\nDone! {changes} changes applied. File size: {len(html)} bytes')
print(f'sidebarOverlay remaining: {html.count("sidebar-overlay")}')
print(f'sidebarOpen remaining: {html.count("sidebarOpen")}')
