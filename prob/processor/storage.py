from __future__ import annotations

from datetime import timedelta

from config.logger import get_logger
from processor.database import execute, executemany, get_conn

logger = get_logger("processor_storage")

UPSERT_CONTRACT_SQL = """
INSERT OR REPLACE INTO contracts (
    group_key, market_slug, market_name, contract_slug, contract_name,
    yes_token_id, event_id, condition_id, tags, discovered_at_et
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

UPSERT_METRIC_SQL = """
INSERT OR REPLACE INTO minute_metrics (
    ts_et, date_et, group_key, market_slug, market_name, contract_slug,
    contract_name, yes_token_id, probability, volume, volume_enabled,
    probability_change, volume_change, probability_anomaly, volume_anomaly
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_ALERT_SQL = """
INSERT INTO alerts (
    ts_et, date_et, group_key, market_slug, contract_slug, metric,
    old_value, new_value, change_ratio, message
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def save_contracts(rows: list[dict], discovered_at_et: str) -> None:
    payload = [
        (
            row["group_key"],
            row["market_slug"],
            row["market_name"],
            row["contract_slug"],
            row["contract_name"],
            row["yes_token_id"],
            row.get("event_id"),
            row.get("condition_id"),
            row.get("tags"),
            discovered_at_et,
        )
        for row in rows
    ]
    if payload:
        executemany(UPSERT_CONTRACT_SQL, payload)
    logger.info("Saved contracts count=%s discovered_at=%s", len(payload), discovered_at_et)


def load_contracts() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.* FROM contracts c "
            "LEFT JOIN contract_classification cc ON c.contract_slug = cc.contract_slug "
            "  AND c.group_key = cc.group_key "
            "WHERE cc.relevant IS NULL OR cc.relevant = 1 "
            "ORDER BY c.group_key, c.market_name, c.contract_name"
        ).fetchall()
        result = [dict(row) for row in rows]
    # 规则过滤兜底（LLM 可能误判或余额不足）
    result = _filter_by_rules(result)
    return result


def _filter_by_rules(contracts: list[dict]) -> list[dict]:
    """规则级过滤：剔除明显不属于贵金属/原油的合约。"""
    EXCLUDE_PATTERNS = [
        "golden boot", "golden glove", "mls", "nhl", "nba", "wnba",
        "golden state", "golden knights", "golden bulls", "golden kings",
        "goldman sachs", "goldman", "jared golden", "dan goldman",
        "trump.*golden", "trump.*gold",
        "gold medal", "golden dome",
    ]
    import re
    filtered = []
    for c in contracts:
        text = f"{c.get('contract_name', '')} {c.get('market_name', '')} {c.get('contract_slug', '')}".lower()
        excluded = any(re.search(p, text) for p in EXCLUDE_PATTERNS)
        if excluded:
            logger.debug("Rule filter excluded: %s (%s)", c.get("contract_name"), c.get("contract_slug"))
        else:
            filtered.append(c)
    excluded_count = len(contracts) - len(filtered)
    if excluded_count:
        logger.info("Rule filter excluded %d contracts, remaining %d", excluded_count, len(filtered))
    return filtered


def get_metric_one_hour_ago(contract_slug: str) -> dict | None:
    from config.time_utils import now_et
    one_hour_ago = (now_et() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:00")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ts_et, probability, volume, volume_enabled FROM minute_metrics "
            "WHERE contract_slug = ? AND ts_et <= ? "
            "ORDER BY ts_et DESC LIMIT 1",
            (contract_slug, one_hour_ago + "-04:00"),
        ).fetchone()
    return dict(row) if row else None


def get_last_metric(contract_slug: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM minute_metrics WHERE contract_slug = ? ORDER BY ts_et DESC LIMIT 1",
            (contract_slug,),
        ).fetchone()
        return dict(row) if row else None


def save_metric_rows(rows: list[tuple]) -> None:
    if rows:
        executemany(UPSERT_METRIC_SQL, rows)
    logger.info("Saved metrics rows=%s", len(rows))


def save_alert_rows(rows: list[tuple]) -> None:
    if rows:
        executemany(INSERT_ALERT_SQL, rows)
    logger.info("Saved alerts rows=%s", len(rows))


def prune_before(cutoff_ts_et: str) -> dict:
    with get_conn() as conn:
        metric_count = conn.execute(
            "SELECT COUNT(*) FROM minute_metrics WHERE ts_et < ?",
            (cutoff_ts_et,),
        ).fetchone()[0]
        alert_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE ts_et < ?",
            (cutoff_ts_et,),
        ).fetchone()[0]
        conn.execute("DELETE FROM minute_metrics WHERE ts_et < ?", (cutoff_ts_et,))
        conn.execute("DELETE FROM alerts WHERE ts_et < ?", (cutoff_ts_et,))
        conn.execute(
            "DELETE FROM contracts WHERE contract_slug NOT IN (SELECT DISTINCT contract_slug FROM minute_metrics)"
        )
        remaining_contracts = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        conn.commit()
    result = {
        "metrics_deleted": metric_count,
        "alerts_deleted": alert_count,
        "contracts_remaining": remaining_contracts,
    }
    logger.info("Pruned old rows result=%s cutoff=%s", result, cutoff_ts_et)
    return result


def delete_all_metrics_and_alerts() -> None:
    execute("DELETE FROM minute_metrics")
    execute("DELETE FROM alerts")


def remove_contracts(contract_slugs: list[str]) -> None:
    """从 contracts 表中移除已结束的合约。"""
    if not contract_slugs:
        return
    placeholders = ",".join("?" for _ in contract_slugs)
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM contracts WHERE contract_slug IN ({placeholders})",
            contract_slugs,
        )
        conn.commit()
    logger.info("Removed ended contracts count=%s", len(contract_slugs))
