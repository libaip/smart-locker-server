#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 PostgreSQL 备份 + 异地副本 + 失败告警  (2026-09-11 建立)

【为什么需要这个脚本】
  175 生产库此前【没有任何定时数据库备份】, 唯一的"数据副本"是 106 旧机上的
  逻辑复制(lh_sub)。而它自 2026-08-23 起就因为主键冲突(duplicate key on
  withdrawal_records_pkey)导致 apply worker 每 5 秒崩溃一次, 复制位点永远
  不前进, 在 175 上钉住了 25GB WAL, 把磁盘顶到 97% —— 磁盘满会让 PostgreSQL
  PANIC 导致全站宕机。2026-09-11 已删除该死槽位与死订阅。
  => 也就是说: 出事前这台生产库实际上【一个可用备份都没有】。

【本脚本做什么】每天 04:30 由 crontab 调用:
  1. 检查磁盘剩余空间(不足 3GB 直接告警退出, 避免写满盘)
  2. pg_dump -Fc 导出 smart_locker 到 /home/ubuntu/db_backups/
  3. 校验: 文件大小 >= 20MB, 且 pg_restore -l 能正常解析(确保不是半截文件)
  4. 本机保留最近 14 天
  5. 同步一份到 106 旧机(178G 磁盘)做异地副本, 同样保留 14 天
  6. 写状态文件 .last_ok 供监控脚本判断新鲜度
  7. 任何一步失败 -> pushplus 告警(带机器名)

【配套】monitor_crash_guard.py 里有第6项检查: 最新备份超过 28 小时或小于 20MB
        就告警 —— 用来兜住"定时任务根本没跑"这种静默失败。
