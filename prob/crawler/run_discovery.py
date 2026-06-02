from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.logger import get_logger
from config.time_utils import now_et
from crawler.polymarket import discover_contracts
from processor.database import init_db
from processor.storage import save_contracts

logger = get_logger("crawler_discovery_task")


def run() -> list[dict]:
    init_db()
    discovered_at_et = now_et().replace(second=0, microsecond=0).isoformat()
    logger.info("Start discovery discovered_at=%s", discovered_at_et)
    rows = discover_contracts()
    save_contracts(rows, discovered_at_et)
    logger.info("Finish discovery count=%s", len(rows))
    return rows


if __name__ == "__main__":
    run()
