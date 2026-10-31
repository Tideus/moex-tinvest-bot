"""Read-only preparation report for a supervised experiment, never a trading runner."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any


def load_experiment_settings(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("experiment settings must be an object")
    fixed: dict[str, object] = {
        "version": 1,
        "mode": "supervised_experiment",
        "admission": "/etc/moex-tinvest-bot/prod-experiment-admission.json",
        "account_id_env": "T_INVEST_PROD_ACCOUNT_ID",
        "automatic_entries": False,
        "protection_required": True,
        "protect_manual_positions": True,
        "allow_margin": False,
        "allow_short": False,
        "allowed_asset_classes": ["share"],
    }
    required = set(fixed) | {
        "capital_limit_rub",
        "max_order_notional_rub",
        "max_daily_turnover_rub",
        "max_attempts_per_day",
        "maximum_first_entry_lots",
        "max_daily_loss_rub",
        "journal",
        "protection_journal",
        "stage",
        "enabled",
    }
    if set(raw) != required:
        raise ValueError("experiment settings have missing or unknown fields")
    for field, expected in fixed.items():
        if type(raw[field]) is not type(expected) or raw[field] != expected:
            raise ValueError(f"unsafe experiment setting: {field}")
    if raw["stage"] not in ("preparation_only", "supervised_canary"):
        raise ValueError("invalid supervised experiment stage")
    if type(raw["enabled"]) is not bool or (raw["enabled"] and raw["stage"] != "supervised_canary"):
        raise ValueError("enabled requires explicit supervised_canary stage")
    for field, maximum in (
        ("capital_limit_rub", Decimal("40000")),
        ("max_order_notional_rub", Decimal("10000")),
        ("max_daily_turnover_rub", Decimal("20000")),
        ("max_daily_loss_rub", Decimal("600")),
    ):
        value = raw[field]
        if not isinstance(value, str):
            raise ValueError(f"experiment {field} must be a decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"invalid experiment amount: {field}") from exc
        if not amount.is_finite() or not 0 < amount <= maximum:
            raise ValueError(f"experiment {field} exceeds its positive hard limit")
    for field, integer_maximum in (("max_attempts_per_day", 2), ("maximum_first_entry_lots", 1)):
        if type(raw[field]) is not int or not 0 < raw[field] <= integer_maximum:
            raise ValueError(f"invalid experiment integer limit: {field}")
    if (
        Decimal(raw["max_order_notional_rub"]) > Decimal(raw["capital_limit_rub"])
        or Decimal(raw["max_daily_turnover_rub"]) > Decimal(raw["capital_limit_rub"])
        or Decimal(raw["max_daily_loss_rub"]) > Decimal(raw["capital_limit_rub"]) * Decimal(".015")
    ):
        raise ValueError("experiment limits are inconsistent with capital")
    paths = []
    for field in ("journal", "protection_journal"):
        value = raw[field]
        if not isinstance(value, str):
            raise ValueError(f"experiment {field} must be a server path")
        target = PurePosixPath(value)
        base = PurePosixPath("/var/lib/moex-tinvest-bot/execution")
        if (
            not target.is_absolute()
            or ".." in target.parts
            or not target.is_relative_to(base)
            or target.suffix != ".sqlite3"
        ):
            raise ValueError(f"experiment {field} must be an execution sqlite3 path")
        paths.append(target)
    if paths[0] == paths[1]:
        raise ValueError("order and protection journals must be distinct")
    return raw


def readiness_report(root: Path, settings_path: Path) -> dict[str, Any]:
    settings = load_experiment_settings(settings_path)
    policy = json.loads((root / "config/prod_long.json").read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("manual_position_policy") != "protect":
        raise ValueError("production policy must protect manual positions")
    protected = policy.get("protected_secids")
    if (
        not isinstance(protected, list)
        or not protected
        or any(not isinstance(item, str) or not item.strip() for item in protected)
    ):
        raise ValueError("explicit protected manual securities are required")
    return {
        "mode": settings["mode"],
        "stage": settings["stage"],
        "ready_for_orders": False,
        "order_submitted": False,
        "api_called": False,
        "limits": {
            key: settings[key]
            for key in (
                "capital_limit_rub",
                "max_order_notional_rub",
                "max_daily_turnover_rub",
                "max_attempts_per_day",
                "maximum_first_entry_lots",
                "max_daily_loss_rub",
            )
        },
        "protected_secids": sorted(set(protected)),
        "required_evidence": {
            "native_protection": "NOT_VERIFIED_BY_THIS_REPORT",
            "broker_reconciliation": "NOT_VERIFIED_BY_THIS_REPORT",
            "restore_drill": "NOT_VERIFIED_BY_THIS_REPORT",
            "independent_review": "NOT_VERIFIED_BY_THIS_REPORT",
            "exact_first_order_confirmation": "REQUIRED_AFTER_FRESH_PREVIEW",
        },
        "disclaimer": (
            "Experimental admission is not evidence of strategy profitability. "
            "A 600 RUB loss gate is not a guaranteed maximum loss: gaps, slippage "
            "and unavailable execution can exceed it. No admission or scheduler is created."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args()
    try:
        report = readiness_report(
            args.root, args.settings or args.root / "config/prod_experiment.json"
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ready_for_orders": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
