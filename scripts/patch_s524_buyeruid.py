#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[S524-20260921] 支付宝【付款人 uid】补采补丁（只动支付宝，微信那套一行不碰）

背景（本次只读调研的实测结论）
------------------------------------------------------------------
1. 生产 routes/payment.py 里 `/pay/notify/alipay` 已经（S519）在"验签路径"读
   `params['buyer_id']`、"查单路径"读 `q['buyer_user_id']`，写 `orders.alipay_pay_uid`。
2. 订单 137354（order_no=20260921212049784051）21:22:38 的支付结果**是走异步通知**到位的
   （journal 明确 `[支付宝回调] ... 校验方式=sign`），文件 md5 = adbbea97...（=S519 上线后的版本），
   但全天 `订单桥接` 日志出现 0 次 → 说明 `_buyer_uid` 取到了空字符串。
3. 原因是支付宝已经切到 **openid 模式**：本应用（appId 2021006197675152）调
   `alipay.trade.query` 实测返回的是 `buyer_open_id`（022cEZy5IF6...48 位），
   **完全没有 `buyer_user_id` / `buyer_id` 字段**（本次只读探测结果，
   见 probe_alipay_uid.py 的输出）。异步通知同理，带的是 `buyer_open_id`。
   老代码只认 buyer_id / buyer_user_id，所以永远取空。
4. 另外代码里其实**不存在**支付宝的"主动查单/轮询"路径：
   · `/order/<id>/pay-status`（routes/user.py:2550）只在 `ch_type2 == 'wechat'` 时进；
   · `/store/pay`（routes/user.py:1522）调 `wxpay.order_query(...)`，而支付宝实例
     （alipay.AlipayClient）只有 `query()` 没有 `order_query()` → 支付宝单会抛
     AttributeError 被兜底成 500，从来没成功置过"已支付"；
   · app.py 的超时清理线程也只在 `ch_type == 'wechat'` 时查单。

本补丁做什么（3 处，全部在 routes/payment.py 的 alipay_pay_notify 内）
------------------------------------------------------------------
  A1  验签路径：`_buyer_uid` 取值加上 `buyer_open_id`（放最前，兼容老的 buyer_id）；
  A2  查单路径：同样加上 `buyer_open_id`；
  A3  兜底补采：在"已完成验签/查单且金额已核对、且 DB 行锁尚未持有"的位置，
      如果 `_buyer_uid` 仍为空，就对这笔 `out_trade_no` 做一次
      **`alipay.trade.query`（只读、幂等、不改任何状态）**，把 `buyer_open_id`
      （或老的 buyer_user_id / buyer_id）补出来。
  写库仍然是 S519 那段：`UPDATE orders SET alipay_pay_uid=%s WHERE id=%s AND COALESCE(alipay_pay_uid,'')=''`
  —— **只在空时写；任何异常只告警，绝不影响"订单已支付"、开门、记账**。

不做的事（明确边界）
------------------------------------------------------------------
  · 不碰微信任何代码/任何列；只写 `orders.alipay_pay_uid` 这一个支付宝专用列；
  · 不给支付宝新增"主动查单置为已支付"的逻辑（那是另一个变更，见方案文档 §6）；
  · 不改 `/store/pay`、`/order/<id>/pay-status`、app.py 清理线程；
  · 不改 `/alipay/login` 的 `user_id`→`open_id`（那是小程序侧的问题，见方案文档 §6）。

用法
------------------------------------------------------------------
  # 1) 干跑（默认，不写任何文件，只打印锚点命中数与 md5）
  python patch_s524_buyeruid.py <routes/payment.py 路径>

  # 2) 真打（会先 shutil.copy2 备份，再写盘）
  python patch_s524_buyeruid.py <routes/payment.py 路径> --apply

