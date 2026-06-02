"""
OpenVlab market fetch + parse helpers shared by crawler and guard scripts.
"""
from __future__ import annotations

import re
import sys
import json
from typing import Callable

try:
    from playwright.async_api import async_playwright
except ImportError as exc:  # pragma: no cover - dependency guard
    async_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None


MARKET_URL = "https://www.openvlab.cn/market"
READY_MARKERS = ("隐波最大上升", "隐波最大下降")
HEADER_TOKENS = {
    "名称",
    "品种",
    "标的",
    "分时预览",
    "实时预测",
    "隐含波动率",
    "隐波变化",
    "涨幅%",
    "涨跌幅%",
}
NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%?$")
PERCENT_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")
SNAPSHOT_SCRIPT = """
() => {
  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const isVisible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      rect.width > 0 &&
      rect.height > 0
    );
  };

  const extractRows = (card) => {
    const rows = [];
    const rowNodes = card.querySelectorAll(".group.relative.cursor-pointer");

    rowNodes.forEach((row) => {
      if (!isVisible(row)) return;

      const cells = row.querySelectorAll(".grid.grid-cols-12.gap-4.items-center > div");
      if (cells.length < 3) return;

      const link = row.querySelector('a[href*="/chart/light/"]');
      const nameNode = link ? link.querySelector('span.truncate') : null;
      const name = normalize(nameNode ? nameNode.innerText : cells[0].innerText);
      const pctChange = normalize(cells[1].innerText);
      const volChange = normalize(cells[2].innerText);

      // Extract contract slug from href (e.g. "/chart/light/jm2607" -> "jm2607")
      let contractSlug = "";
      if (link) {
        const href = link.getAttribute("href") || "";
        const parts = href.split("/");
        contractSlug = parts[parts.length - 1] || "";
      }

      if (!name || !pctChange || !volChange) return;
      rows.push({
        name,
        pct_change: pctChange,
        vol_change: volChange,
        contract_slug: contractSlug,
      });
    });

    return rows.slice(0, 5);
  };

  const result = { rise: [], fall: [] };
  const cards = document.querySelectorAll(
    'div.rounded-2xl.border.p-2, div[class*="rounded-2xl"][class*="border"][class*="p-2"]'
  );

  cards.forEach((card) => {
    if (!isVisible(card)) return;

    const titleNode = card.querySelector(".text-xl.font-medium span");
    const title = normalize(titleNode ? titleNode.innerText : "");
    if (!title.includes("隐波最大")) return;

    if (title.includes("上升") && result.rise.length === 0) {
      result.rise = extractRows(card);
    } else if (title.includes("下降") && result.fall.length === 0) {
      result.fall = extractRows(card);
    }
  });

  return result;
}
"""


def configure_stdio() -> None:
    """Keep redirected logs readable on Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            continue


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _is_percent_token(value: str) -> bool:
    return bool(PERCENT_RE.match(value.strip()))


def _is_number_token(value: str) -> bool:
    return bool(NUMBER_RE.match(value.strip()))


def _looks_like_name(value: str) -> bool:
    cleaned = value.strip().replace(" ", "")
    if not cleaned or cleaned in HEADER_TOKENS:
        return False
    if any(marker in cleaned for marker in READY_MARKERS):
        return False
    if _is_percent_token(cleaned) or _is_number_token(cleaned):
        return False
    return any("\u4e00" <= ch <= "\u9fff" or ch.isalpha() for ch in cleaned)


def _is_market_row(name: str, pct_change: str, vol_change: str) -> bool:
    return (
        _looks_like_name(name)
        and _is_percent_token(pct_change)
        and _is_number_token(vol_change)
    )


def _parse_section(items: list[str], limit: int = 5) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    index = 0

    while index + 2 < len(items) and len(rows) < limit:
        name, pct_change, vol_change = items[index:index + 3]
        if _is_market_row(name, pct_change, vol_change):
            rows.append(
                {
                    "name": name,
                    "pct_change": pct_change,
                    "vol_change": vol_change,
                }
            )
            index += 3
            continue
        index += 1

    return rows


def parse_text_data(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Extract rise/fall rows from the market page body text."""
    lines = _clean_lines(text)
    rise_idx = -1
    fall_idx = -1

    for index, line in enumerate(lines):
        if "隐波最大上升" in line:
            rise_idx = index
        elif "隐波最大下降" in line:
            fall_idx = index

    if rise_idx < 0 or fall_idx <= rise_idx:
        return [], []

    rise_items = lines[rise_idx + 1:fall_idx]
    fall_items = lines[fall_idx + 1:min(fall_idx + 40, len(lines))]
    return _parse_section(rise_items), _parse_section(fall_items)


async def fetch_market_text(
    progress: Callable[[str, int], None] | None = None,
    wait_cycles: int = 10,
    wait_ms: int = 3000,
) -> str:
    """Fetch the market page body text with bounded retries."""
    if PLAYWRIGHT_IMPORT_ERROR is not None or async_playwright is None:
        raise RuntimeError(
            "请先安装: pip install playwright && python -m playwright install chromium"
        ) from PLAYWRIGHT_IMPORT_ERROR

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        page.set_default_timeout(30000)

        await page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=30000)

        text = ""
        for _ in range(wait_cycles):
            await page.wait_for_timeout(wait_ms)
            try:
                text = await page.inner_text("body")
            except Exception:
                text = ""
            if progress is not None:
                progress("fetching", len(text))
            if len(text) > 500 and all(marker in text for marker in READY_MARKERS):
                break

        await browser.close()
        return text


async def fetch_market_snapshot(
    progress: Callable[[str, int], None] | None = None,
    wait_cycles: int = 10,
    wait_ms: int = 3000,
) -> dict[str, list[dict[str, str]] | str]:
    """Fetch the visible rise/fall cards directly from the page DOM."""
    if PLAYWRIGHT_IMPORT_ERROR is not None or async_playwright is None:
        raise RuntimeError(
            "请先安装: pip install playwright && python -m playwright install chromium"
        ) from PLAYWRIGHT_IMPORT_ERROR

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        page.set_default_timeout(30000)

        await page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=30000)

        snapshot: dict[str, list[dict[str, str]] | str] = {
            "rise": [],
            "fall": [],
            "text": "",
        }
        last_non_empty = snapshot.copy()
        last_signature = ""
        stable_rounds = 0

        for _ in range(wait_cycles):
            await page.wait_for_timeout(wait_ms)
            try:
                snapshot = await page.evaluate(SNAPSHOT_SCRIPT)
            except Exception:
                snapshot = {"rise": [], "fall": []}

            try:
                text = await page.inner_text("body")
            except Exception:
                text = ""

            snapshot["text"] = text
            if progress is not None:
                progress("fetching", len(text))

            if snapshot["rise"] and snapshot["fall"]:
                last_non_empty = {
                    "rise": snapshot["rise"],
                    "fall": snapshot["fall"],
                    "text": text,
                }
                signature = json.dumps(
                    {
                        "rise": snapshot["rise"],
                        "fall": snapshot["fall"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if signature == last_signature:
                    stable_rounds += 1
                else:
                    last_signature = signature
                    stable_rounds = 1

                if stable_rounds >= 2:
                    break

        if last_non_empty["rise"] and last_non_empty["fall"]:
            snapshot = last_non_empty

        await browser.close()
        return snapshot
