from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.helpers import to_float
from config.logger import get_logger
from config.settings import ROLLING_WINDOW_HOURS
from config.time_utils import TZ, now_et, rolling_window_start, to_utc_timestamp
from crawler.polymarket import discover_contracts, fetch_midpoint, fetch_price_history
from processor.database import init_db, reset_db
from processor.storage import prune_before, save_contracts, save_metric_rows

logger = get_logger("crawler_backfill_window")


def _history_to_metric_rows(contract: dict, history: list[dict], window_start: datetime, window_end: datetime) -> list[tuple]:
    rows: list[tuple] = []
    for item in history:
        try:
            ts_raw = int(item["t"])
        except Exception:
            continue
        dt_et = datetime.fromtimestamp(ts_raw, tz=TZ).replace(second=0, microsecond=0)
        if dt_et < window_start or dt_et > window_end:
            continue
        probability = to_float(item.get("p"))
        if probability is None:
            continue
        rows.append(
            (
                dt_et.isoformat(),
                dt_et.strftime("%Y-%m-%d"),
                contract["group_key"],
                contract["market_slug"],
                contract["market_name"],
                contract["contract_slug"],
                contract["contract_name"],
                contract["yes_token_id"],
                probability,
                None,
                0,
                None,
                None,
                0,
                0,
            )
        )
    rows.sort(key=lambda x: x[0])
    return rows


def _process_contract(contract: dict, start_ts: int, end_ts: int, window_start: datetime, window_end: datetime) -> list[tuple]:
    try:
        history = fetch_price_history(contract["yes_token_id"], start_ts, end_ts, fidelity=1)
    except Exception as exc:
        logger.warning("History request failed contract=%s err=%s", contract["contract_slug"], exc)
        history = []

    rows = _history_to_metric_rows(contract, history, window_start, window_end)
    if rows:
        logger.info("Backfilled contract=%s rows=%s", contract["contract_slug"], len(rows))
        return rows

    try:
        probability = fetch_midpoint(contract["yes_token_id"])
    except Exception as exc:
        logger.warning("Midpoint fallback failed contract=%s err=%s", contract["contract_slug"], exc)
        probability = None

    if probability is None:
        return []

    ts = window_end.isoformat()
    return [
        (
            ts,
            window_end.strftime("%Y-%m-%d"),
            contract["group_key"],
            contract["market_slug"],
            contract["market_name"],
            contract["contract_slug"],
            contract["contract_name"],
            contract["yes_token_id"],
            probability,
            None,
            0,
            None,
            None,
            0,
            0,
        )
    ]


def run() -> dict:
    init_db()
    reset_db()

    window_end = now_et().replace(second=0, microsecond=0)
    window_start = rolling_window_start(window_end, ROLLING_WINDOW_HOURS)
    logger.info("Backfill window start=%s end=%s", window_start.isoformat(), window_end.isoformat())

    contracts = discover_contracts()
    if not contracts:
        logger.warning("No contracts found during backfill")
        return {"contracts": 0, "metrics": 0}

    save_contracts(contracts, window_end.isoformat())

    start_ts = to_utc_timestamp(window_start)
    end_ts = to_utc_timestamp(window_end)
    all_rows: list[tuple] = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_process_contract, contract, start_ts, end_ts, window_start, window_end): contract
            for contract in contracts
        }
        total = len(futures)
        for index, future in enumerate(as_completed(futures), start=1):
            contract = futures[future]
            try:
                all_rows.extend(future.result())
            except Exception as exc:
                logger.warning("Backfill failed contract=%s err=%s", contract["contract_slug"], exc)
            if index % 20 == 0 or index == total:
                logger.info("Backfill progress %s/%s metrics=%s", index, total, len(all_rows))

    save_metric_rows(all_rows)
    prune_before(window_start.isoformat())
    logger.info("Backfill done contracts=%s metrics=%s", len(contracts), len(all_rows))
    return {"contracts": len(contracts), "metrics": len(all_rows), "window_start": window_start.isoformat(), "window_end": window_end.isoformat()}


if __name__ == "__main__":
    run()
