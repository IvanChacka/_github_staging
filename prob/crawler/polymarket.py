from __future__ import annotations

import time
from typing import Any

from config.helpers import safe_json_loads, to_float
from config.logger import get_logger
from config.settings import (
    DISCOVERY_MAX_PAGES,
    DISCOVERY_PAGE_SIZE,
    KEYWORDS,
    MODULE_CONFIG,
    POLYMARKET_CLOB,
    POLYMARKET_GAMMA,
    REQUEST_SLEEP_SECONDS,
)
from crawler.http_client import build_session

logger = get_logger("crawler_polymarket")
session = build_session()


def _parse_tags(obj: dict[str, Any]) -> list[str]:
    candidates = []
    for key in ["tags", "eventTags", "categories"]:
        raw = obj.get(key)
        if not raw:
            continue
        if isinstance(raw, list):
            candidates.extend(raw)
        else:
            candidates.append(raw)

    names: list[str] = []
    seen = set()
    for item in candidates:
        if isinstance(item, dict):
            name = item.get("label") or item.get("name") or item.get("slug") or item.get("title") or item.get("id")
        else:
            name = str(item)
        if name:
            name = str(name).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _text_matches_keywords(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in KEYWORDS)


def _classify_module(text: str) -> list[str]:
    lowered = text.lower()
    matched = []
    for module_key, cfg in MODULE_CONFIG.items():
        if any(keyword in lowered for keyword in cfg["keywords"]):
            matched.append(module_key)
    return matched


def _is_binary_yes_no_market(market: dict[str, Any]) -> tuple[bool, str | None]:
    outcomes = safe_json_loads(market.get("outcomes"))
    token_ids = safe_json_loads(market.get("clobTokenIds"))
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return False, None

    lowered = [str(x).strip().lower() for x in outcomes]
    if not (len(lowered) == 2 and set(lowered) <= {"yes", "no", "y", "n"}):
        return False, None

    yes_idx = None
    for candidate in ("yes", "y"):
        if candidate in lowered:
            yes_idx = lowered.index(candidate)
            break
    if yes_idx is None or yes_idx >= len(token_ids):
        return False, None
    return True, str(token_ids[yes_idx])


def discover_contracts() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for page in range(DISCOVERY_MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "limit": DISCOVERY_PAGE_SIZE,
            "offset": page * DISCOVERY_PAGE_SIZE,
        }
        response = session.get(f"{POLYMARKET_GAMMA}/events", params=params, timeout=session.request_timeout)
        response.raise_for_status()
        data = response.json()
        events = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []

        if not events:
            logger.info("Gamma events empty at page=%s, stop", page)
            break

        logger.info("Fetched gamma events page=%s count=%s", page, len(events))

        for event in events:
            event_text = " ".join(
                str(x or "")
                for x in [
                    event.get("title"),
                    event.get("question"),
                    event.get("slug"),
                    event.get("category"),
                    event.get("seriesSlug"),
                    " ".join(_parse_tags(event)),
                ]
            )

            for market in event.get("markets") or []:
                if str(market.get("closed", "")).strip().lower() == "true":
                    continue
                if str(market.get("active", "true")).strip().lower() in {"false", "0", "no"}:
                    continue

                is_binary, yes_token_id = _is_binary_yes_no_market(market)
                if not is_binary or not yes_token_id:
                    continue

                market_text = " ".join(
                    str(x or "")
                    for x in [
                        event_text,
                        market.get("question"),
                        market.get("title"),
                        market.get("slug"),
                        " ".join(_parse_tags(market)),
                    ]
                )

                if not _text_matches_keywords(market_text):
                    continue

                module_keys = _classify_module(market_text)
                if not module_keys:
                    continue

                for module_key in module_keys:
                    contract_slug = str(market.get("slug") or market.get("conditionId") or market.get("id"))
                    unique_key = (module_key, contract_slug)
                    if unique_key in seen:
                        continue
                    seen.add(unique_key)
                    rows.append(
                        {
                            "group_key": module_key,
                            "market_slug": str(event.get("slug") or event.get("id") or ""),
                            "market_name": str(event.get("title") or event.get("question") or ""),
                            "contract_slug": contract_slug,
                            "contract_name": str(market.get("question") or market.get("title") or contract_slug),
                            "yes_token_id": yes_token_id,
                            "event_id": str(event.get("id") or ""),
                            "condition_id": str(market.get("conditionId") or ""),
                            "tags": ";".join(sorted(set(_parse_tags(event) + _parse_tags(market)))),
                        }
                    )
                time.sleep(REQUEST_SLEEP_SECONDS)

    logger.info("Discovery finished rows=%s", len(rows))
    return rows


def check_contract_closed(contract_slug: str) -> bool | None:
    """通过 Gamma API 检查合约是否已结算关闭。返回 True=已关闭, False=未关闭, None=查询失败"""
    try:
        response = session.get(
            f"{POLYMARKET_GAMMA}/markets",
            params={"slug": contract_slug},
            timeout=session.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        markets = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), list) else [])
        if markets and isinstance(markets, list) and len(markets) > 0:
            m = markets[0]
            closed = str(m.get("closed", "")).strip().lower()
            active = str(m.get("active", "true")).strip().lower()
            return closed == "true" or active in {"false", "0", "no"}
        return None
    except Exception:
        return None


def fetch_midpoint(token_id: str) -> float | None:
    response = session.get(
        f"{POLYMARKET_CLOB}/midpoint",
        params={"token_id": token_id},
        timeout=session.request_timeout,
    )
    response.raise_for_status()
    data = response.json()
    return to_float(data.get("mid") or data.get("mid_price") or data.get("price"))


def fetch_price_history(token_id: str, start_ts: int, end_ts: int, fidelity: int = 1) -> list[dict[str, Any]]:
    response = session.get(
        f"{POLYMARKET_CLOB}/prices-history",
        params={"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity},
        timeout=session.request_timeout,
    )
    response.raise_for_status()
    data = response.json()
    history = data.get("history") or []
    return history if isinstance(history, list) else []


def fetch_volume(condition_id: str | None, market_slug: str | None) -> float | None:
    if not condition_id and not market_slug:
        return None

    # 先用 contract slug 查 Gamma API（合约级别的 slug，更精确）
    # condition_id 可能是事件级的，多个合约共享同一个，导致 volume 重复
    candidates = []
    if market_slug:
        candidates.append({"slug": market_slug})
    if condition_id:
        candidates.append({"condition_id": condition_id})

    last_error = None
    for params in candidates:
        try:
            response = session.get(f"{POLYMARKET_GAMMA}/markets", params=params, timeout=session.request_timeout)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                if not payload:
                    continue
                market = payload[0]
            elif isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, list):
                    if not data:
                        continue
                    market = data[0]
                else:
                    market = payload
            else:
                continue
            if not isinstance(market, dict):
                continue
            value = to_float(
                market.get("volume24hrClob")
                or market.get("volume24hr")
                or market.get("volume_24hr")
                or market.get("volume24h")
                or market.get("oneDayVolume")
                or market.get("volume")
            )
            if value is not None:
                return value
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return None
