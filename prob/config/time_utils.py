from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from config.settings import TIMEZONE_NAME


def _build_timezone():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=-4))


TZ = _build_timezone()
UTC = timezone.utc


def now_et() -> datetime:
    return datetime.now(TZ)


def current_et_date_str() -> str:
    return now_et().strftime("%Y-%m-%d")


def et_day_start(dt: datetime | None = None) -> datetime:
    value = dt or now_et()
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def floor_to_minute(dt: datetime | None = None) -> datetime:
    value = dt or now_et()
    return value.replace(second=0, microsecond=0)


def rolling_window_start(dt: datetime | None = None, hours: int = 24) -> datetime:
    value = floor_to_minute(dt or now_et())
    return value - timedelta(hours=hours)


def to_utc_timestamp(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.astimezone(UTC).timestamp())


def isoformat_minute(dt: datetime) -> str:
    return floor_to_minute(dt).isoformat()