退出码：0=成功；2=锚点不唯一/文件已打过补丁/语法校验失败（**不写任何文件**）。
"""
from __future__ import print_function

import hashlib
import os
import py_compile
import shutil
import sys
import time

# 生产 175 上本次调研时的 routes/payment.py md5（= S519 上线后版本）
PROD_MD5_S519 = 'adbbea97b4ee97705cc786e677f85ff3'

MARK = '[S524]'

# ----------------------------------------------------------------------------
# A1：验签路径取付款人 uid —— 补 buyer_open_id
# ----------------------------------------------------------------------------
A1_OLD = """        # [S519] 订单桥接的另一半：付款人的支付宝 uid。
        #   验签路径 -> 通知里的 buyer_id；查单路径 -> 查单结果里的 buyer_user_id（下面覆盖）
        _buyer_uid = str(params.get('buyer_id') or params.get('buyer_user_id') or '').strip()
"""

A1_NEW = """        # [S519] 订单桥接的另一半：付款人的支付宝 uid。
        #   验签路径 -> 通知里的 buyer_id；查单路径 -> 查单结果里的 buyer_user_id（下面覆盖）
        # [S524] 补 buyer_open_id：本应用已切到支付宝 openid 模式（实测 alipay.trade.query
        #   只返回 buyer_open_id、不返回 buyer_user_id），老代码只看 buyer_id 必然取空。
        #   顺序：buyer_open_id 优先，老的 buyer_id / buyer_user_id 保留兼容。
        _buyer_uid = str(params.get('buyer_open_id') or params.get('buyer_id')
                         or params.get('buyer_user_id') or '').strip()
"""

# ----------------------------------------------------------------------------
# A2：查单路径取付款人 uid —— 补 buyer_open_id
# ----------------------------------------------------------------------------
A2_OLD = """                _buyer_uid = str(q.get('buyer_user_id') or q.get('buyer_id') or _buyer_uid or '').strip()
"""

A2_NEW = """                # [S524] 查单结果同样优先取 buyer_open_id（本应用 openid 模式）
                _buyer_uid = str(q.get('buyer_open_id') or q.get('buyer_user_id')
                                 or q.get('buyer_id') or _buyer_uid or '').strip()
"""

# ----------------------------------------------------------------------------
# A3：兜底补采 —— 已确认支付成功、金额已核对、尚未持有订单行锁时，只读查单补 uid
#     （紧贴在 `trade_no = str(params.get('trade_no') or '')` 之前插入；
#       此处上方已 return 掉所有"没确认成功"的分支，下方才拿 FOR UPDATE 行锁）
# ----------------------------------------------------------------------------
A3_OLD = """        trade_no = str(params.get('trade_no') or '')
"""

A3_NEW = """        # [S524] 付款人 uid 兜底补采（只读、幂等、不改任何状态）：
        #   上面两条路都可能取空 —— 验签路径的通知里带的是 buyer_open_id（老代码只认
        #   buyer_id），查单路径老代码只认 buyer_user_id。这里在"已确认支付成功 + 金额已
        #   核对 + 还没拿 FOR UPDATE 行锁"的位置补一次 alipay.trade.query，把 uid 补出来。
        #   任何异常只告警：绝不影响订单已支付 / 开门 / 记账 / 微信那套（本函数只服务支付宝）。
        if not _buyer_uid:
            try:
                _q2 = client.query(out_trade_no=out_trade_no)
                if str(_q2.get('code')) == '10000':
                    _buyer_uid = str(_q2.get('buyer_open_id') or _q2.get('buyer_user_id')
                                     or _q2.get('buyer_id') or '').strip()
                    if _buyer_uid:
                        logger.info('[S524] 付款人 uid 补采(只读查单)成功: order_no=%s len=%s',
                                    out_trade_no, len(_buyer_uid))
                    else:
                        logger.warning('[S524] 只读查单也没拿到付款人 uid: order_no=%s', out_trade_no)
                else:
                    logger.warning('[S524] 只读查单未成功: order_no=%s code=%s sub_code=%s',
                                   out_trade_no, _q2.get('code'), _q2.get('sub_code'))
            except Exception as _qe:
                logger.warning('[S524] 付款人 uid 补查异常(不影响订单已支付): order_no=%s err=%s',
                               out_trade_no, _qe)
        trade_no = str(params.get('trade_no') or '')
