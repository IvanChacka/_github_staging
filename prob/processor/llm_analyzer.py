"""Rewrite generate_module_analysis to be a single-call unified analyzer"""

import json
import time
from typing import Any

from openai import OpenAI

from config.logger import get_logger
from config.settings import (
    LLM_API_KEY_PATH,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
    MODULE_CONFIG,
)

logger = get_logger("llm_analyzer")

# ---------- cache ----------
_ANALYSIS_CACHE: dict[str, dict] = {}  # contract_slug -> {relevance, direction, reason}


def _read_api_key() -> str:
    try:
        return LLM_API_KEY_PATH.read_text().strip()
    except FileNotFoundError:
        logger.error("LLM API key file not found at %s", LLM_API_KEY_PATH)
        return ""


def _build_client() -> OpenAI | None:
    key = _read_api_key()
    if not key:
        return None
    return OpenAI(api_key=key, base_url=LLM_BASE_URL)


# ---------- prompts ----------

_RELEVANCE_PROMPT = """你是一个 Polymarket 合约筛选器。以下合约通过关键词 '{keyword}' 被收录到 '{module_name}' 模块。

判断该合约是否真正与 '{module_name}' 的商品现货/期货价格预测直接相关。
限定条件：
1. 必须是在预测 {module_name} 自身的价格/价格区间（例如 gold/silver/oil/copper 的价格、涨跌、点位、均价等）
2. 排除那些关键词仅为比喻、别名、人名、球队名、地缘事件、政治人物名称中包含关键词但实际与商品价格无关的合约
3. 排除预测政治人物是否提及某个名词的合约（如 "Trump says Golden Dome"）
4. 排除预测与商品本身无关的公司相关合约（如 "Goldman Sachs"）
5. 排除体育奖项（如 "MLS Golden Boot"、"Golden Glove" 等所有体育类奖项）
6. 排除含 "Golden" 的体育队名（如 "Golden State Warriors"、"Vegas Golden Knights"、"Golden Bulls" 等）
7. 排除含 "Gold" 或 "Golden" 的政治人物名（如 "Jared Golden"、"Dan Goldman" 等）

关键词本身是 {keyword}，如果合约名中的 "gold" 或 "{keyword}" 是作为球队名、人名、奖项名的一部分出现（而不是在谈论贵金属商品价格），则视为不相关。

合约名: {contract_name}
合约市场: {market_name}

请返回严格的 JSON，不要多余文字：
{{"relevant": true/false, "reason": "保留/剔除的简短理由（15字以内）"}}
"""

_DIRECTION_PROMPT = """你是一个 Polymarket 合约方向分析器。分析以下合约名称，判断其 Yes 代表"看涨"还是"看跌"。
规则：
- 如果 Yes 表示价格/资产上涨、达到更高价位、突破上方 → "看涨"
- 如果 Yes 表示价格/资产下跌、跌至更低价位、跌破下方 → "看跌"
- 注意像 "Will X hit HIGH $Y" 的 HIGH 合约 Yes 是到高位，所以是看涨
- "Will X hit LOW $Y" 的 LOW 合约 Yes 是到低位，所以是看跌
- "Will X settle over $Y" Yes 是超过某价位 → 看涨；"settle under $Y" → 看跌
- 如果无法判断（如既不看涨也不看跌），返回 "中性"

合约名: {contract_name}

请返回严格的 JSON，不要多余文字：
{{"direction": "看涨"/"看跌"/"中性"}}
"""

# ── Unified Analysis Prompt (all 4 modules in one call) ──

_MODULE_LABELS = {"gold": "黄金", "oil": "原油", "silver": "白银", "natgas": "天然气"}

_UNIFIED_ANALYSIS_PROMPT = """你是一位专业的商品期货市场分析师。以下是 Polymarket 预测市场中四个商品模块在过去半小时内的异常合约数据。

请针对{modules_prompt}这四个模块，分析各自的异常合约信号，并结合近期市场新闻、地缘政治、宏观经济等因素，生成一份专业的市场分析报告。

注意：只对存在异常合约的模块输出分析。

异常合约数据如下：

{all_contracts}

请返回严格的 JSON 格式（不要多余文字），每个存在异常的模块一个条目：

{{
    "gold": {{
        "news_titles": ["最相关的新闻标题1", "最相关的新闻标题2"],
        "analysis": "综合分析（150字以内，中文，分析群体信号、市场情绪和可能驱动因素，结合异常数据说话）",
        "suggestion": "操作建议（80字以内，中文，具体可行的观察标的或交易策略参考，用词专业风趣）"
    }},
    "oil": {{
        "news_titles": ["最相关的新闻标题1", "最相关的新闻标题2"],
        "analysis": "...",
        "suggestion": "..."
    }},
    "silver": {{ ... }},
    "natgas": {{ ... }}
}}

注意：
1. 只输出存在异常合约的模块，没有异常则省略该字段
2. news_titles 只输出新闻标题文字，不要包含任何 URL！
3. analysis 要基于实际数据说话，结合预测市场概率变化和宏观经济背景
4. suggestion 要具体可行，给出明确的观察标的
"""


# ---------- module entry URLs (real, not LLM-hallucinated) ----------

_MODULE_ENTRY_URLS = {
    "gold": [
        ("路孚特 — 贵金属", "https://www.reuters.com/markets/commodities/metals/"),
        ("金十数据 — 黄金", "https://www.jin10.com/"),
    ],
    "oil": [
        ("路孚特 — 能源", "https://www.reuters.com/business/energy/"),
        ("金十数据 — 原油", "https://www.jin10.com/"),
    ],
    "silver": [
        ("路孚特 — 贵金属", "https://www.reuters.com/markets/commodities/metals/"),
        ("金十数据 — 白银", "https://www.jin10.com/"),
    ],
    "natgas": [
        ("路孚特 — 能源", "https://www.reuters.com/business/energy/"),
        ("金十数据 — 天然气", "https://www.jin10.com/"),
    ],
}


