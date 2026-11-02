"""Deterministic DAY-limit simulation. No broker transport or live execution code."""

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from .domain import OrderRecord, OrderStatus, Side
from .integrations.moexalgo_data import HistoricalCandle


@dataclass(frozen=True, slots=True)
class Window:
    # Half-open, in seconds from the start of the trading session, not midnight.
    start: int
    end: int
    candle: HistoricalCandle

    def __post_init__(self) -> None:
        c = self.candle
        if not all(v.is_finite() for v in (c.open, c.high, c.low, c.close, c.volume)):
            raise ValueError("execution window contains non-finite price or volume")
        if not (
            type(self.start) is int
            and type(self.end) is int
            and 0 <= self.start < self.end <= 86400
        ):
            raise ValueError("invalid execution window offsets")


WindowBook = dict[date, dict[str, list[Window]]]


def load_execution_windows(path: Path) -> WindowBook:
    book: WindowBook = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            r = json.loads(line)
            if not isinstance(r, dict) or not {
                "trading_date",
                "secid",
                "start_seconds",
                "end_seconds",
                "open",
                "high",
                "low",
                "close",
                "volume",
            }.issubset(r):
                raise ValueError("invalid execution window JSONL row")
            if not isinstance(r["secid"], str) or not r["secid"].strip():
                raise ValueError("execution window needs a security identifier")
            try:
                day = date.fromisoformat(r["trading_date"])
                bar = HistoricalCandle(
                    day, *(Decimal(str(r[k])) for k in ("open", "high", "low", "close", "volume"))
                )
            except (TypeError, InvalidOperation) as exc:
                raise ValueError("invalid execution window date or OHLCV") from exc
            book.setdefault(day, {}).setdefault(r["secid"], []).append(
                Window(r["start_seconds"], r["end_seconds"], bar)
            )
    if not book:
        raise ValueError("empty execution window file")
    return book


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    trading_date: date
    secid: str
    side: str
    lots: int
    units: int
    signal_limit: Decimal
    fill_price: Decimal
    gross: Decimal
    commission: Decimal
    order_request_id: str
    window_start_seconds: int
    window_end_seconds: int


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    trading_date: date
    order_request_id: str
    secid: str
    requested_lots: int
    filled_lots: int
    expired_lots: int
    status: str
    last_reason: str


def match_session(
    day: date,
    orders: Sequence[OrderRecord],
    windows: Mapping[str, Sequence[Window]],
    cash: Decimal,
    positions: Mapping[str, int],
    *,
    reserve: Decimal,
    impact: Decimal,
    commission: Decimal,
    participation: Decimal,
    latency: int,
) -> tuple[Decimal, dict[str, int], tuple[BacktestTrade, ...], tuple[OrderOutcome, ...]]:
    """Run at window completion. Strict trade-through and shared participation budget.

    Equal-end windows use original plan priority. Unfilled DAY remainders expire.
    No interpolation of intrabar timestamps or queue position.
    """
    held = dict(positions)
    fills = []
    done = [0] * len(orders)
    reasons = ["missing_window"] * len(orders)
    by_symbol: dict[str, list[int]] = defaultdict(list)
    for i, rec in enumerate(orders):
        if rec.status != OrderStatus.VALIDATED or rec.intent.order_type != "limit":
            raise ValueError("only validated limit orders can enter the historical matcher")
        by_symbol[rec.intent.instrument.secid].append(i)

    events: list[tuple[int, int, Window]] = []
    for symbol, indices in by_symbol.items():
        previous_end = -1
        for w in windows.get(symbol, ()):
            if w.candle.trading_date != day or w.start < previous_end:
                raise ValueError("execution windows must be same-session, ordered and disjoint")
            previous_end = w.end
            events.extend((w.end, i, w) for i in indices)
    # Budget in units, not lots, shared between buys and sells of the same symbol.
    consumed: dict[tuple[str, int], int] = defaultdict(int)
    for _, i, w in sorted(events, key=lambda e: (e[0], e[1])):
        order = orders[i].intent
        if done[i] == order.lots:
            continue
        if w.start < latency:
            reasons[i] = "not_active_for_full_window"
            continue
        inst, limit, c = order.instrument, order.limit_price, w.candle
        buy = order.side == Side.BUY
        factor = 1 + impact if buy else 1 - impact
        # Executable proxy must be strictly through limit, not merely touching.
        if (buy and c.low * factor >= limit) or (not buy and c.high * factor <= limit):
            reasons[i] = "no_trade_through"
            continue
        # Opening improvement only when available; a subsequent cross pays the limit.
        price = min(c.high, limit, c.open * factor) if buy else max(c.low, limit, c.open * factor)
        tick = inst.tick_size
        price = (price / tick).to_integral_value(
            rounding=ROUND_CEILING if buy else ROUND_FLOOR
        ) * tick
        if not c.low <= price <= c.high or (buy and price > limit) or (not buy and price < limit):
            reasons[i] = "no_executable_tick"
            continue
        key = (inst.secid, w.start)
        capacity = int(c.volume * participation) - consumed[key]
        lots = min(order.lots - done[i], capacity // inst.lot_size)
        if lots <= 0:
            reasons[i] = "volume_cap"
            continue
        current = held.get(inst.secid, 0)
        if buy:
            cost = price * inst.lot_size * (1 + commission)
            affordable = int(max(Decimal(0), cash - reserve) // cost)
            cover = min(max(0, -current), int(max(Decimal(0), cash) // cost))
            lots = min(lots, max(affordable, cover))
            if not lots:
                reasons[i] = "cash_or_reserve"
                continue
        elif not order.confirm_margin_trade:
            lots = min(lots, max(0, current))
            if not lots:
                reasons[i] = "no_unreserved_position"
                continue
        units = lots * inst.lot_size
        gross = units * price
        fee = gross * commission
        cash += (-gross if buy else gross) - fee
        held[inst.secid] = current + (lots if buy else -lots)
        if held[inst.secid] == 0:
            del held[inst.secid]
        consumed[key] += units
        done[i] += lots
        reasons[i] = "filled" if done[i] == order.lots else "capacity_or_cash_partial"
        fills.append(
            BacktestTrade(
                day,
                inst.secid,
                order.side.value,
                lots,
                units,
                limit,
                price,
                gross,
                fee,
                order.order_request_id,
                w.start,
                w.end,
            )
        )
    outcomes = tuple(
        OrderOutcome(
            day,
            r.intent.order_request_id,
            r.intent.instrument.secid,
            r.intent.lots,
            n,
            r.intent.lots - n,
            "filled" if n == r.intent.lots else "partial_expired" if n else "expired",
            reason,
        )
        for r, n, reason in zip(orders, done, reasons, strict=True)
    )
    return cash, held, tuple(fills), outcomes
