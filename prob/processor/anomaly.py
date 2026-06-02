from __future__ import annotations

from dataclasses import dataclass

from config.settings import ANOMALY_THRESHOLD


@dataclass
class ChangeResult:
    old_value: float | None
    new_value: float | None
    change_ratio: float | None
    is_anomaly: bool


def compute_change(
    old_value: float | None,
    new_value: float | None,
    *,
    metric: str = "probability",
) -> ChangeResult:
    """
    Compute the change and determine if it's an anomaly.

    For probability: use absolute change (|new - old| >= ANOMALY_THRESHOLD).
      ANOMALY_THRESHOLD=0.05 means ≥5 percentage points change.
      change_ratio is stored as abs_change (points) for display.

    For volume: use relative change (|new - old| / old >= ANOMALY_THRESHOLD).
      ANOMALY_THRESHOLD=0.05 means ≥5% relative change.
    """
    if old_value is None or new_value is None:
        return ChangeResult(old_value, new_value, None, False)

    if metric == "volume":
        # Volume: relative change, with protection for old_value=0
        if old_value == 0:
            return ChangeResult(old_value, new_value, None, abs(new_value) >= 1.0)
        change_ratio = (new_value - old_value) / abs(old_value)
        return ChangeResult(old_value, new_value, change_ratio, abs(change_ratio) >= ANOMALY_THRESHOLD)
    else:
        # Probability: absolute change in percentage points
        abs_change = abs(new_value - old_value)
        change_ratio = new_value - old_value  # signed absolute change
        return ChangeResult(old_value, new_value, change_ratio, abs_change >= ANOMALY_THRESHOLD)