# ---------- helpers ----------

def _call_llm(prompt: str, retries: int = 2) -> dict[str, Any] | None:
    client = _build_client()
    if not client:
        return None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout=LLM_TIMEOUT,
                max_tokens=1500,
            )
            text = resp.choices[0].message.content.strip()
            text = text.removeprefix("```json").removesuffix("```").strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                import re as _re
                m = _re.search(r'\{[^{}]*"gold".*\}', text, _re.DOTALL)
                if m:
                    return json.loads(m.group())
                raise
        except Exception as exc:
            err_str = str(exc)
            if "402" in err_str or "Insufficient Balance" in err_str or "401" in err_str or "unauthorized" in err_str.lower():
                logger.warning("LLM call failed (non-retryable): %s", err_str[:80])
                return None
            logger.warning("LLM call attempt %d failed: %s", attempt + 1, exc)
            if attempt < retries:
                time.sleep(1)
    logger.warning("LLM call exhausted retries, returning None")
    return None


def _title_urls(module_key: str, titles: list[str]) -> list[dict]:
    """Map LLM-generated news titles to real entry URLs."""
    entries = _MODULE_ENTRY_URLS.get(module_key, [("路孚特 — 商品", "https://www.reuters.com/markets/commodities/")])
    result = []
    for i, title in enumerate(titles):
        url = entries[i][1] if i < len(entries) else entries[0][1]
        result.append({"title": title, "url": url})
    return result


def _default_result(module_key: str) -> dict:
    """Fallback result when LLM is unavailable."""
    entries = _MODULE_ENTRY_URLS.get(module_key, [])
    return {
        "news_links": [{"title": t, "url": u} for t, u in entries[:2]],
        "analysis": "LLM 分析暂不可用。",
        "suggestion": "建议关注市场动态，设置合理止损。",
    }


# ---------- public API ----------

def analyze_relevance(
    contract_name: str,
    market_name: str,
    module_key: str,
    keyword: str,
) -> dict[str, Any]:
    cache_key = f"{module_key}::{contract_name}"
    if cache_key in _ANALYSIS_CACHE:
        return _ANALYSIS_CACHE[cache_key]

    module_label = MODULE_CONFIG[module_key]["label"]
    prompt = _RELEVANCE_PROMPT.format(
        keyword=keyword,
        module_name=module_label,
        contract_name=contract_name,
        market_name=market_name,
    )
    result = _call_llm(prompt)
    if result is None:
        result = {"relevant": True, "reason": "保留（LLM不可用）"}
    else:
        result.setdefault("relevant", True)
        result.setdefault("reason", "")
    _ANALYSIS_CACHE[cache_key] = result
    return result


def analyze_direction(contract_name: str) -> str:
    prompt = _DIRECTION_PROMPT.format(contract_name=contract_name)
    result = _call_llm(prompt)
    if result is None:
        return "中性"
    return result.get("direction", "中性")


def generate_all_analyses(
    all_contracts: dict[str, list[dict]],
) -> dict[str, dict]:
    """
    One LLM call to analyze all 4 modules at once.
    
    Args:
        all_contracts: {module_key: [contract_dict, ...]}
                       each contract has: name/contract_name, prob_chg/probability_change, direction
        
    Returns:
        {module_key: {"news_links": [...], "analysis": "...", "suggestion": "..."}}
    """
    # Filter to modules that actually have anomalies
    active_modules = {mk: items for mk, items in all_contracts.items() if items}
    if not active_modules:
        return {}

    # Build contract data text per module
    module_sections = []
    module_labels = []
    for mk, items in active_modules.items():
        label = _MODULE_LABELS.get(mk, mk)
        module_labels.append(label)
        lines = []
        for i, c in enumerate(items, 1):
            name = c.get("contract_name", c.get("name", "未知"))
            chg = c.get("probability_change", c.get("prob_chg", 0))
            direction = c.get("direction", "中性")
            chg_pct = chg * 100 if abs(chg) < 10 else chg
            lines.append(f"{i}. {name[:80]} | 概率变化: {chg_pct:+.1f}% | 原始方向: {direction}")
        module_sections.append(f"### {label}\n" + "\n".join(lines))

    all_text = "\n\n".join(module_sections)
    modules_str = "、".join(module_labels)

    prompt = _UNIFIED_ANALYSIS_PROMPT.format(
        modules_prompt=modules_str,
        all_contracts=all_text,
    )

    result = _call_llm(prompt, retries=2)

    # Map LLM result to our format
    output = {}
    for mk in active_modules:
        label = _MODULE_LABELS.get(mk, mk)
        if result and mk in result:
            mod = result[mk]
            titles = mod.get("news_titles", [])
            output[mk] = {
                "news_links": _title_urls(mk, titles),
                "analysis": mod.get("analysis", "分析暂缺"),
                "suggestion": mod.get("suggestion", "建议暂缺"),
            }
        else:
            # LLM didn't return this module — use fallback
            output[mk] = _default_result(mk)

    return output


def generate_module_analysis(module_key: str, abnormal_contracts: list[dict]) -> dict:
    """
    Legacy single-module wrapper. For backward compatibility.
    """
    all_data = {module_key: abnormal_contracts}
    all_results = generate_all_analyses(all_data)
    if module_key not in all_results:
        return _default_result(module_key)
    return all_results[module_key]


def clear_cache():
    _ANALYSIS_CACHE.clear()
