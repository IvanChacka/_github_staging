#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
macro_forecast.py — 宏观经济板块抓取 + DeepSeek 综合分析
独立文件，不修改现有代码。
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from openai import OpenAI  # noqa: E402

from config.logger import get_logger  # noqa: E402
from config.settings import (  # noqa: E402
    BASE_DIR,
    LLM_API_KEY_PATH,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
    STATE_DIR,
)
# ─── 发送目标群 ──────────────────────────────────
MACRO_CHAT_ID = "oc_40593d5279bcbd4494869d0333775321"
FEISHU_DOMAIN = "https://open.feishu.cn"
FEISHU_APP_ID_VAL = "cli_aa8027366778dcba"
FEISHU_APP_SECRET_VAL = "iqviiidEzHwSVOuQ0OAKeck7PB8bfQfp"
# ─────────────────────────────────────────────

logger = get_logger("macro_forecast")

# ─── 板块配置 ──────────────────────────────────────

MACRO_GROUPS: dict[str, dict[str, Any]] = {
    "geopolitics_us_china": {
        "label": "地缘政治—中美",
        "keywords": ["china", "taiwan", "south china sea", "tariff", "trade war",
                     "semiconductor", "export control", "us-china"],
    },
    "geopolitics_middle_east": {
        "label": "地缘政治—中东",
        "keywords": ["israel", "iran", "middle east", "gaza", "hezbollah",
                     "houthi", "red sea", "strait of hormuz"],
    },
    "fed_rates": {
        "label": "美联储利率",
        "keywords": ["fed", "federal reserve", "interest rate", "rate cut",
                     "rate hike", "fomc", "fed funds"],
    },
    "forex": {
        "label": "外汇",
        "keywords": ["eurusd", "usdjpy", "gbpusd", "dollar index", "dxy",
                     "fx", "forex", "yuan", "cny", "euro", "yen"],
    },
    "indices": {
        "label": "股指",
        "keywords": ["s&p", "sp500", "nasdaq", "dow jones", "russell",
                     "vix", "stock market", "equity"],
    },
    "trump": {
        "label": "特朗普",
        "keywords": ["trump", "donald trump", "election", "trump tariff"],
    },
}

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ─── 小工具 ────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (ValueError, TypeError):
        return None


def _is_binary_yes_no(market: dict) -> tuple[bool, str | None]:
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            return False, None
    token_ids = market.get("clobTokenIds")
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            return False, None
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return False, None
    if len(outcomes) != 2 or len(token_ids) != 2:
        return False, None
    lowered = [s.strip().lower() if isinstance(s, str) else str(s).strip().lower() for s in outcomes]
    if set(lowered) != {"yes", "no"}:
        return False, None
    yes_idx = lowered.index("yes")
    return True, str(token_ids[yes_idx])


def _text_contains(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in keywords)


def _read_llm_key() -> str:
    try:
        return LLM_API_KEY_PATH.read_text().strip()
    except FileNotFoundError:
        logger.error("LLM key not found at %s", LLM_API_KEY_PATH)
        return ""


def _build_llm_client() -> OpenAI | None:
    key = _read_llm_key()
    return OpenAI(api_key=key, base_url=LLM_BASE_URL) if key else None


# ─── 核心 ──────────────────────────────────────────

def _fetch_one_midpoint(token_id: str) -> float | None:
    """Fetch a single midpoint."""
    try:
        resp = requests.get(f"{CLOB_API}/midpoint", params={"token_id": token_id},
                            timeout=10, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return _to_float(resp.json().get("mid") or resp.json().get("mid_price") or resp.json().get("price"))
    except Exception:
        return None


def _fetch_midpoints_batch(token_ids: list[str], max_workers: int = 8) -> dict[str, float | None]:
    """Concurrently fetch midpoints for many tokens."""
    results: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(_fetch_one_midpoint, tid): tid for tid in token_ids}
        for fut in as_completed(fut_map):
            tid = fut_map[fut]
            try:
                results[tid] = fut.result()
            except Exception:
                results[tid] = None
    return results


