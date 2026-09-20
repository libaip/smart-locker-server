# -*- coding: utf-8 -*-
"""S373：公众号图文卡片的配图（动态生成，带中文）。
字体：static/fonts/cjk.ttc（文泉驿正黑，apt 只下载解包取得，未装系统包）
"""
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'fonts', 'cjk.ttc')
_FALLBACK_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
_CACHE = {}
_CACHE_MAX = 200


def _font(size):
    p = FONT if os.path.exists(FONT) else _FALLBACK_FONT
    return ImageFont.truetype(p, size)


def _clean(s, n=16):
    s = re.sub(r'[\x00-\x1f\x7f]', '', str(s or '')).strip()
    return s[:n]


def _gradient(w, h, c1, c2):
    """竖向渐变（按 1 像素宽的条生成再放大，避免逐像素慢）"""
    strip = Image.new('RGB', (1, h))
    px = strip.load()
    for y in range(h):
        t = y / float(max(1, h - 1))
        px[0, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return strip.resize((w, h))


def _cache_put(key, data):
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = data
    return data


def banner(site='', title='自助存取包', sub='点击下方 · 存包', w=900, h=500):
    """首条图文的大图。返回 JPEG 字节。"""
    site = _clean(site, 16)
    key = ('b', site, title, sub, w, h)
    if key in _CACHE:
        return _CACHE[key]
    im = _gradient(w, h, (48, 130, 255), (18, 82, 205))
    d = ImageDraw.Draw(im)
    f1 = _font(84)
    f2 = _font(44)
    f3 = _font(38)
    tw = d.textlength(title, font=f1)
    d.text(((w - tw) / 2.0, int(h * 0.26)), title, font=f1, fill=(255, 255, 255))
    sw = d.textlength(sub, font=f2)
    d.text(((w - sw) / 2.0, int(h * 0.50)), sub, font=f2, fill=(226, 238, 255))
    if site:
        d.text((44, h - 84), site, font=f3, fill=(206, 224, 255))
    b = io.BytesIO()
    im.save(b, 'JPEG', quality=88)
    return _cache_put(key, b.getvalue())


def icon(ch='存', w=200, h=200):
    """次条的小缩略图（存/取）。返回 PNG 字节。"""
    ch = (ch or '存')[:1]
    if ch not in ('存', '取'):
        ch = '存'
    color = (43, 181, 129) if ch == '存' else (43, 124, 255)
    key = ('i', ch, w, h)
    if key in _CACHE:
        return _CACHE[key]
    im = Image.new('RGB', (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([6, 6, w - 6, h - 6], radius=30, fill=color)
    f = _font(124)
    tb = d.textbbox((0, 0), ch, font=f)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]
    d.text(((w - tw) / 2.0 - tb[0], (h - th) / 2.0 - tb[1]), ch, font=f, fill=(255, 255, 255))
    b = io.BytesIO()
    im.save(b, 'PNG')
    return _cache_put(key, b.getvalue())
