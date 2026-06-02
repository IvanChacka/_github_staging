"""
iv_api_scanner.py — 直接调 OpenVlab API 抓隐含波动率异常合约

流程：
1. 调 flow-data API 按成交额降序取前 N 个活跃合约
2. 对每个合约调 option-series-with-underlying 拿分钟线（第8列=IV）
3. 算 IV 变化（最新有IV的行 vs 更早一条有IV的行，或最新两条）
4. 筛选：上升 > 2% / 下降 > 3%
5. 输出结果（含持仓量、成交、主动买卖方向等）

用法：
  python iv_api_scanner.py

依赖：pip install requests
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ── 强制标准输出使用 UTF-8 ──
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 配置 ──
TOP_CONTRACTS = 150           # 取成交额前 N 个合约查 IV
THRESHOLD_UP = 2.0            # 上升阈值 %
THRESHOLD_DOWN = 3.0          # 下降阈值 %
API_BASE = "https://www.openvlab.cn"
TZ = ZoneInfo("Asia/Shanghai")
DATA_FILE = Path(__file__).resolve().parent / "vol_data.json"
REQUEST_DELAY = 0.2            # 请求间隔（秒），礼貌策略

# ── 品种 → series URL 前缀映射 ──
# 通过探测得到：有些用 代码_O，有些直接用合约前缀
PRODUCT_SERIES_PREFIX = {
    "AU": "AU_O", "CU": "CU_O", "AG": "AG_O",
    "SN": "SN_O", "LC": "LC_O", "JD": "JD_O",
    "SS": "SS_O", "AL": "AL_O", "NI": "NI_O",
    "PB": "PB_O", "ZN": "ZN_O", "RU": "RU_O",
    "BR": "BR_O", "BU": "BU_O", "FU": "FU_O",
    "HC": "HC_O", "RB": "RB_O", "WR": "WR_O",
    "SC": "SC_O", "LU": "LU_O", "PG": "PG_O",
    "EG": "EG_O", "EB": "EB_O", "PP": "PP_O",
    "L":  "L_O",  "V":  "V_O",  "TA": "TA_O",
    "MA": "MA_O", "RM": "RM_O", "FG": "FG_O",
    "SM": "SM_O", "SF": "SF_O", "SA": "SA_O",
    "AP": "AP_O", "CJ": "CJ_O", "UR": "UR_O",
    "CF": "CF_O", "PF": "PF_O", "PK": "PK_O",
    "SR": "SR_O", "WH": "WH_O", "OI": "OI_O",
    "RI": "RI_O", "JR": "JR_O", "LR": "LR_O",
    "CY": "CY_O", "ZC": "ZC_O",
    # 特殊品种：不用 _O
    "IM": "MO",   # 中证1000 → MO
    "IH": "HO",   # 上证50 → HO
    "IF": "IO",   # 沪深300 → IO
}

# 合约代码前缀映射（从 contract_code 提取字母前缀 → series prefix）
# 例: MO2607-C-8300 → "MO"
# 例: au2610C840   → "AU"
# 例: HO2607-C-2950 → "HO"
CONTRACT_PREFIX_MAP = {
    "MO": "MO", "HO": "HO", "IO": "IO",
    "au": "AU_O", "ag": "AG_O", "cu": "CU_O",
    "sn": "SN_O", "lc": "LC_O", "jd": "JD_O",
    "ss": "SS_O", "al": "AL_O", "ni": "NI_O",
    "pb": "PB_O", "zn": "ZN_O", "ru": "RU_O",
    "br": "BR_O", "bu": "BU_O", "fu": "FU_O",
    "hc": "HC_O", "rb": "RB_O", "wr": "WR_O",
    "sc": "SC_O", "lu": "LU_O", "pg": "PG_O",
    "eg": "EG_O", "eb": "EB_O",
}


_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
})


# ════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════

def log(msg: str):
    t = datetime.now(TZ).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def extract_contract_prefix(contract_code: str) -> str | None:
    """从 contract_code 提取字母前缀，如 MO2607-C-8300 → MO, au2610C840 → au"""
    m = re.match(r"([A-Za-z]+)", contract_code)
    return m.group(1) if m else None


def build_series_url(contract: dict) -> str | None:
    """
    根据合约信息构建 option-series-with-underlying URL
    ETF 期权（OPT_SHSE_ / OPT_SZSE_）没有 series 接口，返回 None
    """
    instrument = contract.get("instrument", "")
    if not instrument:
        return None
    
    # ETF 期权跳过
    if instrument.startswith("OPT_SHSE_") or instrument.startswith("OPT_SZSE_"):
        return None

    try:
        prefix, month, opt_type, strike = instrument.split(":")
    except ValueError:
        return None

    # 策略1：从 product_und 映射
    und = contract.get("product_und", "")
    series_prefix = PRODUCT_SERIES_PREFIX.get(und)
    if series_prefix:
        return f"{series_prefix}/{month}/{opt_type}/{strike}"

    # 策略2：从 contract_code 前缀映射
    cc = contract.get("contract_code", "")
    cc_prefix = extract_contract_prefix(cc)
    if cc_prefix:
        mapped = CONTRACT_PREFIX_MAP.get(cc_prefix)
        if mapped:
            return f"{mapped}/{month}/{opt_type}/{strike}"

    # 策略3：从 instrument 解析 product code
    parts = prefix.split("_")
    product_code = parts[-1]
    # 试两种格式
    for fmt in [f"{product_code}_O", product_code]:
        url = f"{fmt}/{month}/{opt_type}/{strike}"
        # 不请求验证，返回最优猜测
        # 先用 product_code（无 _O）格式返回，因为大部分股指类用这格式
        return url

    return None


def extract_iv_change(data: dict) -> tuple[float | None, float | None]:
    """
    从 option-series API 返回提取 IV 变化
    返回 (iv_change_pct, iv_current)
    
    option_series = [[时间, 价格, 涨跌幅%, 标的价格, 主动买量, 主动卖量, 中性量, IV], ...]
    第8列是 IV
    
    方法：找最后两条有 IV 的行，计算变化。
    如果只有一条有 IV 的行，返回 None（没法算变化）。
    """
    series = data.get("result", {}).get("option_series", [])
    if not series:
        return None, None

    # 过滤出有 IV 值的行
    iv_rows = [row for row in series if row[7] is not None]
    if len(iv_rows) < 2:
        return None, None

    try:
        newest = float(iv_rows[-1][7])
        older = float(iv_rows[-2][7])
    except (TypeError, ValueError, IndexError):
        return None, None

    if older == 0:
        return None, None

    iv_change = (newest - older) / older * 100
    return iv_change, newest


# ════════════════════════════════════════════
#  核心抓取
# ════════════════════════════════════════════

def fetch_active_contracts(top: int = 150) -> list[dict]:
    """从 flow-data API 获取成交额前 N 的活跃合约"""
    all_contracts = []
    page_size = 50
    total_pages = (top + page_size - 1) // page_size

    for page in range(1, total_pages + 1):
        log(f"拉取排名列表 第{page}页...")
        try:
            resp = _session.post(
                f"{API_BASE}/api/flow-data",
                json={"page": page, "pageSize": page_size},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            contracts = data.get("result", {}).get("data", [])
            all_contracts.extend(contracts)

            if len(contracts) < page_size:
                break
        except Exception as e:
            log(f"拉取排名列表失败: {e}")
            time.sleep(2)
            continue

        time.sleep(0.3)

    return all_contracts[:top]


def fetch_iv_single(contract: dict) -> dict | None:
    """
    查询单个合约的 IV 分钟线
    返回 {iv_change, iv_current, 以及原 flow-data 数据}
    """
    series_url = build_series_url(contract)
    if not series_url:
        return None

    try:
        resp = _session.post(
            f"{API_BASE}/api/option-series-with-underlying/{series_url}",
            json={},
            timeout=10,  # 单个请求超时10秒
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            return None

        iv_change, iv_current = extract_iv_change(data)
        if iv_change is None or iv_current is None:
            return None

        return {
            # 识
            "name": contract["full_name"],
            "product_alias": contract.get("product_alias", ""),
            "contract_code": contract.get("contract_code", ""),
            "instrument": contract.get("instrument", ""),
            # IV 数据
            "iv_current": round(iv_current, 2),
            "iv_change_pct": round(iv_change, 2),
            # 持仓量
            "oi": contract.get("oi", 0),
            "oi_change": contract.get("oiChange", 0),
            "oi_change_pct": round(contract.get("oiChangePct", 0), 2),
            "prev_oi": contract.get("prevOi", 0),
            # 成交数据
            "volume": contract.get("volume", 0),
            "volume_value": contract.get("volume_value", 0),
            "last_price": contract.get("last_trade_price", 0),
            "ctnPct": contract.get("ctnPct", 0),
            "underlying_price": contract.get("underlying_price", 0),
            "otmPct": contract.get("otmPct", 0),
            # 主动买卖方向
            "ask_pct": contract.get("ask_percentage", 0),
            "bid_pct": contract.get("bid_percentage", 0),
            # 合约属性
            "opt_type": contract.get("optType", ""),
            "sector": contract.get("sector", ""),
            "exchange": contract.get("exchange", ""),
            "dte": round(contract.get("dte", 0), 1),
            "strike_price": contract.get("strikePrice", 0),
        }
    except Exception:
        return None


# ════════════════════════════════════════════
#  筛选
# ════════════════════════════════════════════

def scan(iv_results: list[dict]) -> tuple[list[dict], list[dict]]:
    """按阈值筛选"""
    rise = [r for r in iv_results if r["iv_change_pct"] >= THRESHOLD_UP]
    fall = [r for r in iv_results if r["iv_change_pct"] <= -THRESHOLD_DOWN]
    rise.sort(key=lambda x: x["iv_change_pct"], reverse=True)
    fall.sort(key=lambda x: x["iv_change_pct"])
    return rise, fall


# ════════════════════════════════════════════
#  输出
# ════════════════════════════════════════════

def print_results(rise: list[dict], fall: list[dict], elapsed: float, scanned: int):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    print()
    print("=" * 110)
    print(f"  OpenVlab 隐波异常扫描  {now}")
    print(f"  扫描合约: {scanned} 个  |  耗时: {elapsed:.1f}s")
    print("=" * 110)
    print()

    # ── 上升 >> 2% ──
    print(f"  🔥 隐含波动率上升 > {THRESHOLD_UP}%  ({len(rise)} 条)")
    print("  " + "-" * 105)
    if rise:
        hdr = (
            f"  {'品种':10s} {'合约':18s} {'IV':>7s} {'IV变化':>9s} "
            f"{'持仓量':>7s} {'增仓':>7s} {'增仓%':>9s} "
            f"{'成交量':>7s} {'主动买%':>8s} {'方向':>6s} {'到期':>5s} {'涨跌%':>8s}"
        )
        print(hdr)
        print("  " + "-" * 105)
        for r in rise:
            direction = "🥇买多" if r["ask_pct"] > 55 else "🥈卖空" if r["bid_pct"] > 55 else "⚖️中性"
            oi_sign = "+" if r["oi_change"] >= 0 else ""
            ctn = r.get("ctnPct", 0)
            ctn_s = f"{ctn:+.2f}%" if isinstance(ctn, (int, float)) else str(ctn)
            print(
                f"  {r['product_alias']:10s} {r['contract_code']:18s} "
                f"{r['iv_current']:>7.1f} {fmt_pct(r['iv_change_pct']):>9s} "
                f"{r['oi']:>7d} {oi_sign}{r['oi_change']:>+6d} {r['oi_change_pct']:>+8.1f}% "
                f"{r['volume']:>7d} {r['ask_pct']:>7.1f}% {direction:>6s} {r['dte']:>4.0f}d {ctn_s:>8s}"
            )
    else:
        print("  (无)")
    print()

    # ── 下降 >> 3% ──
    print(f"  🍀 隐含波动率下降 > {THRESHOLD_DOWN}%  ({len(fall)} 条)")
    print("  " + "-" * 105)
    if fall:
        hdr = (
            f"  {'品种':10s} {'合约':18s} {'IV':>7s} {'IV变化':>9s} "
            f"{'持仓量':>7s} {'增仓':>7s} {'增仓%':>9s} "
            f"{'成交量':>7s} {'主动买%':>8s} {'方向':>6s} {'到期':>5s} {'涨跌%':>8s}"
        )
        print(hdr)
        print("  " + "-" * 105)
        for r in fall:
            direction = "🥇买多" if r["ask_pct"] > 55 else "🥈卖空" if r["bid_pct"] > 55 else "⚖️中性"
            oi_sign = "+" if r["oi_change"] >= 0 else ""
            ctn = r.get("ctnPct", 0)
            ctn_s = f"{ctn:+.2f}%" if isinstance(ctn, (int, float)) else str(ctn)
            print(
                f"  {r['product_alias']:10s} {r['contract_code']:18s} "
                f"{r['iv_current']:>7.1f} {fmt_pct(r['iv_change_pct']):>9s} "
                f"{r['oi']:>7d} {oi_sign}{r['oi_change']:>+6d} {r['oi_change_pct']:>+8.1f}% "
                f"{r['volume']:>7d} {r['ask_pct']:>7.1f}% {direction:>6s} {r['dte']:>4.0f}d {ctn_s:>8s}"
            )
    else:
        print("  (无)")
    print()
    print("-" * 110)


def save_results(rise: list[dict], fall: list[dict], all_iv: list[dict]):
    now = datetime.now(TZ).isoformat()
    data = {
        "time": now,
        "rise": rise,
        "fall": fall,
        "all_scanned": all_iv,
        "config": {
            "top_contracts": TOP_CONTRACTS,
            "threshold_up": THRESHOLD_UP,
            "threshold_down": THRESHOLD_DOWN,
        },
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"结果已保存: {DATA_FILE}")


# ════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════

def run():
    log(f"启动扫描... 取前{TOP_CONTRACTS}个活跃合约查 IV")
    t0 = time.time()

    # Step 1: 拉活跃合约列表
    contracts = fetch_active_contracts(TOP_CONTRACTS)
    log(f"获取到 {len(contracts)} 个活跃合约")

    if not contracts:
        log("没有获取到合约数据，检查 API")
        return

    # Step 2: 逐个查 IV
    iv_results = []
    total = len(contracts)
    for i, c in enumerate(contracts):
        result = fetch_iv_single(c)
        if result:
            iv_results.append(result)
            # 每5个或 IV 变化 >= 1% 时打印
            if (i+1) % 5 == 0 or abs(result["iv_change_pct"]) >= 1.5:
                print(f"  [{i+1}/{total}] {result['contract_code']:18s} "
                    f"IV={result['iv_current']:.1f} 变化={result['iv_change_pct']:+.2f}%  "
                    f"OI={result['oi']} 增仓={result['oi_change']:+d}", flush=True)
        time.sleep(REQUEST_DELAY)

    elapsed = time.time() - t0
    log(f"IV 扫描完成: {len(iv_results)}/{total} 个成功 | 耗时 {elapsed:.1f}s")

    # Step 3: 筛选
    rise, fall = scan(iv_results)
    log(f"筛选结果: 上升>{THRESHOLD_UP}% = {len(rise)} 条, 下降>{THRESHOLD_DOWN}% = {len(fall)} 条")

    # Step 4: 输出
    print_results(rise, fall, elapsed, len(iv_results))
    save_results(rise, fall, iv_results)


if __name__ == "__main__":
    run()
