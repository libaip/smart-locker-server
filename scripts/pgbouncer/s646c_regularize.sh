#!/bin/bash
# [S646-C / 2026-09-24] pgbouncer 配置正规化 + 一次计划内重启（自带安全门与自动回滚）
#
# 目标（老板批准，凌晨低峰执行）：
#   logfile : /tmp/pgbouncer.log        -> /var/log/postgresql/pgbouncer.log
#   pidfile : /tmp/pgbouncer.pid        -> /var/run/postgresql/pgbouncer.pid
#   并把 /etc/init.d/pgbouncer 的 PIDDIR 还原为包默认(/var/run/postgresql)，使两者天然一致，
#   从而撤掉 S646-B 的本地补丁（否则日后 pgbouncer 包升级会把它覆盖回错位状态）。
#
# 失败处理：systemctl start 未就绪 -> 手工 su - postgres 拉起 -> 仍失败 -> 用备份整体回滚
# 幂等：已在目标状态则直接退出并自删 cron
set -u

INI=/etc/pgbouncer/pgbouncer.ini
INITD=/etc/init.d/pgbouncer
LR=/etc/logrotate.d/pgbouncer
NEWLOG=/var/log/postgresql/pgbouncer.log
NEWPID=/var/run/postgresql/pgbouncer.pid
MYCRON=/etc/cron.d/s646_pgbouncer_c
APP=/home/ubuntu/smart-locker
LOG=/home/ubuntu/s646_pgbouncer_c.log
TS=$(date +%Y%m%d_%H%M%S)
BK=/home/ubuntu/backups/s646c_175_before_$TS

log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
port_up(){ pg_isready -h 127.0.0.1 -p 6432 >/dev/null 2>&1; }
wait_down(){ for _i in $(seq 1 30); do port_up || return 0; sleep 0.5; done; return 1; }
wait_up(){ for _i in $(seq 1 30); do port_up && return 0; sleep 0.5; done; return 1; }

log "=== S646-C 开始 (TS=$TS) ==="

# 幂等：pidfile 已是目标值 -> 无需执行
if grep -q "^pidfile = $NEWPID" "$INI" 2>/dev/null; then
    log "已在目标状态, 无需执行; 删除 cron 条目"
    rm -f "$MYCRON"
    exit 0
fi

# 备份
mkdir -p "$BK"
cp -a "$INI" "$BK/pgbouncer.ini"
cp -a "$INITD" "$BK/pgbouncer.init"
[ -f "$LR" ] && cp -a "$LR" "$BK/logrotate.d-pgbouncer"
md5sum "$INI" "$INITD" > "$BK/md5.before" 2>/dev/null || true
log "备份: $BK"
log "备份 md5: $(cat "$BK/md5.before" | tr '\n' ' ')"

# 安全门 1：应用健康
for _p in 5001 5002; do
    _c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$_p/api/health" || echo 000)
    if [ "$_c" != "200" ]; then log "ABORT: :$_p /api/health = $_c (改前就不健康, 不动)"; exit 1; fi
done
log "安全门1 OK: health 200 x2"

# 安全门 2：无待处理投诉（避免打断自动退款）
DBURL=$(cd "$APP" && python3 -c 'from config import DATABASE_URL; print(DATABASE_URL)' 2>/dev/null || echo '')
if [ -z "$DBURL" ]; then log "ABORT: 取不到 DATABASE_URL"; exit 1; fi
PEND=$(psql "$DBURL" -tAc "SELECT count(*) FROM complaints WHERE status IN ('0','1')" 2>/dev/null || echo ERR)
case "$PEND" in
    ''|*[!0-9]*) log "ABORT: 无法确认待处理投诉数(=$PEND)"; exit 1;;
esac
if [ "$PEND" != "0" ]; then
    log "ABORT: 有 $PEND 条待处理投诉(status 0/1), 为免打断自动退款本次不重启; 保留 04:40 重试"
    exit 1
fi
log "安全门2 OK: 待处理投诉 0 条"

# 1) 停止（此时启动脚本 PIDDIR 仍 =/tmp，指向真实 pidfile，能真正停掉）
log "停止 pgbouncer ..."
systemctl stop pgbouncer >>"$LOG" 2>&1 || true
if ! wait_down; then
    log "systemctl stop 未生效, 改按 pidfile kill ..."
    _pid=$(cat /tmp/pgbouncer.pid 2>/dev/null || true)
    _comm=$(cat "/proc/${_pid:-0}/comm" 2>/dev/null || true)
    if [ -n "${_pid:-}" ] && [ "$_comm" = "pgbouncer" ]; then
        kill -TERM "$_pid" 2>/dev/null || true
    else
        log "pidfile 里的 PID(=${_pid:-空}) 不是 pgbouncer 进程(comm=${_comm:-无}), 拒绝盲杀 -> 改用 pkill -x pgbouncer"
        pkill -x pgbouncer 2>/dev/null || true
    fi
    if ! wait_down; then log "ABORT: 无法停止 pgbouncer, 未改任何配置"; exit 1; fi
