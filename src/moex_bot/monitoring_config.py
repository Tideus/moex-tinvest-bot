from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_settings(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for key in ("disk_warn_pct", "disk_critical_pct", "disk_min_free_gib", "failures_to_alert",
                "timeout_seconds", "repeat_hours", "intraday_max_age_seconds",
                "long_max_age_seconds", "backup_max_age_hours"):
        if isinstance(raw.get(key), bool) or not isinstance(raw.get(key), (int, float)):
            raise ValueError(f"invalid monitoring setting: {key}")
        if raw[key] <= 0:
            raise ValueError(f"nonpositive monitoring setting: {key}")
    if not 0 < raw["disk_critical_pct"] < raw["disk_warn_pct"] < 100:
        raise ValueError("disk thresholds must satisfy critical < warning < 100")
    if not isinstance(raw.get("calendar_required"), bool):
        raise ValueError("calendar_required must be boolean")
    envs = raw.get("required_tinvest_environments")
    if not isinstance(envs, list) or any(env not in {"sandbox", "prod"} for env in envs):
        raise ValueError("invalid required_tinvest_environments")
    return raw
