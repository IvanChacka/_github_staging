"""
watchdog.py — 全局进程监管器

职责：
  1. 启动所有服务模块（server + scheduler）
  2. 持续检查各模块健康状态：
     - app/server.py → 端口 8510 是否监听
     - crawler/scheduler.py → DB 分钟级写入是否超时
  3. 任何一个挂了 → kill 全组进程 → 5 分钟冷却 → 自动重启
  4. 日志输出到 logs/watchdog.log

使用方法：
    python watchdog.py          # 启动监管
    python watchdog.py stop     # 停止

推荐配合 start.bat 使用，或直接后台运行。
"""
from __future__ import annotations

import os
import sys
import time
import socket
import subprocess
import atexit
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import List, Optional
import ctypes
from ctypes import wintypes

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from config.logger import get_logger
from processor.database import get_conn

# ── 时区 ───────────────────────────────────────
CHINA_TZ = timezone(timedelta(hours=8), "CST")

# ── 配置 ────────────────────────────────────────
SERVER_PORT = 8510
SCHEDULER_DB_TIMEOUT_SEC = 900        # scheduler 超过此时间没写 DB 视为已死（LLM 调用慢，放宽到 15 分钟）
INITIAL_GRACE_SEC = 120               # 启动宽限（首轮采集 + LLM 调用）
COOLDOWN_SEC = 5 * 60                # 挂掉后的冷却期
CHECK_INTERVAL_SEC = 30               # 检查间隔

# ── Windows API ───────────────────────────────
kernel32 = ctypes.windll.kernel32
_OpenProcess = kernel32.OpenProcess
_OpenProcess.restype = wintypes.HANDLE
_OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

_CloseHandle = kernel32.CloseHandle
_CloseHandle.restype = wintypes.BOOL
_CloseHandle.argtypes = [wintypes.HANDLE]

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_TERMINATE = 0x0001

# ── 模块定义 ──────────────────────────────────
MODULES = [
    {
        "name": "server",
        "cmd": [sys.executable, "app/server.py"],
        "stdout": "logs/server_stdout.log",
        "stderr": "logs/server_stderr.log",
    },
    {
        "name": "scheduler",
        "cmd": [sys.executable, "crawler/scheduler.py"],
        "stdout": "logs/scheduler_stdout.log",
        "stderr": "logs/scheduler_stderr.log",
    },
]

_DB_PATH = Path("data/state/prob_monitor.db").resolve()

logger = get_logger("watchdog")
_proc_server: Optional[subprocess.Popen] = None
_proc_scheduler: Optional[subprocess.Popen] = None


def _all_processes() -> List[subprocess.Popen]:
    return [p for p in [_proc_server, _proc_scheduler] if p is not None]


# ── 健康检查 ──────────────────────────────────

def is_port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def parse_et_iso(ts_str: str) -> Optional[datetime]:
    """解析 '2026-05-27T03:29:00-04:00' 返回 UTC naive datetime。"""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def scheduler_last_db_ts() -> Optional[datetime]:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(ts_et) AS max_ts FROM minute_metrics"
            ).fetchone()
            if row and row["max_ts"]:
                return parse_et_iso(row["max_ts"])
    except Exception as exc:
        logger.warning("DB query failed: %s", exc)
    return None


