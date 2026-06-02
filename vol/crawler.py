"""
OpenVlab 隐波爬虫 - 抓取隐波变化最大上升/最大下降的前5个品种
每30分钟运行一次并输出报告
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from market_utils import (
    PLAYWRIGHT_IMPORT_ERROR,
    configure_stdio,
    fetch_market_snapshot,
    parse_text_data,
)

if PLAYWRIGHT_IMPORT_ERROR is not None:
    print("请先安装: pip install playwright && python -m playwright install chromium")
    sys.exit(1)

configure_stdio()

DATA_FILE = os.path.join(os.path.dirname(__file__), "vol_data.json")
TZ = ZoneInfo("Asia/Shanghai")


def format_report(rise_data, fall_data, update_time):
    """生成格式化的报告文本"""
    lines = []
    lines.append("[" + update_time + "] OpenVlab 隐波变化速报")
    lines.append("=" * 40)
    lines.append("")

    lines.append("【隐波最大上升 TOP5】")
    lines.append("-" * 35)
    lines.append("品种".ljust(10) + " 涨跌幅".rjust(8) + " 隐波变化".rjust(8))
    lines.append("-" * 35)
    for item in rise_data:
        name = item.get("name", "")
        pct = item.get("pct_change", "")
        vol_change = item.get("vol_change", "")
        lines.append(name.ljust(10) + " " + pct.rjust(8) + " " + vol_change.rjust(8))
    lines.append("")

    lines.append("【隐波最大下降 TOP5】")
    lines.append("-" * 35)
    lines.append("品种".ljust(10) + " 涨跌幅".rjust(8) + " 隐波变化".rjust(8))
    lines.append("-" * 35)
    for item in fall_data:
        name = item.get("name", "")
        pct = item.get("pct_change", "")
        vol_change = item.get("vol_change", "")
        lines.append(name.ljust(10) + " " + pct.rjust(8) + " " + vol_change.rjust(8))
    lines.append("")

    lines.append("-" * 40)
    lines.append("来源: OpenVlab | 更新: " + update_time)

    return "\n".join(lines)


async def fetch_market_data():
    """获取页面卡片数据，保留原返回结构以兼容现有调用。"""
    return await fetch_market_snapshot()


async def run_once():
    """运行一次抓取并返回报告"""
    now_str = datetime.now(TZ).strftime("%H:%M")
    print("[" + now_str + "] 开始抓取...")

    try:
        snapshot = await fetch_market_data()
    except Exception as e:
        print("抓取出错: " + str(e))
        import traceback
        traceback.print_exc()
        return None

    rise = snapshot.get("rise", [])
    fall = snapshot.get("fall", [])
    text = snapshot.get("text", "")
    if not rise or not fall:
        rise, fall = parse_text_data(text)

    print("\n隐波最大上升:")
    for item in rise:
        print("  " + item['name'].rjust(8) + "  " + item['pct_change'].rjust(8) + "  " + item['vol_change'].rjust(8))

    print("\n隐波最大下降:")
    for item in fall:
        print("  " + item['name'].rjust(8) + "  " + item['pct_change'].rjust(8) + "  " + item['vol_change'].rjust(8))

    report = format_report(rise, fall, now_str)

    data = {
        "time": datetime.now(TZ).isoformat(),
        "rise": rise,
        "fall": fall,
        "report": report,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return report


async def run_loop():
    """持续运行循环"""
    print("OpenVlab 隐波监控启动 - 每30分钟抓取一次")
    print("=" * 50)

    while True:
        try:
            report = await run_once()
            if report:
                print("\n" + report)
        except Exception as e:
            print("抓取出错: " + str(e))
            import traceback
            traceback.print_exc()

        print("\n等待30分钟... (" + datetime.now(TZ).strftime('%H:%M') + ")")
        await asyncio.sleep(30 * 60)


if __name__ == "__main__":
    # Default: run once
    report = asyncio.run(run_once())
    if report:
        print("\n" + report)
