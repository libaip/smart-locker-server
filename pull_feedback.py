#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小程序用户反馈拉取 - 存库 (可cron定时)
拉取微信小程序"运维中心-用户反馈列表"(getFeedback)，去重后存入 wx_feedback 表。
反馈类型 type: 1无法打开 2闪退 3卡顿 4黑屏白屏 5死机 6界面错位 7界面加载慢 8其他异常
用法:
  python3 pull_feedback.py            # 拉最近所有页
  python3 pull_feedback.py --days 3   # 只拉最近3天(配合cron)
"""
import sys, os, json, time
sys.path.insert(0, "/home/ubuntu/smart-locker")
import requests

DB_CFG = {"host":"127.0.0.1","port":6432,"user":"locker_admin","password":"locker_pass_2024","dbname":"smart_locker"}
TYPE_MAP = {1:'无法打开',2:'闪退',3:'卡顿',4:'黑屏白屏',5:'死机',6:'界面错位',7:'界面加载慢',8:'其他异常'}

def get_mp_token():
    from config import WX_MP_APP_ID, WX_MP_APP_SECRET
    url = 'https://api.weixin.qq.com/cgi-bin/stable_token'
    payload = dict(grant_type='client_credential', appid=WX_MP_APP_ID, secret=WX_MP_APP_SECRET, force_refresh=False)
    r = requests.post(url, json=payload, timeout=10)
    d = r.json()
    return d.get('access_token'), d

def pull_feedback(token, page=1, num=200):
    url = 'https://api.weixin.qq.com/wxaapi/feedback/list?access_token=%s&page=%s&num=%s' % (token, page, num)
    r = requests.get(url, timeout=15)
    return r.json()

def main():
    days = None
    if len(sys.argv) > 2 and sys.argv[1] == '--days':
        days = int(sys.argv[2])

    token, td = get_mp_token()
    if not token:
        print("token获取失败:", td)
        return

    import psycopg2, psycopg2.extras, datetime
    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()

    all_items = []
    page = 1
    total = None
    while True:
        data = pull_feedback(token, page=page)
        if data.get('errcode') != 0:
            print("拉取失败 errcode=%s errmsg=%s" % (data.get('errcode'), data.get('errmsg')))
            break
        lst = data.get('list', [])
        total = data.get('total_num')
        all_items.extend(lst)
        if not lst:
            break
        # 若指定天数，过滤后判定是否需要继续翻页(时间降序，遇早于筛选则停)
        if days is not None and lst:
            import time as _t
            oldest = min(x.get('create_time') or 0 for x in lst)
            if oldest < (_t.time() - days*86400):
                break
        if len(all_items) >= (total or 0):
            break
        page += 1
        if page > 50:
            break

    inserted = 0
    exist_count = 0
    now = datetime.datetime.now()
    for it in all_items:
        rid = it.get('record_id')
        if not rid:
            continue
        cur.execute("SELECT id FROM wx_feedback WHERE record_id=%s", (rid,))
        if cur.fetchone():
            exist_count += 1
            continue
        cur.execute("""INSERT INTO wx_feedback
            (record_id, create_time, type, phone, openid, nickname, head_url,
             content, media_ids, system_info, raw_json, pulled_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (record_id) DO NOTHING""",
            (rid, it.get('create_time'), it.get('type'), it.get('phone'), it.get('openid'),
             it.get('nickname'), it.get('head_url'), it.get('content'),
             json.dumps(it.get('mediaIds') or [], ensure_ascii=False), it.get('systemInfo'),
             json.dumps(it, ensure_ascii=False), now, now))
        inserted += 1
    conn.commit()
    conn.close()
    print("拉取完成: total=%s, 本批%s条, 新增%s, 已存在%s" % (total, len(all_items), inserted, exist_count))

if __name__ == "__main__":
    main()
