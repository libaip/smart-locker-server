#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防崩溃监控脚本（2026-08-18 添加）
每分钟由 crontab 调用，监控昨天(8-17)全站 499 崩溃的前兆指标：
  1. idle in transaction 卡死连接数（>=3 告警，>=10 立即杀）
  2. 未授予锁数量（>=5 告警）
  3. 最近1分钟 499/5xx 数量（>=20 告警，>=100 严重）
  4. gunicorn worker 数异常（<7 告警）
  5. 健康检查失败（连续2次告警）
发现异常：记录日志 + PushPlus 推送告警。每类指标 10 分钟内最多告警 1 次，避免刷屏。
"""
import os
import sys
import time
import subprocess
import logging
import socket
import glob
import json

sys.path.insert(0, '/home/ubuntu/smart-locker')

logging.basicConfig(
    filename='/home/ubuntu/smart-locker/logs/crash_guard.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# 告警节流：每类指标 10 分钟最多 1 次
_LAST_ALERT = {}
_THROTTLE = 600

# [FIX-20260912] 节流状态必须落盘!
#   本脚本由 crontab 每分钟拉起一个【全新进程】, 内存里的 _LAST_ALERT 每次都从空开始,
#   所以原来的节流实际上完全没生效 —— 一旦某个指标持续异常, 会每分钟推一条告警刷屏。
#   改为把上次告警时间存到文件, 跨进程生效。
_ALERT_STATE = '/home/ubuntu/smart-locker/logs/.crash_guard_last_alert.json'


def _load_alert_state():
    try:
        with open(_ALERT_STATE, 'r') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_alert_state(d):
    try:
        tmp = _ALERT_STATE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(d, f)
        os.replace(tmp, _ALERT_STATE)
    except Exception as e:
        logging.warning('告警节流状态落盘失败: %s', e)


def _should_alert(key, throttle=None):
    now = time.time()
    d = _load_alert_state()
    try:
        last = float(d.get(key, 0) or 0)
    except Exception:
        last = 0
    if now - last < (throttle or _THROTTLE):
        return False
    d[key] = now
    # 顺手清理超过7天的记录, 避免文件无限增长
    for k in list(d.keys()):
        try:
            if now - float(d[k] or 0) > 7 * 86400:
                d.pop(k, None)
        except Exception:
            d.pop(k, None)
    _save_alert_state(d)
    _LAST_ALERT[key] = now
    return True

# 机器标识(2026-09-10): 告警标题统一带机器名, 避免旧机(106)告警被误判为生产(175)故障
_NODE_TAG = {"VM-0-2-ubuntu": "175-生产", "VM-0-13-ubuntu": "106-旧机"}.get(socket.gethostname(), socket.gethostname())


def _alert(title, content):
    """PushPlus 告警，失败时写日志（标题统一带机器标识）"""
    try:
        title = "[%s] %s" % (_NODE_TAG, title)
        from helpers import send_pushplus
        ok = send_pushplus(title, content)
        logging.info('告警已推送: %s (ok=%s)', title, ok)
    except Exception as e:
        logging.error('告警推送失败: %s', e)
        # 兜底：写状态文件供其他监控读取
        with open('/tmp/crash_guard_alert', 'a') as f:
            f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), title))

def _run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception as e:
        logging.error('命令执行失败 %s: %s', cmd, e)
        return ''

def main():
    issues = []

    # 1. idle in transaction 卡死连接
    idle_txn = _run("""sudo -u postgres psql -d smart_locker -t -A -c "SELECT COUNT(*) FROM pg_stat_activity WHERE state='idle in transaction'" 2>/dev/null""")
    try:
        idle_n = int(idle_txn or 0)
    except ValueError:
        idle_n = 0
    if idle_n >= 10:
        # 严重：立即杀卡死连接（复用 kill_stuck_txn 逻辑）
        _run("sudo -u postgres psql -d smart_locker -t -A -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='smart_locker' AND state='idle in transaction' AND NOW()-xact_start > interval '30 seconds'\" > /dev/null 2>&1")
        if _should_alert('idle_critical'):
            _alert('【严重】数据库卡死连接%d个，已自动清理' % idle_n,
                   'smart-locker idle in transaction=%d（>=10 已自动 kill）。\n时间: %s' % (idle_n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('idle_txn=%d(严重)' % idle_n)
    elif idle_n >= 3:
        if _should_alert('idle_warn'):
            _alert('【警告】数据库卡死连接%d个' % idle_n,
                   'smart-locker idle in transaction=%d，接近崩溃前兆。\n时间: %s' % (idle_n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('idle_txn=%d' % idle_n)

    # 2. 未授予锁
    locks = _run("""sudo -u postgres psql -d smart_locker -t -A -c "SELECT COUNT(*) FROM pg_locks WHERE NOT granted" 2>/dev/null""")
    try:
        lock_n = int(locks or 0)
    except ValueError:
        lock_n = 0
    if lock_n >= 5:
        if _should_alert('lock'):
            _alert('【警告】数据库锁等待%d个' % lock_n,
                   'smart-locker 未授予锁=%d，可能有事务卡住。\n时间: %s' % (lock_n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('locks=%d' % lock_n)

    # 3. 最近1分钟 499/5xx
    code_5xx = _run("""sudo journalctl --since '1 minute ago' 2>/dev/null | grep -oE 'HTTP/1.[01]" 5[0-9]{2}' | wc -l""")
    try:
        err_n = int(code_5xx or 0)
    except ValueError:
        err_n = 0
    if err_n >= 100:
        if _should_alert('err_critical'):
            _alert('【严重】1分钟%d个5xx错误' % err_n,
                   'smart-locker 最近1分钟 5xx=%d，疑似全站异常。\n时间: %s' % (err_n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('5xx=%d(严重)' % err_n)
    elif err_n >= 20:
        if _should_alert('err_warn'):
            _alert('【警告】1分钟%d个5xx错误' % err_n,
                   'smart-locker 最近1分钟 5xx=%d。\n时间: %s' % (err_n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('5xx=%d' % err_n)

    # 4. gunicorn worker 数
    workers = _run("ps aux | grep -c '[g]unicorn'")
    try:
        w_n = int(workers or 0)
    except ValueError:
        w_n = 0
    if w_n < 7:
        if _should_alert('worker'):
            _alert('【警告】gunicorn worker仅%d个' % w_n,
                   'smart-locker gunicorn 进程=%d（正常应为9: 1主+8worker），worker可能被kill。\n时间: %s' % (w_n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('workers=%d' % w_n)

    # 5. 健康检查
    health = _run("curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:5001/api/health || echo 000")
    if health != '200':
        # 连续失败文件计数
        state_file = '/tmp/crash_guard_health_fail'
        n = 0
        try:
            n = int(open(state_file).read().strip() or 0)
        except Exception:
            n = 0
        n += 1
        open(state_file, 'w').write(str(n))
        if n >= 2 and _should_alert('health'):
            _alert('【严重】健康检查连续%d次失败' % n,
                   'smart-locker /api/health 返回 %s（连续%d次）。\n时间: %s' % (health, n, time.strftime('%Y-%m-%d %H:%M:%S')))
        issues.append('health=%s' % health)
    else:
        try:
            os.remove('/tmp/crash_guard_health_fail')
        except Exception:
            pass

    # 6. 数据库备份新鲜度 (2026-09-11 添加)
    #    背景: 175 此前没有任何定时数据库备份, 唯一的"副本"是已坏19天的逻辑复制。
    #    本项确保"备份真的跑成功了", 兜住定时任务静默失败的情况。
    try:
        _bk_files = sorted(glob.glob('/home/ubuntu/db_backups/smart_locker_*.dump'))
        _bk_ok = False
        if _bk_files:
            _newest = _bk_files[-1]
            _age_h = (time.time() - os.path.getmtime(_newest)) / 3600.0
            _sz_mb = os.path.getsize(_newest) / 1048576.0
            _bk_msg = '最新备份 %s: %.1f小时前, %.1fMB' % (os.path.basename(_newest), _age_h, _sz_mb)
            if _age_h <= 28 and _sz_mb >= 20:
                _bk_ok = True
        else:
            _bk_msg = '备份目录内没有找到任何备份文件'
        if not _bk_ok:
            # 备份类问题每 6 小时最多告警一次, 避免每分钟刷屏
            if _should_alert('backup', 21600):
                _alert('【严重】数据库备份异常',
                       'smart-locker 数据库备份检查未通过: %s\n备份目录: /home/ubuntu/db_backups/\n'
                       '排查: tail -50 /home/ubuntu/smart-locker/logs/backup_db.log\n时间: %s'
                       % (_bk_msg, time.strftime('%Y-%m-%d %H:%M:%S')))
            issues.append('backup=BAD')
    except Exception as e:
        logging.error('备份新鲜度检查失败: %s', e)

    if issues:
        logging.warning('检测到异常指标: %s', '; '.join(issues))
    else:
        logging.info('全部指标正常')

if __name__ == '__main__':
    main()
