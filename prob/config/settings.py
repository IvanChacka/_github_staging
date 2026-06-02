from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(r"D:\Ivan\prob")
DATA_DIR = BASE_DIR / "data"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
CRAWLER_DIR = BASE_DIR / "crawler"
PROCESSOR_DIR = BASE_DIR / "processor"
APP_DIR = BASE_DIR / "app"
CONFIG_DIR = BASE_DIR / "config"

for path in [
    BASE_DIR,
    DATA_DIR,
    STATE_DIR,
    LOG_DIR,
    CRAWLER_DIR,
    PROCESSOR_DIR,
    APP_DIR,
    CONFIG_DIR,
]:
    path.mkdir(parents=True, exist_ok=True)

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
REQUEST_TIMEOUT = 45
REQUEST_SLEEP_SECONDS = 0.15
MINUTE_FREQUENCY = 1
ANOMALY_THRESHOLD = 0.05
DISCOVERY_PAGE_SIZE = 100
DISCOVERY_MAX_PAGES = 80
APP_HOST = "0.0.0.0"
APP_PORT = 8510
TIMEZONE_NAME = "America/New_York"
ROLLING_WINDOW_HOURS = 24
ROLLING_RETENTION_MINUTES = ROLLING_WINDOW_HOURS * 60
VOLUME_FAILURE_THRESHOLD = 5
KEYWORDS = ["gold", "silver", "oil", "wti", "crude", "copper", "natural gas", "ipo", "initial public offering", "public offering", "direct listing", "go public", "stock market debut"]

# --- LLM settings ---
LLM_API_KEY_PATH = STATE_DIR / "llm_api_key.txt"        # write your key here
LLM_BASE_URL = "https://api.deepseek.com/v1"
LLM_MODEL = "deepseek-chat"
LLM_TIMEOUT = 30

MODULE_CONFIG = {
    "oil": {"label": "原油模块", "keywords": ["oil", "wti", "crude"]},
    "gold": {"label": "黄金模块", "keywords": ["gold", "xauusd", "gc"]},
    "silver": {"label": "白银模块", "keywords": ["silver", "xagusd", "si"]},
    "copper": {"label": "铜模块", "keywords": ["copper", "xcusd", "hg"]},
    "natgas": {"label": "天然气模块", "keywords": ["natural gas", "natgas"]},
    "ipo": {"label": "IPO模块", "keywords": ["ipo", "initial public offering", "public offering", "direct listing", "go public", "stock market debut", "de-spac"]},
}
