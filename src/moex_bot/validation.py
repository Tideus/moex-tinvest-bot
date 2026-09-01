from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .account_profiles import load_account_registry
from .backtest import load_backtest_settings
from .backtest_reporting import load_promotion_gates
from .config import load_config
from .geo_feed import load_sources
from .intraday_config import load_intraday_config
from .notification_policy import load_notification_policy
from .ownership import load_ownership_disclosures
from .runtime_config import load_runtime_config
from .service_config import load_service_config
from .shadow import load_geo_feed, load_universe


def validate_project_configs(root: Path) -> tuple[str, ...]:
    checks: list[str] = []
    replay = load_config(root / "config" / "replay.json")
    shadow = load_config(root / "config" / "shadow.json")
    if replay.mode.value != "replay" or shadow.mode.value != "shadow":
        raise ValueError("replay/shadow mode files are mismatched")
    checks.extend(("config/replay.json", "config/shadow.json"))

    load_service_config(root / "config" / "services.json")
    checks.append("config/services.json")
    accounts = load_account_registry(root / "config" / "accounts.json")
    for profile in accounts.profiles:
        for strategy in profile.strategies:
            if not (root / strategy.config_path).is_file():
                raise ValueError(f"strategy config does not exist: {strategy.config_path}")
    checks.append("config/accounts.json")
    intraday = load_intraday_config(root / "config" / "intraday.json")
    intraday_account = accounts.by_id("intraday")
    if intraday_account.order_execution_enabled != (
        intraday.execution_stage == "sandbox"
    ):
        raise ValueError(
            "intraday account execution gate must match intraday execution_stage"
        )
    checks.append("config/intraday.json")
    load_notification_policy(root / "config" / "notifications.json")
    checks.append("config/notifications.json")
    runtime = load_runtime_config(root / "config" / "runtime.json")
    long_account = accounts.by_id("long")
    if long_account.order_execution_enabled != runtime.sandbox_orders_enabled:
        raise ValueError(
            "long account execution gate must match runtime sandbox_orders_enabled"
        )
    checks.append("config/runtime.json")
    load_universe(root / "config" / "universe.json")
    checks.append("config/universe.json")
    load_backtest_settings(root / "config" / "backtest.json")
    checks.append("config/backtest.json")
    load_promotion_gates(root / "config" / "promotion_gates.json")
    checks.append("config/promotion_gates.json")
    load_sources(root / "config" / "geo_sources.json")
    checks.append("config/geo_sources.json")

    ownership = _array(root / "config" / "ownership_disclosures.json")
    for item in ownership:
        if not isinstance(item, dict):
            raise ValueError("ownership disclosure entries must be objects")
        required = {
            "secid", "holder_name", "holder_type", "report_date", "published_at",
            "source_url", "source_kind", "verified",
        }
        if not required.issubset(item):
            raise ValueError("ownership disclosure entry is incomplete")
    load_ownership_disclosures(
        root / "config" / "ownership_disclosures.json",
        as_of=datetime.now(UTC),
    )
    checks.append("config/ownership_disclosures.json")

    portfolio = _object(root / "examples" / "portfolio_empty.json")
    if "cash" not in portfolio or not isinstance(portfolio.get("positions"), dict):
        raise ValueError("portfolio example is invalid")
    checks.append("examples/portfolio_empty.json")
    load_geo_feed(
        root / "examples" / "geo_events_empty.json",
        as_of=datetime.now(UTC),
    )
    checks.append("examples/geo_events_empty.json")

    replay_snapshot = _object(root / "examples" / "replay_snapshot.json")
    if not isinstance(replay_snapshot.get("market"), list) or not replay_snapshot["market"]:
        raise ValueError("replay snapshot market must be non-empty")
    checks.append("examples/replay_snapshot.json")
    return tuple(checks)


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _array(path: Path) -> list[Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a JSON array")
    return value
