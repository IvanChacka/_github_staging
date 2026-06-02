#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
hourly_report.py — 中和动力 v10
中金研究风格，DeepSeek LLM 分析，SVG 封面，独立免责声明
"""
import base64, io, json, logging, os, sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

from config.settings import BASE_DIR, STATE_DIR
from processor.llm_analyzer import generate_all_analyses

# ── Beijing time helper ──
from datetime import timezone as dt_timezone, timedelta as dt_timedelta
_CST = dt_timezone(dt_timedelta(hours=8))

def now_cn() -> datetime:
    """Current Beijing time (CST, UTC+8)"""
    return datetime.now(_CST)

logger = logging.getLogger("hourly_report")
_h = logging.FileHandler(str(BASE_DIR / "logs" / "hourly_report.log"), encoding="utf-8", mode="a")
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(_h)
logger.propagate = False

# ── Constants ──
FS = "https://open.feishu.cn"
APP_ID = "cli_aa8027366778dcba"
APP_SEC = "iqviiidEzHwSVOuQ0OAKeck7PB8bfQfp"
CHAT_ID = "oc_40593d5279bcbd4494869d0333775321"
DB = STATE_DIR / "prob_monitor.db"
OUT = STATE_DIR / "hourly_reports"
OUT.mkdir(parents=True, exist_ok=True)

ML = {"gold": "黄金", "oil": "原油", "silver": "白银", "natgas": "天然气"}
ML_ORDER = ["gold", "oil", "silver", "natgas"]
ML_COLORS = {"gold": "#C9A34A", "oil": "#e74c3c", "silver": "#95a5a6", "natgas": "#3498db"}
ML_ICONS = {"gold": "🥇", "oil": "🛢", "silver": "🥈", "natgas": "🔥"}
ZH_FONTS = ["Microsoft YaHei", "SimHei", "Noto Sans SC", "DejaVu Sans"]

BRAND = ""
TITLE = "Polymarket 异常合约分析日报"
DBLUE = "#003366"
MBLUE = "#0055A4"
GOLD = "#C9A34A"
BLACK = "#333333"
WHITE = "#FFFFFF"
LGRAY = "#F5F6F8"


def _mpl_font():
    a = {f.name for f in fm.fontManager.ttflist}
    for fn in ZH_FONTS:
        if fn in a:
            return fn
    return "sans-serif"


# ── Feishu API (retry-safe, requests-based) ──
def _get_token():
    """Get tenant access token for Feishu API (retries ×3, exponential backoff)."""
    import requests, time
    for attempt in range(3):
        try:
            r = requests.post(
                f"{FS}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": APP_ID, "app_secret": APP_SEC},
                timeout=30,
            )
            return r.json()["tenant_access_token"]
        except Exception as e:
            logger.warning("Get token attempt %d/3 failed: %s", attempt + 1, e)
            if attempt == 2:
                logger.error("Get token failed after 3 retries: %s", e)
                return None
            time.sleep(2 ** attempt)


def _upload_file(token, filepath, filename):
    """
    Upload file to Feishu API. Retries ×3 with exponential backoff (1s→3s→9s).
    """
    import requests, time
    with open(filepath, "rb") as f:
        file_data = f.read()
    for attempt in range(3):
        try:
            r = requests.post(
                f"{FS}/open-apis/im/v1/files",
                files={"file": (filename, file_data, "application/pdf")},
                data={"file_type": "stream", "file_name": filename},
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,
            )
            return r.json()
        except Exception as e:
            backoff = 3 ** attempt
            logger.warning("Upload attempt %d/3 failed (retry in %ds): %s", attempt + 1, backoff, e)
            if attempt == 2:
                raise
            time.sleep(backoff)
    return {"code": -1, "msg": "unreachable"}


def _send_file(token, file_key):
    """Send uploaded file as message to target chat. Retries ×3 with exponential backoff."""
    import requests, time
    body = {"receive_id": CHAT_ID, "msg_type": "file", "content": json.dumps({"file_key": file_key})}
    for attempt in range(3):
        try:
            r = requests.post(
                f"{FS}/open-apis/im/v1/messages?receive_id_type=chat_id",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            return r.json().get("code") == 0
        except Exception as e:
            backoff = 3 ** attempt
            logger.warning("Send attempt %d/3 failed (retry in %ds): %s", attempt + 1, backoff, e)
            if attempt == 2:
                raise
            time.sleep(backoff)
    return False


# ── Data Loading ──
def _load_data(mod_key):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT m.contract_slug,m.contract_name,m.market_name,
                  m.probability,m.probability_change,m.volume,
                  COALESCE(cc.direction,'') AS direction
           FROM minute_metrics m
           LEFT JOIN contract_classification cc
             ON cc.contract_slug=m.contract_slug AND cc.group_key=m.group_key AND cc.relevant=1
           WHERE m.group_key=? AND m.ts_et=(SELECT MAX(ts_et) FROM minute_metrics WHERE group_key=?)
             AND m.probability_anomaly=1
           ORDER BY ABS(m.probability_change) DESC""",
        (mod_key, mod_key),
    ).fetchall()
    items = [
        {
            "name": r["contract_name"] or r["contract_slug"],
            "slug": r["contract_slug"],
            "prob": r["probability"] or 0,
            "prob_chg": r["probability_change"] or 0,
            "volume": r["volume"] or 0,
            "direction": r["direction"] or "",
        }
        for r in rows
    ]
    dirs = conn.execute(
        """SELECT direction,COUNT(*) AS c FROM contract_classification
           WHERE group_key=? AND relevant=1 AND direction!='' GROUP BY direction""",
        (mod_key,),
    ).fetchall()
    b = be = n = 0
    for d in dirs:
        if "看涨" in d["direction"]:
            b += d["c"]
        elif "看跌" in d["direction"]:
            be += d["c"]
        else:
            n += d["c"]
    conn.close()
    return items, b, be, n


