from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class IntradayConfig:
    execution_stage: str
    candle_minutes: int
    scan_interval_minutes: int
    max_capital_weight: Decimal
    max_position_notional_rub: Decimal
    max_concurrent_positions: int
    max_entries_per_day: int
    max_daily_loss_weight: Decimal
    max_daily_turnover_rub: Decimal
    allow_short: bool
    allow_overnight: bool
    order_type: str
    order_ttl_seconds: int
    enabled_strategies: tuple[str, ...]
    new_entries_start_moscow: time
    new_entries_stop_moscow: time
    force_flat_moscow: time
    history_bars: int
    min_price_move: Decimal
    min_abs_trade_imbalance: Decimal
    min_abs_order_flow: Decimal
    min_abs_book_imbalance: Decimal
    max_spread_bbo: Decimal


def load_intraday_config(path: Path) -> IntradayConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("intraday configuration must be an object")
    risk = raw.get("risk")
    execution = raw.get("execution")
    session = raw.get("session")
    signal = raw.get("signal")
    strategies = raw.get("strategies")
    if not all(isinstance(item, dict) for item in (risk, execution, session, signal)):
        raise ValueError("intraday risk/execution/session/signal sections must be objects")
    assert isinstance(risk, dict)
    assert isinstance(execution, dict)
    assert isinstance(session, dict)
    assert isinstance(signal, dict)
    if not isinstance(strategies, list):
        raise ValueError("intraday strategies must be an array")
    enabled: list[str] = []
    for item in strategies:
        if not isinstance(item, dict) or not isinstance(item.get("enabled"), bool):
            raise ValueError("intraday strategy entries must be valid objects")
        if item["enabled"]:
            enabled.append(str(item.get("strategy_id", "")).strip())
    config = IntradayConfig(
        execution_stage=str(raw.get("execution_stage", "")),
        candle_minutes=_integer(raw.get("candle_minutes"), "candle_minutes"),
        scan_interval_minutes=_integer(
            raw.get("scan_interval_minutes"), "scan_interval_minutes"
        ),
        max_capital_weight=Decimal(str(risk.get("max_capital_weight", "0"))),
        max_position_notional_rub=Decimal(
            str(risk.get("max_position_notional_rub", "0"))
        ),
        max_concurrent_positions=_integer(
            risk.get("max_concurrent_positions"), "max_concurrent_positions"
        ),
        max_entries_per_day=_integer(
            risk.get("max_entries_per_day"), "max_entries_per_day"
        ),
        max_daily_loss_weight=Decimal(str(risk.get("max_daily_loss_weight", "0"))),
        max_daily_turnover_rub=Decimal(str(risk.get("max_daily_turnover_rub", "0"))),
        allow_short=_boolean(risk.get("allow_short"), "allow_short"),
        allow_overnight=_boolean(risk.get("allow_overnight"), "allow_overnight"),
        order_type=str(execution.get("order_type", "")),
        order_ttl_seconds=_integer(
            execution.get("order_ttl_seconds"), "order_ttl_seconds"
        ),
        enabled_strategies=tuple(enabled),
        new_entries_start_moscow=time.fromisoformat(
            str(session.get("new_entries_start_moscow", ""))
        ),
        new_entries_stop_moscow=time.fromisoformat(
            str(session.get("new_entries_stop_moscow", ""))
        ),
        force_flat_moscow=time.fromisoformat(str(session.get("force_flat_moscow", ""))),
        history_bars=_integer(signal.get("history_bars"), "history_bars"),
        min_price_move=Decimal(str(signal.get("min_price_move", "0"))),
        min_abs_trade_imbalance=Decimal(
            str(signal.get("min_abs_trade_imbalance", "0"))
        ),
        min_abs_order_flow=Decimal(str(signal.get("min_abs_order_flow", "0"))),
        min_abs_book_imbalance=Decimal(
            str(signal.get("min_abs_book_imbalance", "0"))
        ),
        max_spread_bbo=Decimal(str(signal.get("max_spread_bbo", "0"))),
    )
    _validate(config)
    return config


def _validate(config: IntradayConfig) -> None:
    if config.execution_stage not in {"research_only", "sandbox"}:
        raise ValueError("intraday execution_stage must be research_only or sandbox")
    if config.candle_minutes != 5 or config.scan_interval_minutes != 5:
        raise ValueError("intraday v1 requires completed 5-minute intervals")
    if not Decimal("0") < config.max_capital_weight <= Decimal("0.10"):
        raise ValueError("intraday max_capital_weight must be in (0, 0.10]")
    if not Decimal("0") < config.max_daily_loss_weight <= Decimal("0.01"):
        raise ValueError("intraday max_daily_loss_weight must be in (0, 0.01]")
    if config.max_position_notional_rub <= 0 or config.max_daily_turnover_rub <= 0:
        raise ValueError("intraday notional limits must be positive")
    if not 1 <= config.max_concurrent_positions <= 2:
        raise ValueError("intraday max_concurrent_positions must be 1 or 2")
    if not 1 <= config.max_entries_per_day <= 3:
        raise ValueError("intraday max_entries_per_day must be between 1 and 3")
    if config.allow_overnight:
        raise ValueError("intraday positions cannot be carried overnight")
    if config.order_type != "limit" or not 5 <= config.order_ttl_seconds <= 300:
        raise ValueError("intraday v1 requires short-lived limit orders")
    if not config.enabled_strategies or any(not item for item in config.enabled_strategies):
        raise ValueError("at least one named intraday strategy must be enabled")
    if not (
        config.new_entries_start_moscow
        < config.new_entries_stop_moscow
        < config.force_flat_moscow
    ):
        raise ValueError("intraday session times must be strictly increasing")
    if not 3 <= config.history_bars <= 12:
        raise ValueError("intraday history_bars must be between 3 and 12")
    for name, value in (
        ("min_price_move", config.min_price_move),
        ("min_abs_trade_imbalance", config.min_abs_trade_imbalance),
        ("min_abs_order_flow", config.min_abs_order_flow),
        ("min_abs_book_imbalance", config.min_abs_book_imbalance),
        ("max_spread_bbo", config.max_spread_bbo),
    ):
        if not value.is_finite() or value <= 0 or value > 1:
            raise ValueError(f"intraday {name} must be in (0, 1]")


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value
