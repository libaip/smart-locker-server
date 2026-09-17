#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[S230] 台账追加一条：把 mp_enter_log 那条记录改成复用连接"""
import io, time, shutil

P = '.workbench/TASKS.md'
TS = time.strftime('%Y%m%d_%H%M%S')
shutil.copyfile(P, P + '.bak_s230_' + TS)
s = io.open(P, encoding='utf-8').read().split('\n')
last = None
for i, ln in enumerate(s):
    if ln.startswith('| T-'):
        last = i
if last is None:
    raise SystemExit('找不到表格行')

tid = int(time.time())
row = (
    '| T-' + str(tid) + ' | S230 | routes/user.py | 已完成 | 09-17 16:07 | 09-17 16:09 | '
    '【起因】S229(T-1789625654) 为把"小程序进页面就报信"记进 mp_enter_log，用了 get_db() 另开一个连接写（每天约 1,900 次）；'
    '当天 14:17-15:45 出现 24 行"连接池取到坏连接"抖动（落在 complaint_scheduler / auto_withdraw 两条后台链，15:45 后归零），'
    '虽不像是主因，但老板同意顺手收紧 → 改成复用本接口已有的连接。'
    '【技术依据·实测过】database.py:178 把底层连接设为 autocommit=True → 每条 execute 自成事务，'
    '写失败不会污染后续语句（因此不需要 SAVEPOINT；实测此环境下 SAVEPOINT 也不可用：'
    '"SAVEPOINT can only be used in transaction blocks"）；且 get_db() 在 Flask 请求内复用同一个连接(flask.g)。'
    '【改法】删掉 _mep_conn/_mep_cur 新连接与 commit/close，直接用 handler 的 cursor.execute 写 mp_page；仍保留 try/except 只告警。'
    '【部署与校验】106 备份 backups/iosjump_s230_20260917_160746/user.py.bak（旧 md5 d9461d9be2ec64e6727958d7cf53de2c）；'
    '先对临时副本空跑看 diff（只少了 7 行、无其他变化）→ 正式改 → 新 md5 **2067f844330d99b31798e8cf10e69085**（py_compile OK，_mep_conn 残留 0）；'
    '175 同步（md5 一致）→ 零停机重载两个服务：MainPID 1736960/1736981 未变、is-active=active/active、首页 200、/api/health 200、'
    '重载后 60 秒 0 条 traceback/exception/坏连接。'
    '【遗留·必须补验】16:08 之后没有真实用户进小程序（等了 4 分钟，link_mp_openid 报信 0 次），所以"复用连接"这条写法**还没有真实流量验证**；'
    '需等有人进小程序时核对："link_mp_openid 成功绑定"次数 == mp_page 落库条数、且 0 条落库失败告警。'
    '【踩到的一个坑·已修正】175 上 S230 的"备份"是在 scp 上传**之后**才做的，等于备到的是新文件（备份与当前 md5 相同，d9461d9b→2067f844 之间没有留证）；'
    '已另存干净回滚点 175:/home/ubuntu/smart-locker/backups/user.py.bak_s229_for_rollback（md5 d9461d9be2ec64e6727958d7cf53de2c，已核对）。'
    '【回滚】cp -a /home/ubuntu/smart-locker/backups/user.py.bak_s229_for_rollback /home/ubuntu/smart-locker/routes/user.py && sudo kill -HUP 1736960 1736981 (175) |'
)

s[last + 1:last + 1] = [row]
io.open(P, 'w', encoding='utf-8', newline='').write('\n'.join(s))
print('已追加 1 行: T-%d (S230 已完成, 含遗留待验证说明)' % tid)