def scheduler_is_alive() -> tuple[bool, str]:
    """检查 scheduler 健康状态，返回 (alive, detail)。

    策略：优先检查进程是否还活着。
    - 如果没有托管进程 → 任务未启动
    - 进程死了 → 直接判死
    - 进程活着 + DB 有最新记录 → 正常
    - 进程活着但 DB 无记录（首轮）→ 等
    - 进程活着但 DB 超时 → 累计连续 strike，满 2 次判死
    """
    global _proc_scheduler
    sched_proc = _proc_scheduler
    if sched_proc is None:
        return False, "No scheduler process started yet"

    is_alive = sched_proc.poll() is None
    if not is_alive:
        return False, "Process exited"

    last_ts = scheduler_last_db_ts()
    if last_ts is None:
        # 还没写入过任何记录，进程活着就放行（首轮采集慢）
        return True, "Process alive, no DB records yet (startup grace)"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = (now - last_ts).total_seconds()

    # 启动后 5 分钟内不执行 DB stale 判定
    # 因为上一轮 scheduler 被杀后 DB 里的老记录是旧数据，不能用来判断新进程
    if _scheduler_started_at is not None:
        uptime = (time.monotonic() - _scheduler_started_at)
        if uptime < 300:  # 5 分钟启动宽限
            return True, f"Process alive, startup grace ({uptime:.0f}s / 300s remaining)"

    db_fresh = elapsed < SCHEDULER_DB_TIMEOUT_SEC

    if db_fresh:
        _clear_stale()
        return True, f"Process alive + DB fresh ({elapsed:.0f}s ago)"
    else:
        n = _bump_stale()
        if n >= 2:
            return False, f"DB stale {elapsed:.0f}s ago, strike {n}/2 — WILL KILL"
        else:
            return True, f"DB stale {elapsed:.0f}s ago, strike {n}/2 — waiting"

_stale_counter = 0
_last_stale_check = 0.0
_scheduler_started_at: Optional[float] = None  # 给首次写入宽限时间


def _bump_stale() -> int:
    global _stale_counter, _last_stale_check
    now = time.time()
    if now - _last_stale_check > CHECK_INTERVAL_SEC * 3:
        # 如果距离上次检查太久，重置（说明可能是全新周期）
        _stale_counter = 1
    else:
        _stale_counter += 1
    _last_stale_check = now
    return _stale_counter


def _clear_stale() -> None:
    global _stale_counter
    _stale_counter = 0


@dataclass
class Health:
    name: str
    alive: bool
    detail: str = ""


def check_health() -> List[Health]:
    results: List[Health] = []
    port_ok = is_port_listening(SERVER_PORT)
    results.append(Health("server", port_ok,
                          f"port {SERVER_PORT} {'LISTENING' if port_ok else 'DOWN'}"))
    sched_alive, sched_detail = scheduler_is_alive()
    results.append(Health("scheduler", sched_alive, sched_detail))
    return results


# ── 进程管理 ──────────────────────────────────

def start_module(mod: dict) -> Optional[subprocess.Popen]:
    stdout_path = ROOT_DIR / mod["stdout"]
    stderr_path = ROOT_DIR / mod["stderr"]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_fh = open(stdout_path, "ab")
    stderr_fh = open(stderr_path, "ab")

    proc = subprocess.Popen(
        mod["cmd"],
        cwd=str(ROOT_DIR),
        stdout=stdout_fh,
        stderr=stderr_fh,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        if sys.platform == "win32" else 0,
    )
    logger.info("Started %s (PID %s)", mod["name"], proc.pid)
    return proc


