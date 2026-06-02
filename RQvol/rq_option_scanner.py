"""
RQData 期权 IV 分钟级监控 + 企业微信推送
"""
import json, os, sys, time as stime, requests, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import rqdatac as rq

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ = ZoneInfo("Asia/Shanghai")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "rq_option_data.json")
LICENSE_KEY = "YOUR_RQDATAC_LICENSE_KEY"
WECOM_KEY = "YOUR_WECOM_WEBHOOK_KEY"
POLL_INTERVAL = 60
THRESHOLD_UP = 2.0
THRESHOLD_DOWN = 3.0

TRADING_SESSIONS = [
    (9, 30, 11, 30),
    (13, 0, 15, 0),
    (21, 0, 23, 59),
    (0, 0, 2, 30),
]


def _ts():
    return datetime.now(TZ).strftime("%m-%d %H:%M")


def is_trading_time():
    now = datetime.now(TZ)
    now_min = now.hour * 60 + now.minute
    for sh, sm, eh, em in TRADING_SESSIONS:
        if (sh * 60 + sm) <= now_min <= (eh * 60 + em):
            return True
    return False


def send_wecom(text):
    payload = {"msgtype": "text", "text": {"content": text}}
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}"
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"},
                             data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=10)
        return resp.json()
    except Exception as e:
        print(f"  push fail: {e}", flush=True)
        return None


def format_label(name, month, otype, strike):
    """格式化标签: 上证50ETF-2607-P-2850"""
    if strike >= 100:
        sk_str = f"{strike:.0f}"
    elif strike >= 10:
        sk_str = f"{strike:.1f}"
    else:
        # ETF行权价如2.85 → 2850（*1000，与产品代码02850对齐）
        sk_str = f"{strike * 1000:.0f}"
    return f"{name}-{month}-{otype}-{sk_str}"


def build_pool():
    """构建所有标的的平值附近合约池"""
    pool = []

    # ETF 期权
    for code, name, month, lo, hi in [
        ("510050.XSHG", "上证50ETF", "2607", 2.75, 3.30),
        ("510300.XSHG", "沪深300ETF", "2607", 3.80, 5.50),
        ("510500.XSHG", "中证500ETF", "2607", 5.00, 8.00),
        ("588000.XSHG", "科创50ETF", "2607", 1.45, 2.00),
    ]:
        c = _build_etf_pool(code, name, month, lo, hi)
        if c:
            pool.append((name, c))

    # 期货期权（暂不开启，数据加载慢）
    # for code, name, month, lo, hi in [...]

    return pool


def _build_etf_pool(underlying, name, month, sk_min, sk_max):
    """筛选 ETF 期权近月平值合约"""
    cons = rq.options.get_contracts(underlying)
    if not cons:
        return []
    sample = cons[-50:] if len(cons) > 50 else cons
    props = rq.options.get_contract_property(sample)
    if props is None or props.empty:
        return []

    selected = {}
    for idx in props.index:
        oid = idx[0]
        pn = str(props.loc[idx, "product_name"])
        sk = float(props.loc[idx, "strike_price"])
        if month not in pn:
            continue
        if sk < sk_min or sk > sk_max:
            continue
        if oid in selected:
            continue
        otype = "C" if "C" in pn else "P"
        selected[oid] = {
            "code": oid,
            "label": format_label(name, month, otype, sk),
            "strike": sk,
            "otype": otype,
            "month": month,
        }

    return list(selected.values())