"""

REPLACEMENTS = [
    ('A1 验签路径取 buyer_open_id', A1_OLD, A1_NEW),
    ('A2 查单路径取 buyer_open_id', A2_OLD, A2_NEW),
    ('A3 只读查单兜底补采', A3_OLD, A3_NEW),
]


def md5_of(path):
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def md5_text(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    apply = '--apply' in sys.argv[2:]

    if not os.path.isfile(path):
        print('[S524][FAIL] 文件不存在: %s' % path)
        return 2

    # 必须按要求用 newline='' 读取，保证原样读入、原样写回（不擅自改行尾）
    with open(path, encoding='utf-8', newline='') as f:
        src = f.read()

    before_md5 = md5_text(src)
    print('[S524] 目标文件        : %s' % path)
    print('[S524] 改前 md5        : %s' % before_md5)
    print('[S524] 改前 md5(文件)  : %s' % md5_of(path))
    if before_md5 != md5_of(path):
        print('[S524][WARN] 文本 md5 与文件 md5 不一致（可能存在编码/换行差异），继续但请留意')
    if before_md5 == PROD_MD5_S519:
        print('[S524] 与生产 175 本次调研版本(S519)一致 ✅')
    else:
        print('[S524][WARN] 与已知生产版本 %s 不一致，请确认这是不是最新的 payment.py'
              % PROD_MD5_S519)

    if 'alipay_pay_uid' not in src:
        print('[S524][FAIL] 文件里没有 alipay_pay_uid —— 说明 S519 还没上线，先上 S519')
        return 2
    if 'buyer_open_id' in src:
        print('[S524][FAIL] 文件里已经出现 buyer_open_id —— 本补丁可能已打过，拒绝重复执行')
        return 2

    # ---- 锚点唯一性校验：任何一处不唯一 => 一个字节都不写 ----
    out = src
    bad = []
    print('[S524] ---- 锚点校验 ----')
    for tag, old, new in REPLACEMENTS:
        n = out.count(old)
        print('[S524]   %-28s count=%d (必须=1)' % (tag, n))
        if n != 1:
            bad.append('%s 锚点命中 %d 次（应为 1）' % (tag, n))
            continue
        out = out.replace(old, new, 1)
    if bad:
        print('[S524][FAIL] 锚点校验不通过，未写任何文件：')
        for b in bad:
            print('[S524]        - %s' % b)
        return 2

    after_md5 = md5_text(out)
    print('[S524] ---- 结果 ----')
    print('[S524] 改后 md5        : %s' % after_md5)
    print('[S524] 新增 %s 注释处数: %d' % (MARK, out.count(MARK)))

    if not apply:
        print('[S524] 干跑模式（未加 --apply）：不写任何文件。确认无误后加 --apply 真打。')
        return 0

    # ---- 先写临时文件 + 语法校验，通过后才覆盖，并留 copy2 备份 ----
    tmp = path + '.s524tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(out)
    tmp_md5 = md5_of(tmp)
    if tmp_md5 != after_md5:
        print('[S524][FAIL] 临时文件 md5 与预期不符，放弃（未覆盖原文件）')
        os.remove(tmp)
        return 2
    try:
        py_compile.compile(tmp, cfile=tmp + '.pyc', doraise=True)
        print('[S524] py_compile 校验通过 ✅')
    except Exception as e:
        print('[S524][FAIL] 语法校验失败，未覆盖原文件: %s' % e)
        os.remove(tmp)
        return 2

    stamp = time.strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(path)), 'backups')
    if not os.path.isdir(backup_dir):
        backup_dir = os.path.dirname(os.path.abspath(path))
    backup = os.path.join(backup_dir, 'payment.py.s524bak.%s' % stamp)
    shutil.copy2(path, backup)
    print('[S524] 已备份(copy2)  : %s' % backup)

    shutil.move(tmp, path)
    try:
        os.remove(tmp + '.pyc')
    except Exception:
        pass

    print('[S524] 写入完成        : %s' % path)
    print('[S524] 回读 md5        : %s' % md5_of(path))
    print('[S524] 期望 md5        : %s' % after_md5)
    print('[S524] 回滚命令        : cp -a %s %s' % (backup, path))
    return 0 if md5_of(path) == after_md5 else 2


if __name__ == '__main__':
    sys.exit(main())