def _load_chart_data():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    latest = conn.execute("SELECT MAX(ts_et) FROM minute_metrics").fetchone()[0]
    if not latest:
        conn.close()
        return {}
    try:
        ldt = datetime.fromisoformat(latest)
    except Exception:
        ldt = datetime.now()
    ws = (ldt - timedelta(hours=4)).isoformat()
    cd = {}
    for mk in ML_ORDER:
        tops = conn.execute(
            """SELECT contract_slug,contract_name,probability_change
               FROM minute_metrics
               WHERE group_key=? AND ts_et=(SELECT MAX(ts_et) FROM minute_metrics WHERE group_key=?)
               ORDER BY ABS(probability_change) DESC LIMIT 3""",
            (mk, mk),
        ).fetchall()
        series = {}
        for t in tops:
            pts = conn.execute(
                "SELECT ts_et,probability FROM minute_metrics WHERE group_key=? AND contract_slug=? AND ts_et>=? ORDER BY ts_et",
                (mk, t["contract_slug"], ws),
            ).fetchall()
            parsed = []
            for p in pts:
                try:
                    parsed.append((datetime.fromisoformat(p["ts_et"]), p["probability"]))
                except Exception:
                    pass
            if len(parsed) >= 3:
                lbl = (t["contract_name"] or t["contract_slug"])[:45]
                series[lbl] = parsed
        if series:
            cd[mk] = series
    conn.close()
    return cd