def scan_minute(pool, alerted_set):
    """单次扫描 + 推送"""
    now = datetime.now(TZ)
    print(f"\n--- {now.strftime('%H:%M:%S')} ---", flush=True)

    if not is_trading_time():
        print("  [休市]", flush=True)
        alerted_set.clear()
        return

    start_str = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:00")
    end_str = now.strftime("%Y-%m-%d %H:%M:00")

    all_alerts = []

    for display_name, contracts in pool:
        if not contracts:
            continue
        codes = [c["code"] for c in contracts]
        label_map = {c["code"]: c["label"] for c in contracts}

        g = rq.options.get_greeks(
            codes, start_date=start_str, end_date=end_str,
            fields=["iv"], frequency="1m"
        )
        if g is None or g.empty:
            continue

        for oid in codes:
            try:
                sub = g.loc[oid]
            except:
                continue
            rows = sub[sub["iv"] > 1e-6]
            if len(rows) < 2:
                continue
            last_two = rows.tail(2)
            prev_iv = last_two.iloc[0]["iv"]
            curr_iv = last_two.iloc[1]["iv"]
            if prev_iv <= 0:
                continue

            pct = (curr_iv - prev_iv) / prev_iv * 100
            pct_abs = abs(pct)
            direction = "up" if pct > 0 else "down"
            iv_str = f"{'+' if pct>0 else ''}{pct:.1f}%"

            if (direction == "up" and pct_abs >= THRESHOLD_UP) or \
               (direction == "down" and pct_abs >= THRESHOLD_DOWN):
                all_alerts.append({
                    "label": label_map[oid],
                    "iv_change_str": iv_str,
                    "direction": direction,
                    "iv_change_pct": pct_abs,
                })

    if not all_alerts:
        print("  无预警", flush=True)
        alerted_set.clear()
        return

    rise = [a for a in all_alerts if a["direction"] == "up"]
    fall = [a for a in all_alerts if a["direction"] == "down"]
    rise.sort(key=lambda x: x["iv_change_pct"], reverse=True)
    fall.sort(key=lambda x: x["iv_change_pct"], reverse=True)

    # ── 推送 ──
    new_items = [a for a in all_alerts if a["label"] not in alerted_set]
    for item in new_items:
        label = item["label"]
        iv_str = item["iv_change_str"]
        pct = item["iv_change_pct"]

        if item["direction"] == "up":
            if pct >= 5:
                emoji, title = "🚨", "隐波大幅升高预警"
            elif pct >= 3:
                emoji, title = "🌶️", "隐波升高预警"
            else:
                emoji, title = "⚠️", "隐波升高预警"
            msg = f"[{_ts()}] {emoji} {title}\n{label} 隐波变化{iv_str}↑"
        else:
            emoji, title = "🍀", "隐波降低预警"
            msg = f"[{_ts()}] {emoji} {title}\n{label} 隐波变化{iv_str}↓"

        r = send_wecom(msg)
        s = "OK" if (r and r.get("errcode") == 0) else "FAIL"
        print(f"  [{item['direction']}] {label} IV={iv_str} [{s}]", flush=True)
        alerted_set.add(item["label"])

    # 汇总
    repeat_items = [a for a in all_alerts if a["label"] in alerted_set]
    if repeat_items:
        up_names = [a["label"] for a in repeat_items if a["direction"] == "up"]
        dn_names = [a["label"] for a in repeat_items if a["direction"] == "down"]
        lines = []
        if up_names:
            lines.append(f"🔥 隐波还在升高: {'、'.join(up_names[:5])}")
        if dn_names:
            lines.append(f"🍀 隐波还在降低: {'、'.join(dn_names[:5])}")
        msg = f"[{_ts()}]\n" + "\n".join(lines)
        r = send_wecom(msg)
        s = "OK" if (r and r.get("errcode") == 0) else "FAIL"
        print(f"  [汇总] [{s}]", flush=True)


def main_loop():
    print("=" * 55, flush=True)
    print("  RQData 隐波变化监控", flush=True)
    print(f"  上升 >= {THRESHOLD_UP}%  下降 >= {THRESHOLD_DOWN}%", flush=True)
    print(f"  轮询每 {POLL_INTERVAL}s", flush=True)
    print("=" * 55, flush=True)

    rq.init(username="license", password=LICENSE_KEY)

    print("\n构建近月平值合约池...", flush=True)
    pool = build_pool()
    total = sum(len(c) for _, c in pool)
    print(f"  共 {len(pool)} 个品类, {total} 个合约\n", flush=True)
    for name, cons in pool:
        labels = [c["label"] for c in cons]
        print(f"  {name:>8} ({len(cons)}个): {' | '.join(labels)}", flush=True)

    alerted_set = set()

    while True:
        try:
            scan_minute(pool, alerted_set)
        except Exception as e:
            print(f"  [ERROR] {e}", flush=True)
            import traceback; traceback.print_exc()

        for remaining in range(POLL_INTERVAL, 0, -1):
            if remaining <= 5:
                print(f"  {remaining}s...", flush=True, end="\r")
            stime.sleep(1)
        print("  " + " " * 10, end="\r", flush=True)


if __name__ == "__main__":
    main_loop()
