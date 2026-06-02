"""
飞书异常告警发送模块（轻量版）
"""

import json
import logging
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import STATE_DIR

logger = logging.getLogger("crawler_minute_task")

FEISHU_APP_ID = "cli_aa8027366778dcba"
FEISHU_APP_SECRET = "iqviiidEzHwSVOuQ0OAKeck7PB8bfQfp"
FEISHU_CHAT_ID = "oc_40593d5279bcbd4494869d0333775321"
FEISHU_DOMAIN = "https://open.feishu.cn"

DB_PATH = STATE_DIR / "prob_monitor.db"

# direction cache
_DIRECTION_CACHE: dict[str, str | None] = {}
_LAST_DIRECTION_LOAD = 0


def _load_directions() -> dict[str, str | None]:
    """load direction mapping from contract_classification"""
    import time
    global _DIRECTION_CACHE, _LAST_DIRECTION_LOAD
    now = time.time()
    if now - _LAST_DIRECTION_LOAD < 60 and _DIRECTION_CACHE:
        return _DIRECTION_CACHE
    _DIRECTION_CACHE = {}
    try:
        with sqlite3.connect(str(DB_PATH)) as db:
            rows = db.execute(
                "SELECT contract_slug, direction FROM contract_classification WHERE relevant = 1"
            ).fetchall()
            for slug, direction in rows:
                _DIRECTION_CACHE[slug] = direction
    except Exception:
        logger.warning("Failed to load directions", exc_info=True)
    _LAST_DIRECTION_LOAD = now
    return _DIRECTION_CACHE


def _resolve_signal(direction: str | None, change: float, contract_slug: str = "") -> str:
    """resolve direction signal from direction + change + contract name"""
    # explicit direction first
    if direction == "bearish":
        return "bearish" if change > 0 else "bullish"
    if direction == "bullish":
        return "bullish" if change > 0 else "bearish"

    # heuristic for bearish contracts
    slug_lower = contract_slug.lower()
    is_bearish_contract = any(kw in slug_lower for kw in [
        "dip-to", "dip_to", "below", "under", "hit-low", "hit_low",
        "closes-below", "closes_below", "settle-below", "settle_below"])

    if is_bearish_contract:
        return "bearish" if change > 0 else "bullish"

    return "bullish" if change > 0 else "bearish"


def _get_token() -> str | None:
    url = f"{FEISHU_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") != 0:
                logger.warning("Feishu get_token failed: %s", result.get("msg"))
                return None
            return result["tenant_access_token"]
    except Exception as e:
        logger.warning("Feishu get_token error: %s", e)
        return None


def _send(chat_id: str, content_json: str) -> bool:
    token = _get_token()
    if not token:
        return False
    url = f"{FEISHU_DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = json.dumps({"receive_id": chat_id, "msg_type": "text", "content": content_json}).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") != 0:
                logger.warning("Feishu send failed: %s", result.get("msg"))
                return False
            logger.info("Feishu alert sent, msg_id=%s", result.get("data", {}).get("message_id", "?"))
            return True
    except Exception as e:
        logger.warning("Feishu send error: %s", e)
        return False


_MODULES = {"gold": "Gold", "oil": "Oil", "silver": "Silver", "copper": "Copper", "natgas": "NatGas", "ipo": "IPO"}
_MOD_ORDER = {"gold": 0, "oil": 1, "silver": 2, "copper": 3, "natgas": 4, "ipo": 5}


def format_alert_message(alerts: list[dict]) -> str:
    """format alerts into Feishu message"""
    if not alerts:
        return ""

    groups: dict[str, list[dict]] = {}
    for a in alerts:
        gk = a.get("group_key", "other")
        if gk not in groups:
            groups[gk] = []
        groups[gk].append(a)

    lines = ["[ALERT]"]
    sorted_groups = sorted(groups.items(), key=lambda kv: _MOD_ORDER.get(kv[0], 99))

    for gk, items in sorted_groups:
        label = _MODULES.get(gk, gk)
        items_sorted = sorted(items, key=lambda x: x.get("ts_et", ""), reverse=True)
        ts_display = items_sorted[0].get("ts_et", "")
        if ts_display:
            try:
                dt = ts_display.split("T")[1].split("-")[0]
                ts_display = dt[:5]
            except Exception:
                ts_display = ""
        lines.append(f"\n[{label}] {ts_display}")

        for a in items:
            name = a.get("contract_name", "?")
            old = a.get("old_value")
            new = a.get("new_value")
            change = a.get("change_ratio")
            slug = a.get("contract_slug", "")
            chg = float(change) if change is not None else 0
            up_or_down = "+" if chg > 0 else "-"

            directions = _load_directions()
            slug_dir = directions.get(slug)
            signal = _resolve_signal(slug_dir, chg, slug)
            signal_icon = "B" if signal == "bullish" else "b"

            old_str = f"{float(old)*100:.1f}" if old is not None else "?"
            new_str = f"{float(new)*100:.1f}" if new is not None else "?"
            chg_str = f"{chg*100:+.1f}" if change is not None else "?"

            lines.append(f"  {name} -> {old_str}% -> {new_str}% ({chg_str}) {up_or_down} {signal_icon}")

    text = "\n".join(lines)
    if len(text) > 2800:
        text = text[:2777] + "\n... (truncated)"

    return json.dumps({"text": text})


def send_feishu_alerts(alerts: list[dict]) -> int:
    if not alerts:
        return 0
    content_json = format_alert_message(alerts)
    if not content_json:
        return 0
    ok = _send(FEISHU_CHAT_ID, content_json)
    return len(alerts) if ok else 0
