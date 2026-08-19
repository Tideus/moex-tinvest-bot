from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum

ZERO = Decimal("0")
ONE = Decimal("1")


class ExecutionMode(StrEnum):
    REPLAY = "replay"
    BACKTEST = "backtest"
    SANDBOX = "sandbox"
    SHADOW = "shadow"
    LIVE = "live"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class GeoRiskLevel(StrEnum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"
    RECOVERY = "recovery"


class OrderStatus(StrEnum):
    PLANNED = "planned"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Instrument:
    secid: str
    uid: str
    board: str
    lot_size: int
    tick_size: Decimal
    currency: str = "RUB"
    issuer_id: str = "unknown"
    sector: str = "unknown"
    risk_cluster: str = "unknown"
    asset_class: str = "share"
    short_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.secid or not self.uid or not self.board:
            raise ValueError("instrument identity must be complete")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.tick_size <= ZERO:
            raise ValueError("tick_size must be positive")
        if not self.issuer_id or not self.sector or not self.risk_cluster:
            raise ValueError("instrument diversification identity must be complete")

    def round_price(self, price: Decimal) -> Decimal:
        ticks = (price / self.tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return ticks * self.tick_size

    def floor_lots(self, units: Decimal) -> int:
        return int((units / Decimal(self.lot_size)).quantize(Decimal("1"), rounding=ROUND_DOWN))


@dataclass(frozen=True, slots=True)
class MarketObservation:
    instrument: Instrument
    price: Decimal
    trend: Decimal
    momentum: Decimal
    volatility: Decimal
    observed_at: datetime
    complete: bool
    tradable: bool

    def __post_init__(self) -> None:
        if self.price <= ZERO or self.trend <= ZERO:
            raise ValueError("prices must be positive")
        if self.volatility < ZERO:
            raise ValueError("volatility cannot be negative")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Position:
    instrument: Instrument
    lots: int
    blocked_lots: int = 0

    def __post_init__(self) -> None:
        if self.blocked_lots < 0:
            raise ValueError("blocked_lots cannot be negative")
        if self.blocked_lots > abs(self.lots):
            raise ValueError("blocked_lots cannot exceed absolute position lots")

    @property
    def units(self) -> int:
        return self.lots * self.instrument.lot_size


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    cash: Decimal
    positions: Mapping[str, Position] = field(default_factory=dict)
    daily_turnover: Decimal = ZERO
    open_orders: int = 0
    blocked_cash: Decimal = ZERO
    reported_equity: Decimal | None = None
    source: str = "file"

    def __post_init__(self) -> None:
        if not self.cash.is_finite() or not self.blocked_cash.is_finite():
            raise ValueError("portfolio cash values must be finite")
        if self.cash < ZERO or self.blocked_cash < ZERO:
            raise ValueError("portfolio cash values cannot be negative")
        if self.open_orders < 0:
            raise ValueError("open_orders cannot be negative")
        if self.reported_equity is not None and not self.reported_equity.is_finite():
            raise ValueError("reported_equity must be finite")

    def equity(self, market: Mapping[str, MarketObservation]) -> Decimal:
        total = self.cash + self.blocked_cash
        for secid, position in self.positions.items():
            observation = market.get(secid)
            if observation is None:
                raise ValueError(f"missing price for position {secid}")
            total += Decimal(position.units) * observation.price
        if self.reported_equity is not None and self.reported_equity > ZERO:
            return min(total, self.reported_equity)
        return total


@dataclass(frozen=True, slots=True)
class GeoEvent:
    event_id: str
    severity: int
    confidence: Decimal
    source_tier: str
    confirmed: bool
    affected_secids: frozenset[str]
    observed_at: datetime

    def __post_init__(self) -> None:
        if not 0 <= self.severity <= 5:
            raise ValueError("severity must be in [0, 5]")
        if not ZERO <= self.confidence <= ONE:
            raise ValueError("confidence must be in [0, 1]")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class GeoRiskSnapshot:
    level: GeoRiskLevel
    multiplier: Decimal
    blocked_secids: frozenset[str]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Target:
    secid: str
    weight: Decimal
    rationale: str


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_request_id: str
    instrument: Instrument
    side: Side
    lots: int
    limit_price: Decimal
    notional: Decimal
    rationale: str
    confirm_margin_trade: bool = False
    order_type: str = "limit"

    def __post_init__(self) -> None:
        if self.lots <= 0:
            raise ValueError("lots must be positive")
        if self.limit_price <= ZERO or self.notional <= ZERO:
            raise ValueError("price and notional must be positive")
        if self.order_type not in {"limit", "market"}:
            raise ValueError("order_type must be limit or market")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    intent: OrderIntent | None


@dataclass(frozen=True, slots=True)
class OrderRecord:
    intent: OrderIntent
    status: OrderStatus
    filled_lots: int = 0
    broker_order_id: str | None = None