def fetch_macro_contracts() -> dict[str, list[dict]]:
    """
    抓取宏观经济各板块合约及当前概率。
    返回 {group_key: [contract_dict, ...]}
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "macro-forecast/1.0", "Accept": "application/json"})

    # Step 1: 使用 Gamma API tag 参数搜索，减少数据量
    all_markets: list[dict] = []
    tags_to_fetch = ["politics", "economics", "finance", "fed", "election", "forex"]
    for tag in tags_to_fetch:
        for page in range(2):
            params = {"tag": tag, "active": "true", "closed": "false", "limit": 100, "offset": page * 100}
            try:
                resp = session.get(f"{GAMMA_API}/markets", params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    all_markets.extend(data)
                time.sleep(0.1)
            except Exception as e:
                logger.warning("Tag '%s' page %d failed: %s", tag, page, e)
                break

    # 去重
    seen_slug: set[str] = set()
    unique_markets: list[dict] = []
    for m in all_markets:
        slug = str(m.get("slug") or m.get("conditionId") or "")
        if slug and slug not in seen_slug:
            seen_slug.add(slug)
            unique_markets.append(m)

    logger.info("Fetched %d unique markets from Gamma API", len(unique_markets))

    # Step 2: 匹配板块
    group_contracts: dict[str, list[dict]] = {gk: [] for gk in MACRO_GROUPS}
    all_token_ids: list[str] = []
    token_to_group: dict[str, str] = {}

    for market in unique_markets:
        # 过滤无效
        if str(market.get("closed", "")).strip().lower() == "true":
            continue
        if str(market.get("active", "true")).strip().lower() in ("false", "0", "no"):
            continue

        is_binary, yes_token_id = _is_binary_yes_no(market)
        if not is_binary or not yes_token_id:
            continue

        # 构建搜索文本
        text = " ".join(str(x or "") for x in [
            market.get("question", ""),
            market.get("title", ""),
            market.get("slug", ""),
            market.get("eventSlug", ""),
        ])

        # 匹配哪个组
        matched = None
        for gk, cfg in MACRO_GROUPS.items():
            if _text_contains(text, cfg["keywords"]):
                matched = gk
                break

        if not matched:
            continue

        contract_name = str(market.get("question") or market.get("title") or "")
        group_contracts[matched].append({
            "contract_name": contract_name,
            "contract_slug": str(market.get("slug", "")),
            "yes_token_id": yes_token_id,
            "market_name": str(market.get("question", "")[:60]),
        })
        all_token_ids.append(yes_token_id)
        token_to_group[yes_token_id] = matched

    logger.info("Matched %d contracts to macro groups", len(all_token_ids))

    # Step 3: 并发抓取概率
    if all_token_ids:
        probs = _fetch_midpoints_batch(all_token_ids, max_workers=10)
        for gk in group_contracts:
            for c in group_contracts[gk]:
                c["probability"] = probs.get(c["yes_token_id"])

    # Step 4: 排序+截断
    for gk in group_contracts:
        group_contracts[gk].sort(key=lambda x: x.get("probability") or 0, reverse=True)
        group_contracts[gk] = group_contracts[gk][:10]

    # 日志
    for gk, items in group_contracts.items():
        logger.info("Macro '%s': %d contracts", MACRO_GROUPS[gk]["label"], len(items))

    return group_contracts


# ─── DeepSeek Prompt ────────────────────────────────

ANALYSIS_PROMPT = """你是一位专业的宏观经济与商品期货市场分析师。以下是 Polymarket 预测市场中六大宏观板块的合约当前概率数据：

{module_data}

请分析这些宏观经济信号，综合考虑它们对黄金、原油、白银、天然气四大商品的可能影响。
注意：
1. 在 macro_summary 和各商品影响的文字中，直接引用合约名和具体概率数值，让老板一眼看到重点
2. 在分析句子中标注概率，例如："台湾问题概率50.5%→地缘风险升温"、"民主党控制众议院概率80.5%→政策左倾预期"
3. 根据概率高低使用不同表情：≥80%用 🔥，50-79%用 ⚡，20-49%用 👀，<20%用 💤

