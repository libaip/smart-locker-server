#!/usr/bin/env python3
"""批量自动退款 - 独立进程执行，避免阻塞 gunicorn worker

背景：/api/admin/withdrawal/batch-auto 原在 gunicorn worker 内同步跑退款循环，
每笔退款同步调微信 API（1s+），批量几十笔时 worker 被阻塞超过 120s 被
gunicorn WORKER TIMEOUT 杀死，导致该窗口所有取包 verify 请求 499/502，
APK 弹"解析错误"。本脚本由 cron 直接执行，退款在独立进程完成。

用法：python3 run_withdrawal_batch.py
"""
import sys, os, time, logging, warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, "/home/ubuntu/smart-locker")

LOCK_FILE = "/tmp/withdrawal_batch_auto.lock"
LOG_FILE = "/home/ubuntu/smart-locker/logs/withdrawal_batch.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main():
    lock_fd = None
    try:
        import fcntl
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.info("[withdrawal_batch] 已有任务在执行，跳过")
        print("[withdrawal_batch] 已有任务在执行，跳过", flush=True)
        return
    except Exception as e:
        logging.warning("[withdrawal_batch] 获取锁失败: %s", e)

    t0 = time.time()
    try:
        from routes.admin_v2 import _run_withdrawal_batch_auto
        approved, rejected = _run_withdrawal_batch_auto()
        dt = time.time() - t0
        logging.info("[withdrawal_batch] 完成: 通过%s笔 拒绝%s笔 耗时%.1fs", approved, rejected, dt)
        print(f"[withdrawal_batch] 完成: 通过{approved}笔 拒绝{rejected}笔 耗时{dt:.1f}s", flush=True)
    except Exception as e:
        import traceback
        logging.error("[withdrawal_batch] 异常: %s\n%s", e, traceback.format_exc())
        print(f"[withdrawal_batch] 异常: {e}", flush=True)
    finally:
        if lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
