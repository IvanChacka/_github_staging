from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.logger import get_logger
from config.settings import MODULE_CONFIG
from processor.database import init_db, get_all_contracts_for_module, save_classification_batch
from processor.llm_analyzer import analyze_relevance, analyze_direction


logger = get_logger("classify")

_MAX_WORKERS = 8

# ── 规则过滤（LLM 降级时兜底） ──
_EXCLUDE_PATTERNS = [
    "golden boot", "golden glove", "mls", "nhl", "nba", "wnba",
    "golden state", "golden knights", "golden bulls", "golden kings",
    "goldman sachs", "goldman", "jared golden", "dan goldman",
    "trump.*golden", "trump.*gold",
    "gold medal", "golden dome",
    # 天然气误匹配
    "nigerian national", "nigeria", "niger",
]


def _rule_check_relevant(contract_name: str, market_name: str, contract_slug: str) -> tuple[bool, str]:
    """规则级别判断是否相关，返回 (relevant, reason)。"""
    text = f"{contract_name} {market_name} {contract_slug}".lower()
    for pat in _EXCLUDE_PATTERNS:
        if re.search(pat, text):
            return False, f"规则剔除: '{pat}'"
    return True, ""


def _rule_guess_direction(contract_name: str) -> str:
    """规则级方向猜测。
    优先匹配具体模式，按优先级匹配：
    1. 明确看跌：hit-low/dip-to/settle below/closes below/under
    2. 明确看涨：hit-high/closes above/settle above/over
    3. range/中性：不明确或范围型返回中性
    """
    name = contract_name.lower()
    # 看跌信号（优先级高，避免 hit-low 误判为中性）
    bearish = [
        r"hit\s*\(low\)", r"hit\s+low\b", r"dip\s*to\b", r"dip_to\b",
        r"closes below", r"closes_below",
        r"settle below", r"settle_below", r"settle under", r"settle_under",
        r"low\s+\$?\d", r"<[\$]?\d", r"under\s+\$?\d",
        r"drop\b", r"crash\b", r"fall\b", r"decline\b",
    ]
    # 看涨信号
    bullish = [
        r"hit\s*\(high\)", r"hit\s+high\b", r"closes above", r"closes_above",
        r"settle above", r"settle_above", r"settle over", r"settle_over",
        r"high\s+\$?\d", r">[\$]?\d", r"over\s+\$?\d",
        r"reach\b", r"break above", r"rally\b", r"surge\b",
    ]
    for pat in bearish:
        if re.search(pat, name):
            return "看跌"
    for pat in bullish:
        if re.search(pat, name):
            return "看涨"
    # 范围型和剩余的返回中性
    return "中性"


def _get_keyword_for_module(module_key: str) -> str:
    """Get the first keyword from module config."""
    cfg = MODULE_CONFIG.get(module_key, {})
    keywords = cfg.get("keywords", [module_key])
    return keywords[0] if keywords else module_key


def _classify_single(contract: dict, module_key: str, keyword: str) -> dict:
    """Classify a single contract (for thread pool).
    先用 LLM 判断相关性与方向，LLM 不可用时走规则兜底。
    """
    contract_slug = contract["contract_slug"]
    contract_name = contract["contract_name"] or ""
    market_name = contract["market_name"] or ""

    # LLM 判断相关性
    llm_result = analyze_relevance(contract_name, market_name, module_key, keyword)
    llm_relevant = llm_result.get("relevant", True)
    llm_reason = llm_result.get("reason", "")

    if not llm_relevant:
        return {
            "contract_slug": contract_slug,
            "group_key": module_key,
            "relevant": False,
            "direction": "中性",
            "reject_reason": f"LLM剔除: {llm_reason}",
        }

    # LLM 认为相关 → 用 LLM 分析方向
    direction = analyze_direction(contract_name)

    # 规则兜底：再检查一下排除词（避免 LLM 漏掉明显不相关的）
    rule_relevant, rule_reason = _rule_check_relevant(contract_name, market_name, contract_slug)
    if not rule_relevant:
        return {
            "contract_slug": contract_slug,
            "group_key": module_key,
            "relevant": False,
            "direction": "中性",
            "reject_reason": f"规则剔除（LLM通过）: {rule_reason}",
        }

    return {
        "contract_slug": contract_slug,
        "group_key": module_key,
        "relevant": True,
        "direction": direction,
        "reject_reason": "",
    }


def run():
    init_db()

    total_analyzed = 0
    total_relevant = 0
    total_rejected = 0
    classifications: list[dict] = []

    # IPO 模块不需要方向分类，直接标记相关+中性
    IPO_MODULES = {"ipo"}

    for module_key in MODULE_CONFIG:
        contracts = get_all_contracts_for_module(module_key)
        keyword = _get_keyword_for_module(module_key)

        if module_key in IPO_MODULES:
            # IPO 模块：跳过 LLM 分析，全部标记为 related + 中性
            for c in contracts:
                classifications.append({
                    "contract_slug": c["contract_slug"],
                    "group_key": module_key,
                    "relevant": True,
                    "direction": "中性",
                    "reject_reason": "",
                })
                total_analyzed += 1
                total_relevant += 1
            logger.info("IPO module '%s' auto-classified %d contracts as neutral", module_key, len(contracts))
            continue

        logger.info("Analyzing %d contracts for module '%s' (workers=%d)", len(contracts), module_key, _MAX_WORKERS)

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_classify_single, contract, module_key, keyword): contract
                for contract in contracts
            }
            for idx, future in enumerate(as_completed(futures), start=1):
                try:
                    result = future.result()
                    classifications.append(result)
                    if result["relevant"]:
                        total_relevant += 1
                    else:
                        total_rejected += 1
                    total_analyzed += 1
                    if idx % 50 == 0 or idx == len(contracts):
                        logger.info("Module '%s' progress %d/%d", module_key, idx, len(contracts))
                except Exception as exc:
                    contract = futures[future]
                    logger.exception("Failed to classify %s: %s", contract["contract_slug"], exc)

    # Save all
    if classifications:
        save_classification_batch(classifications)

    logger.info(
        "Classification complete: analyzed=%d relevant=%d rejected=%d",
        total_analyzed,
        total_relevant,
        total_rejected,
    )
    return {
        "analyzed": total_analyzed,
        "relevant": total_relevant,
        "rejected": total_rejected,
    }


if __name__ == "__main__":
    run()