"""
import os
import sys
import time
import glob
import shutil
import logging
import subprocess
import socket

sys.path.insert(0, '/home/ubuntu/smart-locker')

BACKUP_DIR = '/home/ubuntu/db_backups'
OFFSITE_HOST = 'ubuntu@106.55.7.10'
OFFSITE_DIR = '/home/ubuntu/db_backups_from_175'
DB_NAME = 'smart_locker'
KEEP_DAYS = 14
MIN_SIZE_MB = 20
MIN_FREE_GB = 3

logging.basicConfig(
    filename='/home/ubuntu/smart-locker/logs/backup_db.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

_NODE_TAG = {"VM-0-2-ubuntu": "175-生产", "VM-0-13-ubuntu": "106-旧机"}.get(
    socket.gethostname(), socket.gethostname())


def _alert(title, content):
    """pushplus 告警(标题带机器名); 推送失败写状态文件兜底"""
    try:
        title = '[%s] %s' % (_NODE_TAG, title)
        from helpers import send_pushplus
        ok = send_pushplus(title, content)
        logging.info('告警已推送: %s (ok=%s)', title, ok)
    except Exception as e:
        logging.error('告警推送失败: %s', e)
        try:
            with open('/tmp/backup_db_alert', 'a') as f:
                f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), title))
        except Exception:
            pass


def _run(cmd, timeout=1800):
    """执行命令, 返回 (rc, stdout, stderr)"""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def _free_gb(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024.0 ** 3


def _prune(directory, pattern, keep_days):
    """删除超过 keep_days 天的旧备份, 但永远保留最新的一个"""
    cutoff = time.time() - keep_days * 86400
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    if len(files) <= 1:
        return []
    removed = []
    for f in files[:-1]:
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed.append(os.path.basename(f))
        except Exception as e:
            logging.warning('删除旧备份失败 %s: %s', f, e)
    return removed


def main():
    started = time.time()
    stamp = time.strftime('%Y%m%d_%H%M%S')
    fname = 'smart_locker_%s.dump' % stamp
    fpath = os.path.join(BACKUP_DIR, fname)
    # [FIX-20260912] 先写到 .tmp, 校验通过后再原子改名成正式文件。
    #   起因: 2026-09-12 04:30 监控误报"最新备份 0.0MB" —— 因为备份脚本直接往最终文件名里流式写,
    #   而监控每分钟检查一次最新 *.dump, 恰好在备份开始后 1 秒看到了半成品。
    #   改用 .tmp 后, *.dump 永远只会是完整文件(monitor_crash_guard 的 glob 只匹配 *.dump, 不会看到 .tmp)。
    fpath_tmp = fpath + '.tmp'
    problems = []

    logging.info('===== 备份开始 =====')
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # 1. 磁盘空间
    free = _free_gb(BACKUP_DIR)
    if free < MIN_FREE_GB:
        msg = '%s 磁盘剩余 %.2fGB < %dGB, 为避免写满盘已跳过本次备份' % (
            _NODE_TAG, free, MIN_FREE_GB)
        logging.error(msg)
        _alert('【严重】数据库备份被跳过(磁盘将满)', msg + '\n时间: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
        return 1

    # 2. pg_dump（先写 .tmp, 避免监控读到半成品)
    try:
        with open(fpath_tmp, 'wb') as fh:
            r = subprocess.run(
                ['sudo', '-u', 'postgres', 'pg_dump', '-Fc', '-d', DB_NAME],
                stdout=fh, stderr=subprocess.PIPE, timeout=1800,
                stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            err = (r.stderr or b'').decode('utf-8', 'replace').strip()[:500]
            problems.append('pg_dump 失败(rc=%d): %s' % (r.returncode, err))
    except Exception as e:
        problems.append('pg_dump 异常: %s' % e)

    # 3. 校验（校验 .tmp，通过后才改名）
    size_mb = 0
    if os.path.exists(fpath_tmp):
        size_mb = os.path.getsize(fpath_tmp) / 1048576.0
    if not problems:
        if size_mb < MIN_SIZE_MB:
            problems.append('备份文件过小: %.1fMB < %dMB' % (size_mb, MIN_SIZE_MB))
        else:
            rc, out, err = _run('pg_restore -l %s' % fpath_tmp, timeout=300)
            if rc != 0:
                problems.append('pg_restore 校验失败(rc=%d): %s' % (rc, err[:300]))
            else:
                n_items = len([l for l in out.splitlines() if l and not l.startswith(';')])
                if n_items < 100:
                    problems.append('备份内容异常: 只解析出 %d 个对象' % n_items)
                else:
                    logging.info('校验通过: %s (%.1fMB, %d个对象)', fname, size_mb, n_items)

    if problems:
        for _f in (fpath_tmp, fpath):
            try:
                if os.path.exists(_f):
                    os.remove(_f)   # 删掉可疑的坏/半成品文件, 免得监控误判为"有备份"
            except Exception:
                pass
        msg = '数据库备份失败:\n' + '\n'.join('- ' + p for p in problems)
        logging.error(msg.replace('\n', ' | '))
        _alert('【严重】数据库备份失败', msg + '\n时间: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
        return 1

    # 3.5 校验全部通过 -> 原子改名成正式文件(此刻监控才可能看到它, 且一定是完整的)
    try:
        os.replace(fpath_tmp, fpath)
        logging.info('已发布正式备份: %s (%.1fMB)', fname, size_mb)
    except Exception as e:
        logging.error('改名失败: %s', e)
        _alert('【严重】数据库备份改名失败',
               '备份已生成但无法发布为正式文件: %s\n临时文件: %s\n时间: %s'
               % (e, fpath_tmp, time.strftime('%Y-%m-%d %H:%M:%S')))
        return 1

    # 4. 本机保留策略
    #   顺手清掉超过1天的 .tmp 残留(备份中途被杀会留下半成品)
    try:
        _cutoff = time.time() - 86400
        for _t in glob.glob(os.path.join(BACKUP_DIR, 'smart_locker_*.dump.tmp')):
            if os.path.getmtime(_t) < _cutoff:
                os.remove(_t)
                logging.info('清理残留临时文件: %s', os.path.basename(_t))
    except Exception as e:
        logging.warning('清理临时文件失败: %s', e)

    removed = _prune(BACKUP_DIR, 'smart_locker_*.dump', KEEP_DAYS)
    if removed:
        logging.info('本机清理旧备份 %d 个: %s', len(removed), ', '.join(removed))

    # 5. 异地副本 -> 106 旧机
    offsite_ok = False
    try:
        _run('ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no '
             '-o ConnectTimeout=15 %s "mkdir -p %s"' % (OFFSITE_HOST, OFFSITE_DIR), timeout=60)
        rc, out, err = _run(
            'scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15 '
            '-q %s %s:%s/' % (fpath, OFFSITE_HOST, OFFSITE_DIR), timeout=1800)
        if rc == 0:
            offsite_ok = True
            logging.info('异地副本已同步到 %s:%s', OFFSITE_HOST, OFFSITE_DIR)
            # 远端保留策略(用 find 按天数删, 保留最新)
            _run('ssh -n -o BatchMode=yes -o ConnectTimeout=15 %s '
                 '"ls -1t %s/smart_locker_*.dump 2>/dev/null | tail -n +%d | xargs -r rm -f"'
                 % (OFFSITE_HOST, OFFSITE_DIR, KEEP_DAYS + 1), timeout=120)
        else:
            logging.error('异地副本同步失败: %s', err[:300])
            _alert('【警告】数据库异地副本同步失败',
                   '本机备份成功(%s, %.1fMB), 但同步到 106 失败:\n%s\n时间: %s'
                   % (fname, size_mb, err[:300], time.strftime('%Y-%m-%d %H:%M:%S')))
    except Exception as e:
        logging.error('异地副本异常: %s', e)
        _alert('【警告】数据库异地副本同步异常',
               '本机备份成功(%s), 但同步到 106 异常: %s\n时间: %s'
               % (fname, e, time.strftime('%Y-%m-%d %H:%M:%S')))

    # 6. 状态文件
    try:
        with open(os.path.join(BACKUP_DIR, '.last_ok'), 'w') as f:
            f.write('%s\n%s\n%.1fMB\noffsite=%s\n' % (
                time.strftime('%Y-%m-%d %H:%M:%S'), fname, size_mb, offsite_ok))
    except Exception as e:
        logging.warning('写状态文件失败: %s', e)

    logging.info('===== 备份完成: %s (%.1fMB, 耗时%.0fs, 异地=%s) =====',
                 fname, size_mb, time.time() - started, offsite_ok)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        logging.error('备份脚本未捕获异常: %s', e, exc_info=True)
        _alert('【严重】数据库备份脚本崩溃', '未捕获异常: %s\n时间: %s'
               % (e, time.strftime('%Y-%m-%d %H:%M:%S')))
        sys.exit(1)
