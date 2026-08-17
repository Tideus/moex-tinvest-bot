from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import ExecutionMode


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    top_n: int
    min_momentum: Decimal
    require_above_trend: bool


@dataclass(frozen=True, slots=True)
class BotConfig:
    mode: ExecutionMode
    base_currency: str
    max_data_age_seconds: int
    max_position_weight: Decimal
    max_order_notional: Decimal
    max_gross_exposure: Decimal
    max_daily_turnover: Decimal
    min_trade_notional: Decimal
    max_open_orders: int
    allow_margin: bool
    live_interlock: bool
    strategy: StrategyConfig

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.base_currency != "RUB":
            errors.append("base_currency must be RUB in the MVP")
        if self.max_data_age_seconds <= 0:
            errors.append("max_data_age_seconds must be positive")
        for name, value in (
            ("max_position_weight", self.max_position_weight),
            ("max_gross_exposure", self.max_gross_exposure),
        ):
            if not value.is_finite() or not Decimal("0") < value <= Decimal("1"):
                errors.append(f"{name} must be in (0, 1]")
        for name, value in (
            ("max_order_notional", self.max_order_notional),
            ("max_daily_turnover", self.max_daily_turnover),
            ("min_trade_notional", self.min_trade_notional),
        ):
            if not value.is_finite() or value <= 0:
                errors.append(f"{name} must be positive and finite")
        if self.min_trade_notional > self.max_order_notional:
            errors.append("min_trade_notional must not exceed max_order_notional")
        if self.max_order_notional > self.max_daily_turnover:
            errors.append("max_order_notional must not exceed max_daily_turnover")
        if self.max_open_orders <= 0:
            errors.append("max_open_orders must be positive")
        if self.allow_margin:
            errors.append("margin is forbidden in the MVP")
        if self.mode is ExecutionMode.LIVE:
            errors.append("live mode is not implemented or authorized in this scaffold")
        if self.live_interlock:
            errors.append("live_interlock must remain false until a reviewed live adapter exists")
        if self.strategy.top_n <= 0:
            errors.append("strategy.top_n must be positive")
        if (
            not self.strategy.min_momentum.is_finite()
            or self.strategy.min_momentum <= Decimal("-1")
        ):
            errors.append("strategy.min_momentum must be finite and greater than -1")
        return tuple(errors)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def load_config(path: Path) -> BotConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    strategy = raw["strategy"]
    config = BotConfig(
        mode=ExecutionMode(raw["mode"]),
        base_currency=str(raw["base_currency"]),
        max_data_age_seconds=int(raw["max_data_age_seconds"]),
        max_position_weight=_decimal(raw["max_position_weight"]),
        max_order_notional=_decimal(raw["max_order_notional"]),
        max_gross_exposure=_decimal(raw["max_gross_exposure"]),
        max_daily_turnover=_decimal(raw["max_daily_turnover"]),
        min_trade_notional=_decimal(raw["min_trade_notional"]),
        max_open_orders=int(raw["max_open_orders"]),
        allow_margin=_boolean(raw["allow_margin"], "allow_margin"),
        live_interlock=_boolean(raw["live_interlock"], "live_interlock"),
        strategy=StrategyConfig(
            top_n=int(strategy["top_n"]),
            min_momentum=_decimal(strategy["min_momentum"]),
            require_above_trend=_boolean(
                strategy["require_above_trend"], "strategy.require_above_trend"
            ),
        ),
    )
    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    return config
