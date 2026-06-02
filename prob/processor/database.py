from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config.logger import get_logger
from config.settings import STATE_DIR

DB_PATH = STATE_DIR / "prob_monitor.db"
logger = get_logger("database")
_DIR = Path(__file__).resolve().parent.parent / "data" / "state"
_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_tables(cursor: sqlite3.Cursor):
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            group_key       TEXT NOT NULL,
            market_slug     TEXT NOT NULL,
            contract_slug   TEXT NOT NULL PRIMARY KEY,
            contract_name   TEXT NOT NULL,
            market_name     TEXT,
            yes_token_id    TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS minute_metrics (
            ts_et               TEXT NOT NULL,
            date_et             TEXT NOT NULL,
            group_key           TEXT NOT NULL,
            market_slug         TEXT NOT NULL,
            market_name         TEXT,
            contract_slug       TEXT NOT NULL,
            contract_name       TEXT,
            yes_token_id        TEXT,
            probability         REAL,
            volume              REAL,
            volume_enabled      INTEGER DEFAULT 1,
            probability_change  REAL,
            volume_change       REAL,
            probability_anomaly INTEGER DEFAULT 0,
            volume_anomaly      INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_mm_contract_ts ON minute_metrics(contract_slug, ts_et);

        CREATE TABLE IF NOT EXISTS alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_et           TEXT NOT NULL,
            date_et         TEXT NOT NULL,
            group_key       TEXT NOT NULL,
            market_slug     TEXT NOT NULL,
            contract_slug   TEXT NOT NULL,
            metric          TEXT,
            old_value       REAL,
            new_value       REAL,
            change_ratio    REAL,
            message         TEXT
        );

        -- contract classification: relevance + direction
        CREATE TABLE IF NOT EXISTS contract_classification (
            contract_slug   TEXT NOT NULL PRIMARY KEY,
            group_key       TEXT NOT NULL,
            relevant        INTEGER NOT NULL DEFAULT 1,   -- 1=保留, 0=剔除
            direction       TEXT DEFAULT '中性',           -- 看涨/看跌/中性
            reject_reason   TEXT DEFAULT '',
            analyzed_at     TEXT DEFAULT (datetime('now'))
        );
        """
    )


@contextmanager
def get_conn_context() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def execute(sql: str, params: tuple | list = ()) -> int:
    with get_conn_context() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def executemany(sql: str, seq: list[tuple]) -> int:
    with get_conn_context() as conn:
        cur = conn.executemany(sql, seq)
        conn.commit()
        return cur.rowcount


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        _ensure_tables(conn)
        conn.commit()


def seed_contracts(contracts: list[dict[str, Any]], group_key: str):
    """Insert/update contracts table."""
    with get_conn() as conn:
        for c in contracts:
            conn.execute(
                """
                INSERT OR IGNORE INTO contracts (group_key, market_slug, contract_slug, contract_name, market_name, yes_token_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_key,
                    c["market_slug"],
                    c["slug"] if "slug" in c else c.get("contract_slug", ""),
                    c["title"],
                    c.get("market_name", ""),
                    c.get("yes_token_id", ""),
                ),
            )
        conn.commit()


def get_all_contracts_for_module(group_key: str) -> list[dict]:
    """Return all distinct contracts (including name) for a module."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT mm.contract_slug, mm.contract_name, mm.market_name, mm.group_key
            FROM minute_metrics mm
            WHERE mm.group_key = ?
            ORDER BY mm.contract_name
            """,
            (group_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_classification_batch(classifications: list[dict]):
    """Bulk upsert classification results.
    Each dict: {contract_slug, group_key, relevant (bool), direction (str), reject_reason (str)}
    """
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO contract_classification (contract_slug, group_key, relevant, direction, reject_reason, analyzed_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(contract_slug) DO UPDATE SET
                relevant=excluded.relevant,
                direction=excluded.direction,
                reject_reason=excluded.reject_reason,
                analyzed_at=excluded.analyzed_at
            """,
            [
                (
                    c["contract_slug"],
                    c["group_key"],
                    int(c["relevant"]),
                    c["direction"],
                    c["reject_reason"],
                )
                for c in classifications
            ],
        )
        conn.commit()


def get_classification(contract_slug: str) -> dict | None:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM contract_classification WHERE contract_slug = ?",
            (contract_slug,),
        ).fetchone()
        return dict(r) if r else None


def get_classifications_for_module(group_key: str, relevant: bool | None = None) -> list[dict]:
    with get_conn() as conn:
        if relevant is not None:
            rows = conn.execute(
                "SELECT * FROM contract_classification WHERE group_key = ? AND relevant = ?",
                (group_key, int(relevant)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM contract_classification WHERE group_key = ?",
                (group_key,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_rejected_contracts() -> list[dict]:
    """Return all rejected contracts (relevant=0) with reason, across all modules."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cc.*, mm.contract_name, mm.market_name
            FROM contract_classification cc
            LEFT JOIN minute_metrics mm ON mm.contract_slug = cc.contract_slug
            WHERE cc.relevant = 0
            GROUP BY cc.contract_slug
            ORDER BY cc.group_key, cc.contract_slug
            """
        ).fetchall()
        return [dict(r) for r in rows]
