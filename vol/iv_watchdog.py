"""
iv_watchdog.py — 隐波守护进程的监管器
- 每 30 秒检查一次 iv_guard 的心跳
- 如果心跳超时（> 2 次检测）则重启进程
- 记录重启日志，防止无限重启风暴
- 经 scheduler 的日志系统可被发现
"""

import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# ── 路径 ──
VOL_DIR = Path(__file__).resolve().parent
GUARD_SCRIPT = VOL_DIR / "iv_guard.py"
HEARTBEAT_FILE = VOL_DIR / "iv_guard.heartbeat.json"

# ── 配置 ──
CHECK_INTERVAL = 30         # 每次检测间隔（秒）
HEARTBEAT_TIMEOUT = 120     # 心跳过期时间（秒）— 超过此值视为守护进程挂了
MAX_RESTARTS = 5            # 连续重启上限
RESTART_WINDOW = 600        # 统计窗口（秒）— 窗口内重启超过上限则停止

# ── Python 解释器 ──
PYTHON = sys.executable or "python"


# ============================================================


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            continue


def _format_dt() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def read_heartbeat() -> dict | None:
    """读取心跳文件，返回 dict；失败返回 None"""
    try:
        if HEARTBEAT_FILE.exists():
            with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def is_heartbeat_alive(hb: dict | None) -> bool:
    """检查心跳是否活着"""
    if hb is None:
        return False
    ts = hb.get("ts", 0)
    now = time.time()
    if now - ts > HEARTBEAT_TIMEOUT:
        return False
    # 如果有错误状态也算不活
    if hb.get("status") == "error":
        return False
    if hb.get("status") == "stopped":
        return False
    return True


def is_process_running(proc: subprocess.Popen | None) -> bool:
    """检查进程是否还活着"""
    if proc is None:
        return False
    return proc.poll() is None


def start_guard() -> subprocess.Popen | None:
    """启动 iv_guard.py 子进程"""
    try:
        proc = subprocess.Popen(
            [PYTHON, str(GUARD_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        print(f"[{_format_dt()}] [WATCHDOG] 启动 iv_guard.py (PID={proc.pid})")
        return proc
    except Exception as e:
        print(f"[{_format_dt()}] [WATCHDOG] 启动失败: {e}")
        return None


def main():
    _configure_stdio()
    print("=" * 60)
    print(f"  iv_watchdog.py — 隐波守护监管器")
    print(f"  检测间隔: {CHECK_INTERVAL}s")
    print(f"  心跳超时: {HEARTBEAT_TIMEOUT}s")
    print(f"  守护脚本: {GUARD_SCRIPT}")
    print(f"  心跳文件: {HEARTBEAT_FILE}")
    print("=" * 60)

    guard_proc = None
    restart_times = []
    consecutive_failures = 0  # 连续启动失败计数

    while True:
        try:
            now = time.time()

            # 1. 如果守护进程不在运行，先启动
            if not is_process_running(guard_proc):
                if guard_proc is not None:
                    print(f"[{_format_dt()}] [WATCHDOG] 进程已退出 (returncode={guard_proc.poll()})")
                # 检查是否达到重启上限
                restart_times = [t for t in restart_times if now - t < RESTART_WINDOW]
                if len(restart_times) >= MAX_RESTARTS:
                    print(f"[{_format_dt()}] [WATCHDOG] !!! 连续重启 {MAX_RESTARTS} 次，停止监管 !!!")
                    print(f"[{_format_dt()}] [WATCHDOG] 请手动检查 {GUARD_SCRIPT}")
                    # 写 FAILED 状态
                    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                        json.dump({
                            "time": datetime.now(TZ).isoformat(),
                            "ts": now,
                            "status": "watchdog_giveup",
                            "error": f"连续重启 {MAX_RESTARTS} 次/窗口 {RESTART_WINDOW}s",
                        }, f, ensure_ascii=False)
                    break

                guard_proc = start_guard()
                if guard_proc is not None:
                    restart_times.append(now)
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    print(f"[{_format_dt()}] [WATCHDOG] 连续启动失败 #{consecutive_failures}")
                    if consecutive_failures >= 3:
                        print(f"[{_format_dt()}] [WATCHDOG] !!! 连续 3 次启动失败，退出 !!!")
                        break
                time.sleep(CHECK_INTERVAL)
                continue

            # 2. 进程在运行，检查心跳
            hb = read_heartbeat()
            alive = is_heartbeat_alive(hb)

            if not alive:
                hb_time = hb.get("time", "?") if hb else "—"
                hb_status = hb.get("status", "?") if hb else "no_heartbeat"
                print(f"[{_format_dt()}] [WATCHDOG] 心跳异常! ts={hb_time} status={hb_status}")

                # 杀掉旧进程
                try:
                    guard_proc.terminate()
                    guard_proc.wait(timeout=5)
                except Exception:
                    guard_proc.kill()
                guard_proc = None

                # 写异常状态
                with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                    json.dump({
                        "time": datetime.now(TZ).isoformat(),
                        "ts": now,
                        "status": "watchdog_killed",
                        "reason": f"heartbeat_dead: last_status={hb_status}",
                    }, f, ensure_ascii=False)
            else:
                # 心跳正常，重置重启计数器
                if restart_times:
                    restart_times.clear()
                    print(f"[{_format_dt()}] [WATCHDOG] 心跳恢复，重置重启计数器")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n[{_format_dt()}] [WATCHDOG] 用户中断")
            if guard_proc and is_process_running(guard_proc):
                guard_proc.terminate()
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "time": datetime.now(TZ).isoformat(),
                    "ts": time.time(),
                    "status": "watchdog_stopped",
                }, f, ensure_ascii=False)
            break
        except Exception as e:
            print(f"[{_format_dt()}] [WATCHDOG] 异常: {e}")
            traceback.print_exc()
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
