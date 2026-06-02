"""run_minute_job.py — Pure data collection. No report/macro logic."""
from __future__ import annotations

import sys
import time as _time_module
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FutTimeout
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.logger import get_logger
import threading as _threading
TOTAL_RUN_TIMEOUT = 420  # 7 min hard stop for entire run()

def _abort_on_timeout():
    """Emergency abort daemon kills the process if run() hangs."""
    import os as _os
    _os.abort()

from config.settings import ROLLING_WINDOW_HOURS, VOLUME_FAILURE_THRESHOLD
from config.time_utils import floor_to_minute, now_et, rolling_window_start
from crawler.polymarket import fetch_midpoint, fetch_volume
from processor.anomaly import compute_change
from processor.database import get_conn, init_db
from processor.storage import get_metric_one_hour_ago, load_contracts, prune_before, remove_contracts, save_alert_rows, save_metric_rows
from processor.notifier import send_feishu_alerts

_CLEANED_DATE = None

logger = get_logger("crawler_minute_task")
_VOLUME_DISABLED = False
_VOLUME_FAILURES = 0
_MAX_WORKERS = 8


def _build_alert_row(ts_et: str, date_et: str, contract: dict, metric: str, change_result, label: str) -> tuple:
    return (
        ts_et,
        date_et,
        contract["group_key"],
        contract["market_slug"],
        contract["contract_slug"],
        metric,
        change_result.old_value,
        change_result.new_value,
        change_result.change_ratio,
        f"[{contract['group_key']}] {contract['contract_name']} {label}波动超过5%",
    )


def _fetch_one_contract(contract: dict, volume_disabled: bool) -> dict:
    """单个线程抓取一个合约的 midpoint 和 volume"""
    midpoint_404 = False
    try:
        probability = fetch_midpoint(contract["yes_token_id"])
    except Exception as exc:
        err_msg = str(exc)
        if "404" in err_msg or "Not Found" in err_msg:
            midpoint_404 = True
            logger.info("Midpoint 404 (contract ended), will remove contract=%s", contract["contract_slug"])
        else:
            logger.warning("Midpoint failed contract=%s err=%s", contract["contract_slug"], exc)
        probability = None

    volume = None
    volume_ok = False
    if not volume_disabled:
        try:
            volume = fetch_volume(contract.get("condition_id"), contract.get("contract_slug"))
            if volume is not None:
                volume_ok = True
        except Exception as exc:
            logger.warning("Volume failed contract=%s err=%s", contract["contract_slug"], exc)

    return {
        "contract": contract,
        "probability": probability,
        "volume": volume,
        "volume_ok": volume_ok,
        "midpoint_404": midpoint_404,
    }


def _clean_expired_by_date():
    """根据合约中的过期日期，移除已过期的合约。每天只执行一次。"""
    global _CLEANED_DATE
    today = now_et().strftime("%Y-%m-%d")
    if _CLEANED_DATE == today:
        return

    import re
    from datetime import datetime, timezone, timedelta

    month_map = {
        'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
        'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12
    }

    now_utc = datetime.now(timezone.utc)
    contracts = load_contracts()
    to_remove = []

    for contract in contracts:
        text = f'{contract["contract_slug"]} {contract["contract_name"]}'.lower()
        m = re.search(r'(?:by|on|week\.of|of)\s*(\w+)[-\s]*(\d+)[-\s]*(\d{4})', text)
        if not m:
            m = re.search(r'(\w+)[-\s]*(\d+)[-\s]*(\d{4})', text)
        if not m:
            continue

        mon_str = m.group(1)[:3].lower()
        month = month_map.get(mon_str)
        if not month:
            continue
        try:
            day = int(m.group(2))
            year = int(m.group(3))
        except ValueError:
            continue

        expiry = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)
        if now_utc > expiry:
            to_remove.append(contract["contract_slug"])

    if to_remove:
        remove_contracts(to_remove)
        logger.info("Cleaned expired contracts by date count=%s", len(to_remove))

    _CLEANED_DATE = today


