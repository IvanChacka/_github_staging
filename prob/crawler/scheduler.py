"""scheduler.py — Main loop with independent timer for hourly report + macro at :25/:55."""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.logger import get_logger
from config.time_utils import now_et, et_day_start
from config.single_instance import acquire
from crawler.run_discovery import run as run_discovery
from crawler.run_minute_job import run as run_minute_job
from crawler.run_classify import run as run_classify
from processor.database import get_conn
from processor.llm_analyzer import clear_cache as clear_llm_cache

logger = get_logger("crawler_scheduler")

# ── Independent timer state ──────────────────────────────
_LAST_TRIGGER_KEY = (-1, -1)  # (hour_of_et_day, minute)
_LOCK = threading.Lock()


def _trigger_hourly_report(mark_minute: int) -> bool:
    """Run hourly report + macro_forecast, return True if success."""
    logger.info("Hourly report + macro trigger: ET minute=%s", mark_minute)
    ok = True

    try:
        from processor.hourly_report import run as hourly_run
        hourly_run()
        logger.info("Hourly report done")
    except Exception as e:
        logger.warning("Hourly report failed: %s", e)
        ok = False

    # IPO 简报：放在 macro 之前，避免被其阻塞
    try:
        from processor.ipo_report import run as ipo_run
        ipo_run()
        logger.info("IPO report done")
    except Exception as e:
        logger.warning("IPO report failed: %s", e)
        # IPO 失败不影响其他模块

    try:
        from processor.macro_forecast import run as macro_run
        macro_run()
        logger.info("Macro forecast done")
    except Exception as e:
        logger.warning("Macro forecast failed: %s", e)
        ok = False

    return ok


def _scheduler_timer():
    """Independent timer thread, checks every 10 seconds if it's time :25/:55."""
    global _LAST_TRIGGER_KEY
    while True:
        try:
            now = now_et()
            key = (now.hour, now.minute)
            if now.minute in (25, 55):
                with _LOCK:
                    if key != _LAST_TRIGGER_KEY:
                        _LAST_TRIGGER_KEY = key
                        # Trigger in a thread so it doesn't delay the next check
                        threading.Thread(
                            target=_trigger_hourly_report,
                            args=(now.minute,),
                            daemon=True,
                        ).start()
            # Reset key when minute passes to the next
            if now.minute not in (25, 55):
                with _LOCK:
                    if key != _LAST_TRIGGER_KEY:
                        _LAST_TRIGGER_KEY = key  # just track non-trigger minutes
        except Exception:
            pass
        time.sleep(10)


# ── Main scheduler loop ─────────────────────────────────


def _contract_count() -> int:
    """查询 DB 中合约数量"""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def run_scheduler() -> None:
    last_discovery_day = None

    # Start the independent trigger timer thread
    _timer_thread = threading.Thread(target=_scheduler_timer, daemon=True)
    _timer_thread.start()
    logger.info("Hourly report timer thread started: triggers at ET :25 and :55")

    # 首次启动时检查 DB 是否有合约，没有则触发 discovery + classify
    if _contract_count() == 0:
        logger.info("No contracts in DB, triggering initial discovery + classify")
        run_discovery()
        run_classify()
        last_discovery_day = now_et().strftime("%Y-%m-%d")
        logger.info("Initial discovery + classify done")

    logger.info("Scheduler booted")

    while True:
        try:
            current_dt = now_et()
            today_str = current_dt.strftime("%Y-%m-%d")

            # 每天凌晨 0:00 运行 discovery + classify
            if current_dt.hour == 0 and current_dt.minute < 1 and today_str != last_discovery_day:
                logger.info("Trigger daily discovery + classify at ET day start day=%s", today_str)
                run_discovery()
                try:
                    result = run_classify()
                    logger.info("Classification result=%s", result)
                except Exception as exc:
                    logger.exception("Classification failed: %s", exc)
                last_discovery_day = today_str

            result = run_minute_job()
            logger.info("Minute result=%s", result)
        except Exception as exc:
            logger.exception("Scheduler loop failed: %s", exc)

        sleep_seconds = 60 - now_et().second
        time.sleep(max(sleep_seconds, 1))


if __name__ == "__main__":
    if not acquire("scheduler"):
        sys.exit(1)
    run_scheduler()