fi
log "已停止(6432 已关闭)"

# 2) 改配置
sed -i "s|^pidfile = .*|pidfile = $NEWPID|" "$INI"
sed -i "s|^logfile = .*|logfile = $NEWLOG|" "$INI"
sed -i 's|^PIDDIR=/tmp$|PIDDIR=/var/run/postgresql|' "$INITD"
touch "$NEWLOG"; chown postgres:postgres "$NEWLOG"; chmod 640 "$NEWLOG"
systemctl daemon-reload
log "配置: $(grep -E '^(pidfile|logfile)' "$INI" | tr '\n' ' ')"
log "启动脚本: $(grep '^PIDDIR=' "$INITD")"

# 3) 启动 + 兜底 + 回滚
systemctl start pgbouncer >>"$LOG" 2>&1 || true
if ! wait_up; then
    log "systemctl start 后未就绪, 手工兜底拉起 ..."
    su - postgres -c "/usr/sbin/pgbouncer -d $INI" >>"$LOG" 2>&1 || true
fi
if ! wait_up; then
    log "CRITICAL: 新配置起不来 -> 开始整体回滚"
    systemctl stop pgbouncer >>"$LOG" 2>&1 || true
    cp -a "$BK/pgbouncer.ini" "$INI"
    cp -a "$BK/pgbouncer.init" "$INITD"
    [ -f "$BK/logrotate.d-pgbouncer" ] && cp -a "$BK/logrotate.d-pgbouncer" "$LR"
    systemctl daemon-reload
    su - postgres -c "/usr/sbin/pgbouncer -d $INI" >>"$LOG" 2>&1 || true
    if wait_up; then log "回滚成功: 已用旧配置恢复服务"; else log "CRITICAL: 回滚后仍未就绪, 需人工介入!"; fi
    exit 1
fi
# [S646-C 加固] 旧实例可能仍在退出中(实测 106 彩排: 端口已释放但进程尚未消失),
# 必须等它退干净并断言"只剩 1 个 pgbouncer", 否则可能出现新旧并存。
for _i in $(seq 1 20); do [ "$(pgrep -x pgbouncer | wc -l)" = "1" ] && break; sleep 0.5; done
_n=$(pgrep -x pgbouncer | wc -l)
_pids=$(pgrep -x pgbouncer | tr '\n' ' ')
log "pgbouncer 进程数=$_n PID=$_pids (is-active=$(systemctl is-active pgbouncer))"
if [ "$_n" != "1" ]; then
    _keep=$(ss -ltnp 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
    log "WARNING: 进程数异常(=$_n), 保留持有 6432 的 PID=${_keep:-未知}, 其余逐个善后"
    for _p in $_pids; do
        if [ "$_p" != "${_keep:-}" ] && [ "$(cat /proc/$_p/comm 2>/dev/null)" = "pgbouncer" ]; then
            log "  终止多余 pgbouncer PID=$_p"; kill -TERM "$_p" 2>/dev/null || true
        fi
    done
    for _i in $(seq 1 20); do [ "$(pgrep -x pgbouncer | wc -l)" = "1" ] && break; sleep 0.5; done
    log "善后后进程数=$(pgrep -x pgbouncer | wc -l) PID=$(pgrep -x pgbouncer | tr '\n' ' ')"
fi

# 4) 验证
_ok=1
for _p in 5001 5002; do
    _c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$_p/api/health" || echo 000)
    log "health :$_p = $_c"; [ "$_c" = "200" ] || _ok=0
done
psql "$DBURL" -tAc "SELECT 1" >/dev/null 2>&1 && log "DB 查询通过(经 6432)" || { log "DB 查询失败"; _ok=0; }
log "新 pidfile: $(ls -l "$NEWPID" 2>&1)"
log "新日志: $(ls -l "$NEWLOG" 2>&1)"
if [ "$_ok" != "1" ]; then
    log "RESULT=PARTIAL (服务已起但健康校验未全绿, 请人工复核; 保留 cron 不删)"
    exit 1
fi
log "RESULT=OK"

# 5) 收尾：logrotate 指向新路径 + 清理 /tmp 旧日志
cat > "$LR" <<'LR_EOF'
# [S646-C-20260924] pgbouncer 日志已迁至 /var/log/postgresql/pgbouncer.log(正规位置)
/var/log/postgresql/pgbouncer.log
{
    size 200M
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    su postgres postgres
}
LR_EOF
rm -f /tmp/pgbouncer.log /tmp/pgbouncer.log.[0-9]* /tmp/pgbouncer.pid
log "logrotate 已指向新路径; /tmp 旧日志与旧 pidfile 已清"
rm -f "$MYCRON"
log "已自删 cron 条目 $MYCRON"
log "=== S646-C 结束 ==="
