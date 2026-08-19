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
    candle_period: str = "1h"
    momentum_windows: tuple[int, ...] = (5,)
    trend_window: int = 20
    volatility_window: int = 20
    volatility_floor: Decimal = Decimal("0.005")
    inverse_volatility_weights: bool = False
    exit_rank_buffer: int = 0
    rebalance_hours_moscow: tuple[int, ...] = tuple(range(24))
    shorts_enabled: bool = False
    short_top_n: int = 0
    max_short_momentum: Decimal = Decimal("-0.01")
    require_below_trend_for_short: bool = True
    long_target_gross: Decimal = Decimal("1")
    short_target_gross: Decimal = Decimal("0")


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
    min_cash_reserve_weight: Decimal = Decimal("0")
    max_sector_weight: Decimal = Decimal("1")
    max_risk_cluster_weight: Decimal = Decimal("1")
    max_short_position_weight: Decimal = Decimal("0")
    max_short_gross_exposure: Decimal = Decimal("0")

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.base_currency != "RUB":
            errors.append("base_currency must be RUB in the MVP")
        if self.max_data_age_seconds <= 0:
            errors.append("max_data_age_seconds must be positive")
        for name, value in (
            ("max_position_weight", self.max_position_weight),
            ("max_gross_exposure", self.max_gross_exposure),
            ("min_cash_reserve_weight", self.min_cash_reserve_weight),
            ("max_sector_weight", self.max_sector_weight),
            ("max_risk_cluster_weight", self.max_risk_cluster_weight),
        ):
            lower_ok = value >= 0 if name == "min_cash_reserve_weight" else value > 0
            if not value.is_finite() or not lower_ok or value > Decimal("1"):
                interval = "[0, 1]" if name == "min_cash_reserve_weight" else "(0, 1]"
                errors.append(f"{name} must be in {interval}")
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
        if self.strategy.shorts_enabled and not self.allow_margin:
            errors.append("shorts require allow_margin=true")
        if self.allow_margin and not self.strategy.shorts_enabled:
            errors.append("allow_margin is only permitted for the reviewed short strategy")
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
        if self.strategy.candle_period not in {"1h", "1D"}:
            errors.append("strategy.candle_period must be 1h or 1D")
        if not self.strategy.momentum_windows or any(
            item <= 0 for item in self.strategy.momentum_windows
        ):
            errors.append("strategy.momentum_windows must contain positive integers")
        if tuple(sorted(set(self.strategy.momentum_windows))) != self.strategy.momentum_windows:
            errors.append("strategy.momentum_windows must be sorted and unique")
        if self.strategy.trend_window < 2 or self.strategy.volatility_window < 2:
            errors.append("strategy trend/volatility windows must be at least 2")
        if (
            not self.strategy.volatility_floor.is_finite()
            or self.strategy.volatility_floor <= 0
        ):
            errors.append("strategy.volatility_floor must be positive and finite")
        if self.strategy.exit_rank_buffer < 0:
            errors.append("strategy.exit_rank_buffer cannot be negative")
        if not self.strategy.rebalance_hours_moscow or any(
            not 0 <= item <= 23 for item in self.strategy.rebalance_hours_moscow
        ):
            errors.append("strategy.rebalance_hours_moscow must contain hours in [0, 23]")
        if len(set(self.strategy.rebalance_hours_moscow)) != len(
            self.strategy.rebalance_hours_moscow
        ):
            errors.append("strategy.rebalance_hours_moscow cannot contain duplicates")
        if self.strategy.shorts_enabled:
            if self.strategy.short_top_n <= 0:
                errors.append("strategy.short_top_n must be positive when shorts are enabled")
            if (
                not self.strategy.max_short_momentum.is_finite()
                or self.strategy.max_short_momentum >= 0
            ):
                errors.append("strategy.max_short_momentum must be finite and negative")
            if self.max_short_position_weight <= 0 or self.max_short_position_weight > 1:
                errors.append("max_short_position_weight must be in (0, 1]")
            if self.max_short_gross_exposure <= 0 or self.max_short_gross_exposure > 1:
                errors.append("max_short_gross_exposure must be in (0, 1]")
        elif self.strategy.short_top_n != 0 or self.strategy.short_target_gross != 0:
            errors.append("short parameters require strategy.shorts_enabled=true")
        for name, value in (
            ("strategy.long_target_gross", self.strategy.long_target_gross),
            ("strategy.short_target_gross", self.strategy.short_target_gross),
        ):
            if not value.is_finite() or value < 0 or value > 1:
                errors.append(f"{name} must be in [0, 1]")
        if self.strategy.shorts_enabled and self.strategy.short_target_gross <= 0:
            errors.append("strategy.short_target_gross must be positive when shorts are enabled")
        return tuple(errors)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(isinstance(item, bool) for item in value):
        raise ValueError(f"{label} must be a JSON integer array")
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a JSON integer array") from exc


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
            candle_period=str(strategy.get("candle_period", "1h")),
            momentum_windows=_integer_tuple(
                strategy.get("momentum_windows", [5]),
                "strategy.momentum_windows",
            ),
            trend_window=int(strategy.get("trend_window", 20)),
            volatility_window=int(strategy.get("volatility_window", 20)),
            volatility_floor=_decimal(strategy.get("volatility_floor", "0.005")),
            inverse_volatility_weights=_boolean(
                strategy.get("inverse_volatility_weights", False),
                "strategy.inverse_volatility_weights",
            ),
            exit_rank_buffer=int(strategy.get("exit_rank_buffer", 0)),
            rebalance_hours_moscow=_integer_tuple(
                strategy.get("rebalance_hours_moscow", list(range(24))),
                "strategy.rebalance_hours_moscow",
            ),
            shorts_enabled=_boolean(
                strategy.get("shorts_enabled", False), "strategy.shorts_enabled"
            ),
            short_top_n=int(strategy.get("short_top_n", 0)),
            max_short_momentum=_decimal(strategy.get("max_short_momentum", "-0.01")),
            require_below_trend_for_short=_boolean(
                strategy.get("require_below_trend_for_short", True),
                "strategy.require_below_trend_for_short",
            ),
            long_target_gross=_decimal(strategy.get("long_target_gross", "1")),
            short_target_gross=_decimal(strategy.get("short_target_gross", "0")),
        ),
        min_cash_reserve_weight=_decimal(raw.get("min_cash_reserve_weight", "0.10")),
        max_sector_weight=_decimal(raw.get("max_sector_weight", "0.35")),
        max_risk_cluster_weight=_decimal(raw.get("max_risk_cluster_weight", "0.45")),
        max_short_position_weight=_decimal(raw.get("max_short_position_weight", "0")),
        max_short_gross_exposure=_decimal(raw.get("max_short_gross_exposure", "0")),
    )
    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    return config