def _gen_trend_chart(cd):
    if not cd:
        return ""
    fn = _mpl_font()
    plt.rcParams["font.family"] = fn
    plt.rcParams["axes.unicode_minus"] = False
    mkp = [mk for mk in ML_ORDER if mk in cd]
    if not mkp:
        return ""
    fig, axes = plt.subplots(len(mkp), 1, figsize=(14, 3.5 * len(mkp)))
    if len(mkp) == 1:
        axes = [axes]
    fig.patch.set_facecolor(WHITE)
    for idx, mk in enumerate(mkp):
        ax = axes[idx]
        ax.set_facecolor(WHITE)
        for sp in ax.spines.values():
            sp.set_color("#CCCCCC")
            sp.set_linewidth(0.8)
        ax.tick_params(colors="#666666", labelsize=9)
        ax.grid(True, alpha=0.3, color="#DDDDDD")
        ax.set_title(f"{ML_ICONS[mk]} {ML[mk]} 概率走势", color=DBLUE, fontsize=13, pad=10, fontweight="bold")
        series = cd[mk]
        colors = plt.cm.Set2(np.linspace(0, 1, max(len(series), 3)))
        for i, (label, pts) in enumerate(series.items()):
            ts, ps = zip(*pts)
            col = colors[i % len(colors)]
            ax.plot(ts, ps, color=col, lw=1.8, alpha=0.85, label=label)
            ax.scatter(ts[-1], ps[-1], color=col, s=30, zorder=5, edgecolors=DBLUE, linewidth=0.5)
        ax.set_ylabel("概率", color="#666666", fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", color="#666666")
        ax.legend(loc="upper left", framealpha=0.9, facecolor=WHITE, edgecolor="#CCCCCC", labelcolor=BLACK, fontsize=7)
    fig.tight_layout(pad=2.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=WHITE, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _sig(chg, direction):
    if "看跌" in direction:
        return "看涨" if chg < 0 else "看跌"
    elif "看涨" in direction:
        return "看涨" if chg > 0 else "看跌"
    return "看涨" if chg > 0 else "看跌"


# ── SVG Cover ──
def _cover_svg(dt_cn):
    ds = dt_cn.strftime("%Y年%m月%d日")
    tstr = dt_cn.strftime("%H:%M CST")
    rid = f"ZHDL-{dt_cn.strftime('%Y%m%d%H%M')}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 595 842" width="595" height="842">
<defs><linearGradient id="bgG" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#F0F2F5"/></linearGradient>
<linearGradient id="gG" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="{GOLD}" stop-opacity=".3"/>
<stop offset=".5" stop-color="{GOLD}" stop-opacity="1"/>
<stop offset="1" stop-color="{GOLD}" stop-opacity=".3"/></linearGradient></defs>
<rect width="595" height="842" fill="url(#bgG)"/>
<rect x="0" y="0" width="595" height="6" fill="{DBLUE}"/>
<line x1="80" y1="220" x2="515" y2="220" stroke="url(#gG)" stroke-width="1.5"/>
<line x1="80" y1="228" x2="515" y2="228" stroke="url(#gG)" stroke-width=".5"/>
<text x="298" y="300" text-anchor="middle" font-size="60">📊</text>
<text x="298" y="390" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="48" font-weight="900" fill="{DBLUE}" letter-spacing="8">{BRAND}</text>
<text x="298" y="440" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="20" fill="{MBLUE}" letter-spacing="4">{TITLE}</text>
<line x1="160" y1="470" x2="435" y2="470" stroke="{GOLD}" stroke-width="2"/>
<line x1="180" y1="478" x2="415" y2="478" stroke="{GOLD}" stroke-width="1"/>
<text x="298" y="530" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="16" fill="#666">报告日期：{ds}</text>
<text x="298" y="560" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="14" fill="#888">数据截至 {tstr}</text>
<text x="298" y="600" text-anchor="middle" font-family="'Consolas','Courier New',monospace" font-size="11" fill="#AAA">报告编号：{rid}</text>
<text x="298" y="640" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="12" fill="#999">数据来源：Polymarket | 分析方法：统计异常检测 + LLM 深度分析</text>
<line x1="80" y1="780" x2="515" y2="780" stroke="url(#gG)" stroke-width="1"/>
<text x="298" y="808" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="12" fill="{DBLUE}" letter-spacing="3">预测市场分析报告</text>
</svg>"""


def _disclaimer_html():
    return """<div class="d-page" style="page-break-before:always;">
<div class="d-inner"><div class="d-icon">©</div>
<h2 class="d-title">免责声明</h2>
<div class="d-line"></div>
<p class="d-text">本报告由自动化系统生成，仅供参考，不构成任何投资建议或投资承诺。</p>
<p class="d-text">报告中的数据和信息来源于公开市场平台（Polymarket），系统对数据的准确性、完整性或时效性不作任何明示或暗示的保证。用户在使用本报告时应当自行判断并承担相关风险。</p>
<p class="d-text">任何根据本报告内容做出的投资决策、交易行为或由此产生的任何损失，本系统不承担任何法律责任。</p>
<p class="d-text">本报告由自动化系统生成，未经授权，任何机构或个人不得以任何形式转载、引用、复制或散布本报告的全部或部分内容。</p>
<div class="d-line"></div>
<p class="d-copy">© 2026 预测市场分析报告。保留所有权利。</p>
<p class="d-ver">版本：v10 | 系统自动生成</p>
</div></div>"""


def _gen_html(dt_cn):
    all_data = {}
    for mk in ML_ORDER:
        items, bull, bear, neut = _load_data(mk)
        all_data[mk] = {"items": items, "bull": bull, "bear": bear, "neut": neut}

    cd = _load_chart_data()
    cb64 = _gen_trend_chart(cd)
    chart_html = ""
    if cb64:
        chart_html = f"""<div class="section chart-section" style="page-break-inside:avoid;">
<h2 class="stitle">📈 概率走势图</h2>
<p class="ssub">过去 4 小时各模块 Top-3 异常合约概率变化</p>
<img src="data:image/png;base64,{cb64}" style="width:100%;height:auto;border-radius:6px;border:1px solid #E0E0E0;"></div>"""

    ts_str = dt_cn.strftime("%Y-%m-%d %H:%M CST")
    cover_svg = _cover_svg(dt_cn)

    # ── Overview table ──
    ovrows = ""
    ta = tbull = tbear = 0
    for mk in ML_ORDER:
        d = all_data[mk]
        n = len(d["items"])
        bu = d["bull"]
        be = d["bear"]
        ta += n
        tbull += bu
        tbear += be
        ar, dt = "—", "中性"
        if bu > be:
            ar, dt = "📈", "看涨"
        elif be > bu:
            ar, dt = "📉", "看跌"
        ovrows += f"""<tr><td style="font-weight:600;">{ML_ICONS[mk]} {ML[mk]}</td>
<td class="num hl">{n}</td><td class="num bull">{bu}</td><td class="num bear">{be}</td><td class="num">{ar} {dt}</td></tr>"""

    # ── Unified LLM Analysis (one call for all 4 modules) ──
    llm_results: dict[str, dict] = {}
    modules_with_items = {mk: [{
        "name": c["name"],
        "contract_name": c.get("contract_name", c["name"]),
        "probability_change": c.get("prob_chg", 0),
        "prob_chg": c.get("prob_chg", 0),
        "direction": c.get("direction", ""),
    } for c in all_data[mk]["items"]] for mk in ML_ORDER if all_data[mk]["items"]}
    if modules_with_items:
        try:
            llm_results = generate_all_analyses(modules_with_items)
        except Exception as e:
            logger.warning("Unified LLM analysis failed: %s", e)

    # ── Module sections ──
    mods = []
    for mk in ML_ORDER:
        d = all_data[mk]
        items = d["items"]
        lb = ML[mk]
        ic = ML_ICONS[mk]
        co = ML_COLORS[mk]
        n = len(items)
        bu = d["bull"]
        be = d["bear"]
        ar, dt = "—", "中性"
        if bu > be:
            ar, dt = "📈", "看涨"
        elif be > bu:
            ar, dt = "📉", "看跌"
        dc = "bullish" if "涨" in dt else ("bearish" if "跌" in dt else "neutral")

        cro = []
        for item in items:
            pct = item["prob"] * 100
            chg = item["prob_chg"] * 100
            cc = "up" if chg > 0 else ("down" if chg < 0 else "flat")
            dl = item["direction"] if item["direction"] else "中性"
            dc2 = "bull" if "看涨" in dl else ("bear" if "看跌" in dl else "neut")
            s = _sig(item["prob_chg"], item["direction"])
            sc = "bull" if "看涨" in s else "bear"
            sa = "📈" if "看涨" in s else "📉"
            cro.append(f"""<tr>
<td class="cn">{item["name"][:60]}</td>
<td class="num">{pct:.1f}%</td>
<td class="num {cc}">{chg:+.2f}%</td>
<td class="num">{item["volume"]:,.0f}</td>
<td><span class="db db-{dc2}">{dl}</span></td>
<td><span class="db db-{sc}">{sa} {s}</span></td></tr>""")
        if not cro:
            cro.append('<tr><td colspan="6" class="empty">暂无异常合约</td></tr>')

        # ── LLM Analysis (from unified result) ──
        lnews = '<div class="lp"><p class="pt">🔗 LLM 新闻分析暂缺</p></div>'
        lana = '<div class="lp"><p class="pt">📝 LLM 综合分析暂缺</p></div>'
        lsug = '<div class="lp"><p class="pt">🎯 LLM 操作建议暂缺</p></div>'
        # Default entry links for modules without anomaly / LLM result
        _default_links = {
            "gold": [("路孚特 — 贵金属", "https://www.reuters.com/markets/commodities/metals/"), ("金十数据 — 黄金", "https://www.jin10.com/")],
            "oil": [("路孚特 — 能源", "https://www.reuters.com/business/energy/"), ("金十数据 — 原油", "https://www.jin10.com/")],
            "silver": [("路孚特 — 贵金属", "https://www.reuters.com/markets/commodities/metals/"), ("金十数据 — 白银", "https://www.jin10.com/")],
            "natgas": [("路孚特 — 能源", "https://www.reuters.com/business/energy/"), ("金十数据 — 天然气", "https://www.jin10.com/")],
        }
        if mk in llm_results:
            lr = llm_results[mk]
            ni = lr.get("news_links", [])
            if ni:
                nl = "".join(f'<li><a href="{n["url"]}" target="_blank">{n["title"]}</a></li>' for n in ni if n.get("url"))
                if nl:
                    lnews = f'<ul class="llist">{nl}</ul>'
            if lr.get("analysis"):
                lana = f'<div class="lt">{lr["analysis"]}</div>'
            if lr.get("suggestion"):
                lsug = f'<div class="lt st">💡 {lr["suggestion"]}</div>'
        else:
            # No LLM result — show default entry links
            dl = _default_links.get(mk, [])
            if dl:
                nl = "".join(f'<li><a href="{u}" target="_blank">{t}</a></li>' for t, u in dl)
                lnews = f'<ul class="llist">{nl}</ul></div>'
            lana = '<div class="lt">当前时段该模块未检测到明显异常信号，市场运行平稳。</div>'
            lsug = '<div class="lt st">建议保持关注，等待明确信号再行操作。</div>'

        mods.append(f"""<div class="mc" style="border-left-color:{co};page-break-inside:avoid;">
<div class="mh">
<div class="mt"><span class="mi">{ic}</span><span class="mn">{lb}市场</span><span class="md {dc}">{ar} {dt}</span></div>
<div class="ms"><span class="s sb">📈 {bu}</span><span class="s sr">📉 {be}</span><span class="s sa">⚡ {n}个</span></div></div>
<div class="mb">
<h3 class="st2">📊 异常合约明细</h3>
<table class="ct"><thead><tr><th>合约名称</th><th style="text-align:right;">概率</th>
<th style="text-align:right;">概率变化</th><th style="text-align:right;">成交量</th><th>原始方向</th><th>修正信号</th></tr></thead>
<tbody>{"".join(cro)}</tbody></table>
<h3 class="st2">📰 相关新闻</h3>{lnews}
<h3 class="st2">💡 综合分析</h3>{lana}
<h3 class="st2">🎯 操作建议</h3>{lsug}
</div></div>""")

    dh = _disclaimer_html()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{BRAND} | {TITLE} - {ts_str}</title>
<style>
@page {{ size:A4 portrait; margin:0; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Microsoft YaHei','PingFang SC','Segoe UI',Arial,sans-serif;
  background:{WHITE}; color:{BLACK}; font-size:14px; line-height:1.7; }}
.cover-page {{ width:100%; height:297mm; display:flex; flex-direction:column;
  justify-content:center; align-items:center; }}
.content {{ padding:18mm 16mm; }}
.stitle {{ font-size:22px; color:{DBLUE}; font-weight:700; padding:0 0 8px 0; margin:0 0 12px 0;
  border-bottom:2px solid {DBLUE}; }}
.ssub {{ font-size:13px; color:#888; margin-bottom:14px; }}
.st2 {{ font-size:16px; color:{MBLUE}; font-weight:600; margin:18px 0 8px 0;
  padding-bottom:4px; border-bottom:1px solid #E0E0E0; }}
.ot {{ width:100%; border-collapse:collapse; border-radius:8px; overflow:hidden;
  box-shadow:0 1px 4px rgba(0,0,0,.08); margin:0 0 20px 0; }}
.ot thead th {{ padding:10px 14px; text-align:center; font-size:13px;
  background:{DBLUE}; color:{WHITE}; font-weight:600; letter-spacing:1px; }}
.ot tbody td {{ padding:10px 14px; text-align:center; font-size:14px; border-bottom:1px solid #E8E8E8; }}
.ot tbody tr:nth-child(even) {{ background:{LGRAY}; }}
.num {{ font-family:'Consolas','Courier New',monospace; }}
.hl {{ color:{DBLUE}; font-weight:700; font-size:16px; }}
.bull {{ color:#D4380D; }} .bear {{ color:#096DD9; }}
.mc {{ background:{WHITE}; border-radius:8px; margin:0 0 20px 0;
  border-left:4px solid {GOLD}; box-shadow:0 1px 6px rgba(0,0,0,.08); overflow:hidden; }}
.mh {{ display:flex; justify-content:space-between; align-items:center; padding:12px 18px;
  background:linear-gradient(135deg,{LGRAY},{WHITE}); border-bottom:1px solid #E8E8E8; }}
.mt {{ display:flex; align-items:center; gap:10px; }}
.mi {{ font-size:22px; }} .mn {{ font-size:18px; font-weight:700; color:{DBLUE}; }}
.md {{ font-size:13px; padding:3px 10px; border-radius:4px; font-weight:600; }}
.md.bullish {{ background:#FFF1F0; color:#D4380D; }} .md.bearish {{ background:#E6F7FF; color:#096DD9; }}
.md.neutral {{ background:#F5F5F5; color:#888; }}
.ms {{ display:flex; gap:10px; font-size:12px; }}
.s {{ padding:3px 10px; border-radius:4px; background:{WHITE}; font-weight:600; border:1px solid #E0E0E0; }}
.sb {{ color:#D4380D; border-color:#FFCCC7; }} .sr {{ color:#096DD9; border-color:#91D5FF; }}
.sa {{ color:#D46B08; border-color:#FFD591; }}
.mb {{ padding:14px 18px; }}
.ct {{ width:100%; border-collapse:collapse; font-size:13px; margin:4px 0 0 0; }}
.ct thead th {{ padding:8px 12px; text-align:left; font-size:12px;
  color:{WHITE}; background:{MBLUE}; font-weight:600; border-bottom:2px solid {DBLUE}; }}
.ct tbody td {{ padding:7px 12px; border-bottom:1px solid #ECECEC; vertical-align:middle; }}
.ct tbody tr:nth-child(even) {{ background:#FAFBFC; }}
.up {{ color:#D4380D; font-weight:600; }} .down {{ color:#096DD9; font-weight:600; }} .flat {{ color:#888; }}
.cn {{ max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.db {{ display:inline-block; padding:2px 8px; border-radius:3px; font-size:12px; font-weight:600; }}
.db-bull {{ background:#FFF1F0; color:#D4380D; }} .db-bear {{ background:#E6F7FF; color:#096DD9; }}
.db-neut {{ background:#F5F5F5; color:#888; }}
.empty {{ text-align:center; color:#999; padding:20px; }}
.llist {{ list-style:none; padding:0; }} .llist li {{ padding:6px 0; }}
.llist li a {{ color:{MBLUE}; text-decoration:none; font-size:14px; }}
.llist li a:hover {{ text-decoration:underline; }}
.lp {{ padding:8px 0; }} .pt {{ color:#999; font-style:italic; font-size:13px; }}
.lt {{ padding:8px 12px; background:{LGRAY}; border-radius:6px; font-size:14px; line-height:1.8; }}
.st {{ border-left:3px solid {GOLD}; }}
.d-page {{ width:100%; height:297mm; display:flex; align-items:center; justify-content:center; background:{LGRAY}; }}
.d-inner {{ max-width:460px; text-align:center; padding:40px; background:{WHITE}; border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.08); }}
.d-icon {{ font-size:48px; color:{GOLD}; margin-bottom:10px; }}
.d-title {{ font-size:24px; color:{DBLUE}; font-weight:700; margin-bottom:12px; }}
.d-line {{ width:60px; height:2px; background:{GOLD}; margin:12px auto; }}
.d-text {{ font-size:13px; color:#666; line-height:2; margin-bottom:10px; text-align:left; }}
.d-copy {{ font-size:14px; color:{DBLUE}; font-weight:600; margin-top:10px; }}
.d-ver {{ font-size:11px; color:#999; margin-top:6px; }}
@media print {{ .cover-page{{page-break-after:always;}} .ct td,.ct th{{page-break-inside:avoid;}} .mc{{page-break-inside:avoid;}} }}
</style></head><body>
<div class="cover-page">{cover_svg}</div>
<div class="content">
<h2 class="stitle">📋 概览</h2>
<table class="ot"><thead><tr><th>模块</th><th>异常信号</th><th>看涨</th><th>看跌</th><th>方向</th></tr></thead>
<tbody>{ovrows}</tbody></table>
<h2 class="stitle">📋 模块详情</h2>
{"".join(mods)}
{chart_html}
{dh}
</div></body></html>"""
    return html


def run():
    try:
        cn_now = now_cn()
        html = _gen_html(cn_now)
        ts = cn_now.strftime("%Y%m%d_%H%M")
        html_path = OUT / f"{ts}_report.html"
        pdf_path = OUT / f"{ts}_report.pdf"

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("HTML: %s", html_path)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"],
                                         executable_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
            page = browser.new_page(viewport={"width": 1200, "height": 1600})
            page.goto(html_path.absolute().as_uri(), wait_until="networkidle")
            page.emulate_media(media="print")
            page.pdf(path=str(pdf_path), format="A4", landscape=False,
                     margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            browser.close()
        logger.info("PDF: %s (%.1f KB)", pdf_path, pdf_path.stat().st_size / 1024)

        token = _get_token()
        if token:
            try:
                ur = _upload_file(token, str(pdf_path), pdf_path.name)
                fk = ur.get("data", {}).get("file_key")
                if fk:
                    ok = _send_file(token, fk)
                    logger.info("Feishu sent: %s", ok)
                else:
                    logger.warning("Upload failed: %s", ur.get("msg", "unknown"))
            except Exception as fe:
                logger.warning("Feishu send failed (PDF saved locally): %s", fe)
        else:
            logger.warning("No token, saved locally: %s", pdf_path)
        logger.info("Report done: %s", ts)
    except Exception as e:
        logger.error("Report failed: %s", e, exc_info=True)


if __name__ == "__main__":
    run()
