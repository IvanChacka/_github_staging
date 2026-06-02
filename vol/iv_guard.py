"""
OpenVlab 隐波监控（每分钟轮询 + 企业微信推送）
每次执行独立 Playwright 实例，不影响用户浏览器
"""
import asyncio
import json
import re
import sys
import time as sync_time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from market_utils import (
    PLAYWRIGHT_IMPORT_ERROR,
    configure_stdio,
    fetch_market_snapshot,
    parse_text_data,
)

WECOM_KEY = "YOUR_WECOM_WEBHOOK_KEY"
THRESHOLD_UP = 2.0
THRESHOLD_DOWN = 3.0
POLL_INTERVAL = 60
TZ = ZoneInfo("Asia/Shanghai")
HEARTBEAT_FILE = Path(__file__).with_name("iv_guard.heartbeat.json")

# ── 品种 → 主力合约ID映射（从抓取结果动态更新） ──
# key=品种名(如"焦煤"), value=[合约slug列表]（如["jm2607","jm2608"]）
PRODUCT_CONTRACTS: dict[str, list[str]] = {}

# 中文明 → 合约ID前缀（用于从contract_slug反推品种名）
CN_TO_PREFIX: dict[str, str] = {}
# 合约ID前缀 → 中文明（如"jm"→"焦煤"）
PREFIX_TO_CN: dict[str, str] = {}

TRADING_SESSIONS = [
    (9, 0, 10, 15), (10, 30, 11, 30),
    (13, 30, 15, 0), (21, 0, 23, 59), (0, 0, 2, 30),
]

configure_stdio()

if PLAYWRIGHT_IMPORT_ERROR is not None:
    raise SystemExit("请先安装: pip install playwright && python -m playwright install chromium")


def is_trading_time():
    now = datetime.now(TZ)
    now_min = now.hour * 60 + now.minute
    for sh, sm, eh, em in TRADING_SESSIONS:
        if (sh * 60 + sm) <= now_min <= (eh * 60 + em):
            return True
    return False


def _ts():
    return datetime.now(TZ).strftime("%m-%d %H:%M")


def write_heartbeat(status, **extra):
    payload = {
        "time": datetime.now(TZ).isoformat(),
        "ts": sync_time.time(),
        "status": status,
    }
    payload.update(extra)
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False)


def check_alerts(rise, fall):
    alerts = []
    for item in rise:
        try:
            iv = float(item["vol_change"].replace("%", ""))
        except (ValueError, AttributeError):
            continue
        if abs(iv) >= THRESHOLD_UP:
            alerts.append({"name": item["name"], "pct_change": item["pct_change"],
                           "vol_change": item["vol_change"], "direction": "up"})
    for item in fall:
        try:
            iv = float(item["vol_change"].replace("%", ""))
        except (ValueError, AttributeError):
            continue
        if abs(iv) >= THRESHOLD_DOWN:
            alerts.append({"name": item["name"], "pct_change": item["pct_change"],
                           "vol_change": item["vol_change"], "direction": "down"})
    return alerts

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


def _print_section(title, items):
    print(f"  {title}:", flush=True)
    for item in items:
        print(
            f"    {item['name']:8s}  {item['pct_change']:>8s}  IV:{item['vol_change']:>8s}",
            flush=True,
        )


