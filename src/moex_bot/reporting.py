from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .harness import HarnessResult


class FlowState(StrEnum):
    BUY_DOMINANT = "buy_dominant"
    BALANCED = "balanced"
    SELL_DOMINANT = "sell_dominant"


@dataclass(frozen=True, slots=True)
class EquityFlowSnapshot:
    secid: str
    window_start: datetime
    window_end: datetime
    buy_value: Decimal
    sell_value: Decimal
    intervals: int

    @property
    def imbalance(self) -> Decimal:
        total = self.buy_value + self.sell_value
        return Decimal("0") if total == 0 else (self.buy_value - self.sell_value) / total

    @property
    def state(self) -> FlowState:
        if self.imbalance > Decimal("0.10"):
            return FlowState.BUY_DOMINANT
        if self.imbalance < Decimal("-0.10"):
            return FlowState.SELL_DOMINANT
        return FlowState.BALANCED


@dataclass(frozen=True, slots=True)
class FutoiGroupSnapshot:
    group: str
    net_contracts: Decimal
    gross_long: Decimal
    gross_short: Decimal
    long_participants: int
    short_participants: int


@dataclass(frozen=True, slots=True)
class FutoiSnapshot:
    ticker: str
    observed_at: datetime
    groups: tuple[FutoiGroupSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ConcentrationSnapshot:
    secid: str
    observed_at: datetime
    metrics: Mapping[str, Decimal]


def render_shadow_report(result: HarnessResult, as_of: datetime) -> str:
    return "\n".join(
        (
            "MOEX bot — часовой shadow-отчёт",
            f"Время: {as_of.astimezone(UTC).isoformat()}",
            f"Качество данных: {'OK' if result.quality.passed else 'BLOCKED'}",
            f"Геориск: {result.geo.level.value}, множитель {result.geo.multiplier}",
            f"Целей: {len(result.targets)}; dry-run приказов: {len(result.orders)}; "
            f"отклонено: {len(result.rejected)}",
            "Режим: SHADOW — реальные заявки не отправлялись.",
        )
    )


def render_flow_report(
    equity: EquityFlowSnapshot,
    futoi: FutoiSnapshot | None,
    concentration: ConcentrationSnapshot | None,
) -> str:
    total = equity.buy_value + equity.sell_value
    buy_share = Decimal("0") if total == 0 else equity.buy_value / total * 100
    lines = [
        f"Поток {equity.secid}: {equity.state.value}",
        f"Окно: {equity.window_start.isoformat()} — {equity.window_end.isoformat()}",
        f"Агрессивные покупки: {buy_share:.1f}%; imbalance: {equity.imbalance:+.3f}",
        "Это классификация исполненных сделок, а не число открытых шортов.",
    ]
    if futoi is not None:
        lines.append(f"FUTOI {futoi.ticker} на {futoi.observed_at.isoformat()}:")
        for group in futoi.groups:
            lines.append(
                f"- {group.group}: net {group.net_contracts:+f}, "
                f"long {group.gross_long:f}, short {group.gross_short:f} контрактов"
            )
    if concentration is not None:
        values = ", ".join(f"{key}={value:f}" for key, value in concentration.metrics.items())
        lines.append(f"HI2 (анонимная концентрация): {values or 'нет доступных метрик'}")
    return "\n".join(lines)


def completed_window(as_of: datetime, minutes: int) -> tuple[datetime, datetime]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    completed_minute = as_of.minute - as_of.minute % 5
    end = as_of.replace(minute=completed_minute, second=0, microsecond=0)
    if end == as_of.replace(second=0, microsecond=0):
        end -= timedelta(minutes=5)
    return end - timedelta(minutes=minutes), end


def normalized_groups(groups: Sequence[FutoiGroupSnapshot]) -> tuple[FutoiGroupSnapshot, ...]:
    return tuple(sorted(groups, key=lambda item: item.group))