返回严格的 JSON，不要多余文字：
{{
    "macro_summary": "宏观经济环境概览（200字以内，中文，包含关键合约概率）",
    "gold_impact": "对黄金的影响（150字以内，包含具体概率引用）",
    "oil_impact": "对原油的影响（150字以内，包含具体概率引用）",
    "silver_impact": "对白银的影响（150字以内，包含具体概率引用）",
    "natgas_impact": "对天然气的影响（150字以内，包含具体概率引用）",
    "key_risks": ["risk1（含概率）", "risk2（含概率）", "risk3（含概率）"],
    "overall_outlook": "整体展望（80字以内）"
}}
"""


def _call_llm(prompt: str, max_tokens: int = 2000, retries: int = 2) -> dict | None:
    client = _build_llm_client()
    if not client:
        return None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL, messages=[{"role": "user", "content": prompt}],
                temperature=0.0, timeout=LLM_TIMEOUT, max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content.strip()
            text = text.removeprefix("```json").removesuffix("```").strip()
            return json.loads(text)
        except Exception as e:
            err = str(e)
            if any(x in err for x in ("402", "Insufficient Balance", "401")):
                return None
            logger.warning("LLM attempt %d failed: %s", attempt + 1, err[:80])
            if attempt < retries:
                time.sleep(1)
    return None


def analyze_macro_impact(macro_data: dict[str, list[dict]]) -> dict:
    """Send macro data to LLM and get structured analysis."""
    sections = []
    total = 0
    for gk, cfg in MACRO_GROUPS.items():
        items = macro_data.get(gk, [])
        if not items:
            continue
        total += len(items)
        lines = [f"### {cfg['label']}"]
        for c in items:
            p = c.get("probability")
            ps = f"{p:.1%}" if p is not None else "N/A"
            lines.append(f"  {c['contract_name'][:70]} → {ps}")
        sections.append("\n".join(lines))

    if total == 0:
        return {"macro_summary": "无可用合约数据。", "gold_impact": "—", "oil_impact": "—",
                "silver_impact": "—", "natgas_impact": "—", "key_risks": [], "overall_outlook": "—"}

    prompt = ANALYSIS_PROMPT.format(module_data="\n\n".join(sections))
    logger.info("LLM analyzing %d macro contracts...", total)
    result = _call_llm(prompt)

    if not result:
        result = {"macro_summary": "LLM 暂不可用。", "gold_impact": "—", "oil_impact": "—",
                  "silver_impact": "—", "natgas_impact": "—",
                  "key_risks": ["LLM 服务暂不可用"], "overall_outlook": "注意市场波动。"}

    result["_contracts"] = macro_data
    return result


def format_report(analysis: dict) -> str:
    """Human-readable text report with emoji highlights."""
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "   🔬 Polymarket 宏观经济分析",
        f"   🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} CST",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "📌 宏观经济概览",
        "  " + analysis.get("macro_summary", "N/A"),
        "",
        "🏅 对黄金的影响",
        "  " + analysis.get("gold_impact", "N/A"),
        "",
        "🛢️ 对原油的影响",
        "  " + analysis.get("oil_impact", "N/A"),
        "",
        "🥈 对白银的影响",
        "  " + analysis.get("silver_impact", "N/A"),
        "",
        "🔥 对天然气的影响",
        "  " + analysis.get("natgas_impact", "N/A"),
        "",
        "⚠️ 关键风险点",
    ]
    for r in analysis.get("key_risks", []):
        lines.append("  🚨 " + r)
    lines += [
        "",
        "🔮 整体展望",
        "  " + analysis.get("overall_outlook", "N/A"),
        "",
        "┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅",
        "📡 参考合约概率：",
    ]
    # 按合约数加权排序，最活跃的组在前
    sorted_groups = sorted(
        [(gk, cfg) for gk, cfg in MACRO_GROUPS.items() if analysis.get("_contracts", {}).get(gk, [])],
        key=lambda x: len(analysis.get("_contracts", {}).get(x[0], [])),
        reverse=True,
    )
    for gk, cfg in sorted_groups:
        contracts = analysis["_contracts"].get(gk, [])
        if contracts:
            label_emoji = {
                "geopolitics_us_china": "🇨🇳🇺🇸",
                "geopolitics_middle_east": "🌍",
                "fed_rates": "🏦",
                "forex": "💱",
                "indices": "📈",
                "trump": "🔴",
            }.get(gk, "📍")
            lines.append(f"  {label_emoji} {cfg['label']}")
            for c in contracts:
                p = c.get("probability")
                emoji = "🔥" if p and p >= 0.80 else "⚡" if p and p >= 0.50 else "👀" if p and p >= 0.20 else "💤"
                ps = f"{p:.1%}" if p is not None else "N/A"
                lines.append(f"    {emoji} {c['contract_name'][:55]} → {ps}")
    lines += [
        "",
        "📊 数据：Polymarket | 🤖 分析：DeepSeek",
    ]
    return "\n".join(lines)


# ─── 飞书发送 ────────────────────────────────────────

FEISHU_TOKEN_CACHE: dict[str, str | None | int] = {}


def _get_feishu_token() -> str | None:
    """Get Feishu tenant token (cached)."""
    import time
    now = time.time()
    if FEISHU_TOKEN_CACHE.get("token") and FEISHU_TOKEN_CACHE.get("expires", 0) > now + 60:
        return FEISHU_TOKEN_CACHE["token"]
    try:
        resp = requests.post(
            f"{FEISHU_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID_VAL, "app_secret": FEISHU_APP_SECRET_VAL},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            expires_in = data.get("expire", 7200)
            FEISHU_TOKEN_CACHE["token"] = token
            FEISHU_TOKEN_CACHE["expires"] = now + expires_in
            return token
    except Exception as e:
        logger.warning("Feishu token error: %s", e)
    return None


def _send_text_to_feishu(text: str) -> bool:
    """Send a text message to the macro forecast Feishu group."""
    token = _get_feishu_token()
    if not token:
        logger.warning("No Feishu token, skip send")
        return False
    try:
        payload = json.dumps({
            "receive_id": MACRO_CHAT_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        })
        resp = requests.post(
            f"{FEISHU_DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info("Feishu macro forecast sent")
            return True
        else:
            logger.warning("Feishu send failed: %s", data.get("msg"))
            return False
    except Exception as e:
        logger.warning("Feishu send error: %s", e)
        return False


# ─── 入口 ──────────────────────────────────────────

def run():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M CST")
    logger.info("╔════════════════════════════════════════╗")
    logger.info(f"║  📡 Polymarket 宏观经济信号分析    ║")
    logger.info(f"║  {ts}                 ║")
    logger.info("╚════════════════════════════════════════╝\n")

    logger.info("抓取合约数据（并发 midpoint 查询）...")
    macro_data = fetch_macro_contracts()
    total = sum(len(v) for v in macro_data.values())
    logger.info(f"✅ 共 {total} 个合约\n")

    for gk, items in macro_data.items():
        if items:
            logger.info(f"  [{MACRO_GROUPS[gk]['label']}]")
            for c in items:
                p = c.get("probability")
                ps = f"{p:.1%}" if p is not None else "N/A"
                logger.info(f"    {c['contract_name'][:55]} → {ps}")

    if total == 0:
        logger.info("\n❌ 无合约数据")
        return None

    logger.info("\n🤖 DeepSeek 分析中...")
    analysis = analyze_macro_impact(macro_data)
    logger.info("\n" + format_report(analysis))

    # Save
    out_dir = STATE_DIR / "macro_forecast"
    out_dir.mkdir(parents=True, exist_ok=True)
    fts = datetime.now().strftime("%Y%m%d_%H%M")
    with open(out_dir / f"analysis_{fts}.json", "w", encoding="utf-8") as f:
        save = {k: v for k, v in analysis.items() if not k.startswith("_")}
        json.dump(save, f, ensure_ascii=False, indent=2)
    with open(out_dir / f"report_{fts}.txt", "w", encoding="utf-8") as f:
        f.write(format_report(analysis))
    logger.info(f"\n📁 已保存到 {out_dir}/ (analysis_{fts}.json, report_{fts}.txt)")

    # 发送到飞书群
    report_text = format_report(analysis)
    # 飞书文本消息限制约 3000 字符，截取前 2800
    feishu_text = report_text[:2800]
    _send_text_to_feishu(feishu_text)

    return analysis


if __name__ == "__main__":
    run()