def kill_process_tree(pid: int) -> None:
    """Windows 上通过 taskkill /T 杀掉进程树。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def kill_all() -> None:
    global _proc_server, _proc_scheduler, _stale_counter, _scheduler_started_at
    for proc in _all_processes():
        if proc.poll() is None:
            kill_process_tree(proc.pid)
    _proc_server = None
    _proc_scheduler = None
    _stale_counter = 0       # 重置 stale 计数器
    _scheduler_started_at = None  # 重置启动时间


def start_all() -> None:
    global _proc_server, _proc_scheduler, _scheduler_started_at
    for mod in MODULES:
        proc = start_module(mod)
        if proc:
            if mod["name"] == "server":
                _proc_server = proc
            elif mod["name"] == "scheduler":
                _proc_scheduler = proc
                _scheduler_started_at = time.monotonic()


def cleanup() -> None:
    logger.info("Watchdog shutting down, killing subprocesses...")
    kill_all()


# ── 冷却与等待 ───────────────────────────────

def cooldown_wait(seconds: int) -> None:
    """等待冷却时间，但每秒检查是否有人调了 stop。"""
    logger.info("Cooldown %ds starting...", seconds)
    chunk = 5
    while seconds > 0:
        t = min(chunk, seconds)
        time.sleep(t)
        seconds -= t


# ── PID 锁（单实例） ─────────────────────────

_PID_FILE = ROOT_DIR / "logs" / "watchdog.pid"


def _write_pid() -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        if _PID_FILE.exists():
            _PID_FILE.unlink()
    except Exception:
        pass


def _check_pid() -> Optional[int]:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        handle = _OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
        if handle:
            _CloseHandle(handle)
            return pid
    except Exception:
        pass
    return None


def _force_cleanup() -> None:
    """停止所有相关子进程。被动清理模式（由 stop 命令触发）。"""
    kill_all()
    logger.info("Stop: all subprocesses killed.")


# ── 主逻辑 ──────────────────────────────────

def main() -> None:
    existing = _check_pid()
    if existing:
        print(f"[watchdog] Existing instance PID {existing} is still running.")
        print(f"[watchdog] Run '{Path(__file__).name} stop' or kill it first.")
        sys.exit(1)

    _write_pid()
    atexit.register(cleanup)
    atexit.register(_remove_pid)

    logger.info("=" * 60)
    logger.info("Watchdog started at %s", datetime.now(CHINA_TZ).isoformat())
    logger.info("Modules: %s", ", ".join(m["name"] for m in MODULES))
    logger.info("Check: %ds interval | Cool down: %ds | DB timeout: %ds",
                CHECK_INTERVAL_SEC, COOLDOWN_SEC, SCHEDULER_DB_TIMEOUT_SEC)
    logger.info("=" * 60)

    first_loop = True
    _healthy_ticks = 0

    while True:
        if first_loop or not _all_processes():
            start_all()
            first_loop = False
            logger.info("Initial grace %ds...", INITIAL_GRACE_SEC)
            time.sleep(INITIAL_GRACE_SEC)
            continue

        health = check_health()
        dead = [h for h in health if not h.alive]
        alive_names = [h.name for h in health if h.alive]

        if dead:
            logger.warning("UNHEALTHY: %s | Healthy: %s",
                           [(h.name, h.detail) for h in dead],
                           alive_names)
            logger.warning("Killing all processes...")
            kill_all()

            logger.warning("Cooldown %ds starting at %s...",
                           COOLDOWN_SEC, datetime.now(CHINA_TZ).isoformat())
            cooldown_wait(COOLDOWN_SEC)

            logger.warning("Cooldown done, will restart.")
            continue

        # 每 4 轮健康时输出一次 info（减少日志噪音）
        _healthy_ticks = (_healthy_ticks + 1) % 4
        if _healthy_ticks == 0:
            logger.info("All healthy: %s", [(h.name, h.detail) for h in health])
        time.sleep(CHECK_INTERVAL_SEC)


# ── CLI ─────────────────────────────────────

def stop() -> None:
    """处理 'watchdog.py stop'"""
    existing = _check_pid()
    if not existing:
        print("[watchdog] No running instance found.")
        return
    try:
        import signal as _sig
        os.kill(existing, _sig.SIGTERM)
        print(f"[watchdog] Sent SIGTERM to PID {existing}. Waiting...")
        time.sleep(3)
    except Exception as e:
        print(f"[watchdog] Failed stop via signal: {e}")
        print("[watchdog] Trying taskkill...")
        try:
            kill_process_tree(existing)
        except Exception:
            pass
    _remove_pid()


def killall_cmd() -> None:
    """处理 'watchdog.py killall' — 不通过 PID 文件，直接扫进程树。"""
    print("[watchdog] Scanning for prob python processes...")
    for mod in MODULES:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "python.exe", "/FI",
                 f"WINDOWTITLE eq {mod['name']}"],
                capture_output=True, timeout=5, text=True,
            )
            if result.stdout:
                print(f"  {mod['name']}: {result.stdout.strip()}")
        except Exception:
            pass
    print("[watchdog] Done.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "stop":
            stop()
        elif cmd == "killall":
            killall_cmd()
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python watchdog.py [stop|killall]")
        sys.exit(0)
    main()