def run_once(alerted_set):
    """一次抓取+推送（纯同步包装）"""
    async def _inner():
        try:
            t0 = datetime.now(TZ)
            write_heartbeat("running", phase="start")
            print(f"\n--- {t0.strftime('%H:%M:%S')} ---", flush=True)

            # Fetch
            def on_progress(phase, body_len):
                write_heartbeat("running", phase=phase, body_len=body_len)
                if body_len <= 500:
                    print(f"  wait... len={body_len}", flush=True)

            snapshot = await fetch_market_snapshot(progress=on_progress)
            text = snapshot.get("text", "")
            write_heartbeat("running", phase="fetched", body_len=len(text))
            print(f"  body={len(text)}", flush=True)

            # Parse
            rise = snapshot.get("rise", [])
            fall = snapshot.get("fall", [])
            if not rise or not fall:
                rise, fall = parse_text_data(text)
            write_heartbeat("running", phase="parsed", rise=len(rise), fall=len(fall))
            _print_section("上升", rise)
            _print_section("下降", fall)

            # ── 更新品种→主力合约映射 ──
            for item in rise + fall:
                slug = item.get("contract_slug", "")
                name = item.get("name", "")
                if slug and name:
                    # 提取前缀（如 "JM202607" → "jm"）
                    prefix = re.sub(r"\d+.*$", "", slug).lower()
                    CN_TO_PREFIX[name] = prefix
                    PREFIX_TO_CN[prefix] = name
                    # 提取后4位数字 + 前缀字母作为合约简称，如"JM202607"→"JM2607"
                    prefix_letters = re.match(r"([A-Za-z]+)", slug)
                    suffix = slug[-4:] if len(slug) >= 4 else slug
                    contract_short = f"{prefix_letters.group(1).upper()}{suffix}" if prefix_letters else slug
                    if name not in PRODUCT_CONTRACTS:
                        PRODUCT_CONTRACTS[name] = []
                    if contract_short not in PRODUCT_CONTRACTS[name]:
                        PRODUCT_CONTRACTS[name].append(contract_short)
                        # 保持排序（最新的在前）
                        PRODUCT_CONTRACTS[name].sort(reverse=True)

            if not is_trading_time():
                print("  [休市]", flush=True)
                alerted_set.clear()
                write_heartbeat("sleeping", reason="market_closed")
                return

            alerts = check_alerts(rise, fall)
            if not alerts:
                print("  无预警", flush=True)
                alerted_set.clear()
                write_heartbeat("sleeping", reason="no_alert")
                return

            def _name_with_contracts(product_name: str) -> str:
                """品种名后附加当前监测到的主力合约，如焦煤(JM2607)
                合约slug格式如 "JM202607" 或 "jm2607"，提取字母+后4位数字"""
                contracts = PRODUCT_CONTRACTS.get(product_name, [])
                if contracts:
                    short_contracts = []
                    for c in contracts:
                        # 提取前缀字母 + 后4位数字，如 "JM202607" -> "JM2607"
                        prefix = re.match(r"([A-Za-z]+)", c)
                        suffix = c[-4:] if len(c) >= 4 else c
                        if prefix:
                            short_contracts.append(f"{prefix.group(1).upper()}{suffix}")
                        else:
                            short_contracts.append(suffix)
                    if short_contracts:
                        return f"{product_name}({'、'.join(short_contracts[:3])})"
                return product_name

            # 新品种推送
            new_items = [a for a in alerts if a["name"] not in alerted_set]
            for item in new_items:
                name = item["name"]
                name_ext = _name_with_contracts(name)
                pct = item["pct_change"]
                iv = item["vol_change"]
                if item["direction"] == "up":
                    iv_num = float(iv.replace("%", ""))
                    if iv_num >= 5:
                        emoji, title = "🚨", "隐波大幅升高预警"
                    elif iv_num >= 3:
                        emoji, title = "🌶️", "隐波升高预警"
                    else:
                        emoji, title = "⚠️", "隐波升高预警"
                    msg = f"[{_ts()}] {emoji} {title}\n{name_ext} 涨跌幅{pct} 隐波变化{iv}↑"
                else:
                    emoji, title = "🍀", "隐波降低预警"
                    msg = f"[{_ts()}] {emoji} {title}\n{name_ext} 涨跌幅{pct} 隐波变化{iv}↓"
                r = send_wecom(msg)
                s = "OK" if (r and r.get("errcode") == 0) else "FAIL"
                print(f"  -> [{item['direction']}] {name_ext} IV={iv} [{s}]", flush=True)
                alerted_set.add(item["name"])
                write_heartbeat("running", phase="pushing", last_name=name)

            # 已有品种汇总
            repeat_items = [a for a in alerts if a["name"] in alerted_set]
            if repeat_items:
                up_names = [_name_with_contracts(a["name"]) for a in repeat_items if a["direction"] == "up"]
                dn_names = [_name_with_contracts(a["name"]) for a in repeat_items if a["direction"] == "down"]
                lines = []
                if up_names:
                    lines.append(f"🔥 隐波还在升高: {'、'.join(up_names)}")
                if dn_names:
                    lines.append(f"🍀 隐波还在降低: {'、'.join(dn_names)}")
                msg = f"[{_ts()}]\n" + "\n".join(lines)
                r = send_wecom(msg)
                s = "OK" if (r and r.get("errcode") == 0) else "FAIL"
                print(f"  -> [汇总] [{s}]", flush=True)

            write_heartbeat("running", phase="done", alert_count=len(alerts))

        except Exception as e:
            write_heartbeat("error", error=str(e))
            print(f"  [ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()
            sys.stdout.flush()

    try:
        asyncio.run(_inner())
    except Exception as e:
        write_heartbeat("error", error=str(e))

if __name__ == "__main__":
    print("=" * 60, flush=True)
    print("  OpenVlab 隐波监控守护进程", flush=True)
    print(f"  上升 >= {THRESHOLD_UP}%  下降 >= {THRESHOLD_DOWN}%", flush=True)
    print(f"  轮询每 {POLL_INTERVAL}s", flush=True)
    print("=" * 60, flush=True)

    alerted_set = set()
    while True:
        try:
            run_once(alerted_set)
        except Exception as e:
            write_heartbeat("error", error=str(e))
        for remaining in range(POLL_INTERVAL, 0, -1):
            write_heartbeat("sleeping", remaining=remaining)
            sync_time.sleep(1)