def run() -> dict:
    global _VOLUME_DISABLED, _VOLUME_FAILURES

    # Hard timeout daemon kills process if run() hangs
    _timer = _threading.Timer(TOTAL_RUN_TIMEOUT, _abort_on_timeout)
    _timer.daemon = True
    _timer.start()

    init_db()
    ts_dt = floor_to_minute()
    ts_et = ts_dt.isoformat()
    date_et = ts_dt.strftime("%Y-%m-%d")

    _clean_expired_by_date()

    contracts = load_contracts()
    logger.info("Start minute collection ts=%s contracts=%s volume_disabled=%s", ts_et, len(contracts), _VOLUME_DISABLED)

    volume_success = 0
    volume_failed_this_round = 0

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_contract, c, _VOLUME_DISABLED): c for c in contracts}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                logger.warning("Fetch worker failed err=%s", exc)

    metric_rows: list[tuple] = []
    alert_rows: list[tuple] = []
    ended_slugs: list[str] = []

    for r in results:
        contract = r["contract"]
        probability = r["probability"]

        if r["midpoint_404"]:
            ended_slugs.append(contract["contract_slug"])
            continue

        volume = r["volume"]

        volume_enabled = int(not _VOLUME_DISABLED)
        if not _VOLUME_DISABLED:
            if r["volume_ok"]:
                volume_success += 1
            else:
                volume_failed_this_round += 1
        else:
            volume_enabled = 0

        last_metric = get_metric_one_hour_ago(contract["contract_slug"])
        probability_change = compute_change(
            last_metric["probability"] if last_metric else None,
            probability,
            metric="probability",
        )
        volume_change = compute_change(
            last_metric.get("volume") if last_metric else None,
            volume,
            metric="volume",
        )

        metric_rows.append(
            (
                ts_et,
                date_et,
                contract["group_key"],
                contract["market_slug"],
                contract["market_name"],
                contract["contract_slug"],
                contract["contract_name"],
                contract["yes_token_id"],
                probability,
                volume,
                volume_enabled,
                probability_change.change_ratio,
                volume_change.change_ratio,
                int(probability_change.is_anomaly),
                int(volume_change.is_anomaly and volume_enabled),
            )
        )

        if probability_change.is_anomaly:
            alert_rows.append(_build_alert_row(ts_et, date_et, contract, "probability", probability_change, "价格"))

    if not _VOLUME_DISABLED:
        if volume_success == 0 and volume_failed_this_round > 0:
            _VOLUME_FAILURES += 1
        else:
            _VOLUME_FAILURES = 0
        if _VOLUME_FAILURES >= VOLUME_FAILURE_THRESHOLD:
            _VOLUME_DISABLED = True
            logger.warning("Volume line disabled after consecutive failures=%s", _VOLUME_FAILURES)
    else:
        _VOLUME_FAILURES = 0

    if ended_slugs:
        remove_contracts(ended_slugs)
        logger.info("Removed ended contracts count=%s slugs=%s", len(ended_slugs), ended_slugs)

    save_metric_rows(metric_rows)

    # 发送通知：只发送"1小时内没发过异常通知"的合约
    save_alert_rows(alert_rows)
    if alert_rows:
        from processor.database import get_conn as db_get_conn
        from config.time_utils import now_et

        already_sent = set()
        try:
            cutoff_et = now_et() - __import__("datetime").timedelta(hours=1)
            cutoff_str = cutoff_et.strftime("%Y-%m-%dT%H:%M:%S")
            with db_get_conn() as dbc:
                sent_rows = dbc.execute(
                    "SELECT contract_slug, MAX(ts_et) FROM alerts GROUP BY contract_slug"
                ).fetchall()
                for slug, max_ts in sent_rows:
                    if max_ts and max_ts >= cutoff_str:
                        already_sent.add(slug)
        except Exception:
            pass

        new_rows = [r for r in alert_rows if r[4] not in already_sent]
        if new_rows:
            slug_to_name = {c["contract_slug"]: c.get("contract_name", c["contract_slug"]) for c in contracts}
            alert_dicts = [
                {
                    "contract_name": slug_to_name.get(r[4], r[4]),
                    "contract_slug": r[4],
                    "group_key": r[2],
                    "metric": r[5],
                    "old_value": r[6],
                    "new_value": r[7],
                    "change_ratio": r[8],
                    "ts_et": r[0],
                }
                for r in new_rows
            ]
            sent = send_feishu_alerts(alert_dicts)
            logger.info("Feishu alerts sent=%s new=%s total=%s", sent, len(new_rows), len(alert_rows))
        else:
            logger.info("Feishu alerts skipped (all duplicates), total=%s", len(alert_rows))

    prune_result = prune_before(rolling_window_start(ts_dt, ROLLING_WINDOW_HOURS).isoformat())
    logger.info(
        "Finish minute collection metrics=%s alerts=%s volume_success=%s volume_failed=%s volume_disabled=%s prune=%s",
        len(metric_rows),
        len(alert_rows),
        volume_success,
        volume_failed_this_round,
        _VOLUME_DISABLED,
        prune_result,
    )
    _timer.cancel()
    return {
        "metrics": len(metric_rows),
        "alerts": len(alert_rows),
        "volume_disabled": _VOLUME_DISABLED,
        "volume_success": volume_success,
        "volume_failed": volume_failed_this_round,
        "prune": prune_result,
    }


if __name__ == "__main__":
    run()
