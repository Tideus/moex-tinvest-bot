from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .domain import Instrument, OrderIntent, Side
from .integrations.algopack_intraday import IntradaySuperCandle
from .intraday_config import IntradayConfig

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class IntradaySignal:
    secid: str
    side: Side
    observed_at: datetime
    price: Decimal
    price_move: Decimal
    trade_imbalance: Decimal
    order_flow: Decimal
    book_imbalance: Decimal
    spread_bbo: Decimal

    @property
    def score(self) -> Decimal:
        return abs(self.price_move) * abs(self.trade_imbalance)


@dataclass(frozen=True, slots=True)
class IntradayPlan:
    run_id: str
    quality_passed: bool
    phase: str
    signals: tuple[IntradaySignal, ...]
    orders: tuple[OrderIntent, ...]
    notes: tuple[str, ...]

    def write(
        self, path: Path, *, analysis_input: dict[str, object] | None = None
    ) -> None:
        payload = {
            "run_id": self.run_id,
            "quality": {"passed": self.quality_passed},
            "phase": self.phase,
            "signals": [asdict(item) for item in self.signals],
            "orders": [
                {"status": "validated", "intent": asdict(item)} for item in self.orders
            ],
            "notes": list(self.notes),
            "analysis_input": analysis_input or {},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )


class IntradayStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS intraday_bars (
                    secid TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(secid, observed_at)
                );
                CREATE TABLE IF NOT EXISTS intraday_signals (
                    trade_day TEXT NOT NULL,
                    secid TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    side TEXT NOT NULL,
                    PRIMARY KEY(secid, observed_at, side)
                );
                CREATE TABLE IF NOT EXISTS intraday_sessions (
                    trade_day TEXT PRIMARY KEY,
                    opening_equity TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def add(self, bars: tuple[IntradaySuperCandle, ...]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO intraday_bars(secid, observed_at, payload) VALUES(?,?,?)",
                (
                    (
                        bar.secid,
                        bar.observed_at.isoformat(),
                        json.dumps(asdict(bar), default=str, ensure_ascii=False),
                    )
                    for bar in bars
                ),
            )

    def recent(self, secid: str, limit: int) -> tuple[IntradaySuperCandle, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM intraday_bars WHERE secid=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (secid, limit),
            ).fetchall()
        return tuple(reversed(tuple(_bar(json.loads(row[0])) for row in rows)))

    def mark_signal(self, signal: IntradaySignal) -> bool:
        local_day = signal.observed_at.astimezone(MOSCOW).date().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO intraday_signals"
                "(trade_day,secid,observed_at,side) VALUES(?,?,?,?)",
                (local_day, signal.secid, signal.observed_at.isoformat(), signal.side.value),
            )
        return cursor.rowcount == 1

    def entries(self, trade_day: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM intraday_signals WHERE trade_day=?", (trade_day,)
            ).fetchone()
        return int(row[0])

    def opening_equity(self, trade_day: str, current: Decimal) -> Decimal:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO intraday_sessions(trade_day,opening_equity) VALUES(?,?)",
                (trade_day, str(current)),
            )
            row = conn.execute(
                "SELECT opening_equity FROM intraday_sessions WHERE trade_day=?", (trade_day,)
            ).fetchone()
        return Decimal(str(row[0]))


def build_intraday_plan(
    *,
    config: IntradayConfig,
    instruments: dict[str, Instrument],
    portfolio: dict[str, object],
    store: IntradayStateStore,
    bars: tuple[IntradaySuperCandle, ...],
    as_of: datetime,
) -> IntradayPlan:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    local = as_of.astimezone(MOSCOW)
    run_id = f"intraday-{as_of.isoformat()}"
    store.add(bars)
    positions = _positions(portfolio)
    equity = Decimal(str(portfolio.get("reported_equity", "0")))
    cash = Decimal(str(portfolio.get("cash", "0")))
    daily_turnover = Decimal(str(portfolio.get("daily_turnover", "0")))
    if equity <= 0 or cash < 0:
        raise ValueError("intraday broker equity/cash must be valid")
    trade_day = local.date().isoformat()
    opening = store.opening_equity(trade_day, equity)
    loss_gate = equity <= opening * (Decimal("1") - config.max_daily_loss_weight)
    clock = local.time().replace(tzinfo=None)
    if clock >= config.force_flat_moscow:
        phase = "force_flat"
    elif loss_gate:
        phase = "loss_limit_flat"
    elif config.new_entries_start_moscow <= clock < config.new_entries_stop_moscow:
        phase = "entries"
    else:
        phase = "monitor"

    latest_prices = {bar.secid: bar.close for bar in bars}
    if phase in {"force_flat", "loss_limit_flat"}:
        missing_prices = sorted(set(positions) - set(latest_prices))
        if missing_prices:
            raise ValueError(
                "cannot flatten positions without fresh prices: " + ", ".join(missing_prices)
            )
        flat_orders = _flatten_orders(instruments, positions, latest_prices, as_of)
        notes = ("forced flat before overnight" if phase == "force_flat" else "daily loss gate",)
        return IntradayPlan(run_id, bool(bars), phase, (), flat_orders, notes)
    if phase != "entries":
        return IntradayPlan(run_id, bool(bars), phase, (), (), ("new entries disabled",))
    if _integer_value(portfolio.get("open_orders", 0), "open_orders") != 0:
        return IntradayPlan(run_id, False, phase, (), (), ("active orders not reconciled",))
    missing_position_prices = sorted(set(positions) - set(latest_prices))
    if missing_position_prices:
        raise ValueError(
            "cannot size intraday exposure without fresh prices: "
            + ", ".join(missing_position_prices)
        )

    signals = tuple(
        sorted(
            filter(
                None,
                (
                    _signal(config, secid, store.recent(secid, config.history_bars), as_of)
                    for secid in instruments
                ),
            ),
            key=lambda item: (-item.score, item.secid),
        )
    )
    entry_slots = max(0, config.max_entries_per_day - store.entries(trade_day))
    position_slots = max(0, config.max_concurrent_positions - len(positions))
    turnover_left = max(Decimal("0"), config.max_daily_turnover_rub - daily_turnover)
    current_gross = sum(
        (
            abs(lots)
            * instruments[secid].lot_size
            * latest_prices[secid]
            for secid, lots in positions.items()
        ),
        Decimal("0"),
    )
    capital_left = min(
        cash,
        max(Decimal("0"), equity * config.max_capital_weight - current_gross),
        turnover_left,
    )
    entry_orders: list[OrderIntent] = []
    for signal in signals:
        if not entry_slots or not position_slots or signal.secid in positions:
            continue
        instrument = instruments[signal.secid]
        if signal.side is Side.SELL and (not config.allow_short or not instrument.short_enabled):
            continue
        budget = min(config.max_position_notional_rub, capital_left)
        intent = _entry_order(instrument, signal, budget)
        if intent is None or not store.mark_signal(signal):
            continue
        entry_orders.append(intent)
        entry_slots -= 1
        position_slots -= 1
        capital_left -= intent.notional
    return IntradayPlan(
        run_id,
        bool(bars),
        phase,
        signals,
        tuple(entry_orders),
        ("completed 5-minute TradeStats/OrderStats/OBStats",),
    )


def render_intraday_report(plan: IntradayPlan, *, account_label: str = "intraday") -> str:
    lines = [
        "⚡ MOEX BOT · INTRADAY SANDBOX",
        f"Счёт: {account_label}",
        f"Фаза: {plan.phase}",
        f"Сигналов: {len(plan.signals)} · заявок: {len(plan.orders)}",
    ]
    for signal in plan.signals[:5]:
        lines.append(
            f"• {signal.secid} {signal.side.value.upper()}: "
            f"цена {signal.price_move:+.2%}, сделки {signal.trade_imbalance:+.2%}, "
            f"заявки {signal.order_flow:+.2%}, стакан {signal.book_imbalance:+.2%}"
        )
    for order in plan.orders:
        action = "BUY" if order.side is Side.BUY else "SELL/SHORT"
        lines.append(
            f"🧾 {action} {order.instrument.secid}: {order.lots} лот. · {order.notional:.2f} ₽"
        )
    if not plan.orders:
        lines.append("Заявок нет: условия входа или risk-gate не пройдены.")
    lines.append("Только Sandbox; production-заявки запрещены.")
    return "\n".join(lines)


def _signal(
    config: IntradayConfig,
    secid: str,
    bars: tuple[IntradaySuperCandle, ...],
    as_of: datetime,
) -> IntradaySignal | None:
    if len(bars) != config.history_bars or any(bar.close <= 0 for bar in bars):
        return None
    if any(
        right.observed_at - left.observed_at != timedelta(minutes=config.candle_minutes)
        for left, right in zip(bars, bars[1:], strict=False)
    ):
        return None
    latest = bars[-1]
    if latest.observed_at > as_of or as_of - latest.observed_at > timedelta(minutes=15):
        return None
    move = latest.close / bars[0].close - Decimal("1")
    buy = sum((bar.buy_value for bar in bars), Decimal("0"))
    sell = sum((bar.sell_value for bar in bars), Decimal("0"))
    trade_imbalance = _ratio(buy - sell, buy + sell)
    put_b = sum((bar.put_buy_value for bar in bars), Decimal("0"))
    put_s = sum((bar.put_sell_value for bar in bars), Decimal("0"))
    cancel_b = sum((bar.cancel_buy_value for bar in bars), Decimal("0"))
    cancel_s = sum((bar.cancel_sell_value for bar in bars), Decimal("0"))
    order_flow = _ratio(
        (put_b - cancel_b) - (put_s - cancel_s),
        put_b + put_s + cancel_b + cancel_s,
    )
    direction = Decimal("1") if move > 0 else Decimal("-1")
    if (
        abs(move) < config.min_price_move
        or direction * trade_imbalance < config.min_abs_trade_imbalance
        or direction * order_flow < config.min_abs_order_flow
        or direction * latest.book_imbalance < config.min_abs_book_imbalance
        or latest.spread_bbo > config.max_spread_bbo
    ):
        return None
    return IntradaySignal(
        secid,
        Side.BUY if direction > 0 else Side.SELL,
        latest.observed_at,
        latest.close,
        move,
        trade_imbalance,
        order_flow,
        latest.book_imbalance,
        latest.spread_bbo,
    )


def _entry_order(
    instrument: Instrument, signal: IntradaySignal, budget: Decimal
) -> OrderIntent | None:
    price = _tick_price(signal.side, signal, instrument.tick_size)
    lot_value = price * instrument.lot_size
    lots = int(budget // lot_value)
    if lots <= 0:
        return None
    notional = lot_value * lots
    request_id = str(
        uuid5(
            NAMESPACE_URL,
            f"intraday:{signal.observed_at.isoformat()}:{instrument.uid}:{signal.side.value}",
        )
    )
    return OrderIntent(
        request_id,
        instrument,
        signal.side,
        lots,
        price,
        notional,
        (
            f"intraday momentum;move={signal.price_move};trade={signal.trade_imbalance};"
            f"order={signal.order_flow};book={signal.book_imbalance}"
        ),
        confirm_margin_trade=signal.side is Side.SELL,
    )


def _flatten_orders(
    instruments: dict[str, Instrument],
    positions: dict[str, int],
    prices: dict[str, Decimal],
    as_of: datetime,
) -> tuple[OrderIntent, ...]:
    orders: list[OrderIntent] = []
    for secid, lots in sorted(positions.items()):
        if not lots or secid not in prices or secid not in instruments:
            continue
        instrument = instruments[secid]
        side = Side.SELL if lots > 0 else Side.BUY
        price = _rounded(prices[secid], instrument.tick_size)
        quantity = abs(lots)
        orders.append(
            OrderIntent(
                str(uuid5(NAMESPACE_URL, f"intraday-flat:{as_of.date()}:{instrument.uid}")),
                instrument,
                side,
                quantity,
                price,
                price * instrument.lot_size * quantity,
                "intraday mandatory flat",
                confirm_margin_trade=False,
                order_type="market",
            )
        )
    return tuple(orders)


def _tick_price(side: Side, signal: IntradaySignal, tick: Decimal) -> Decimal:
    del side
    return _rounded(signal.price, tick)


def _rounded(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def _positions(portfolio: dict[str, object]) -> dict[str, int]:
    raw = portfolio.get("positions", {})
    if not isinstance(raw, dict):
        raise ValueError("portfolio positions must be an object")
    result: dict[str, int] = {}
    for secid, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("portfolio position must be an object")
        lots = int(value.get("lots", 0))
        if lots:
            result[str(secid)] = lots
    return result


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return Decimal("0") if denominator == 0 else numerator / denominator


def _integer_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def _bar(raw: dict[str, object]) -> IntradaySuperCandle:
    return IntradaySuperCandle(
        secid=str(raw["secid"]),
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        close=Decimal(str(raw["close"])),
        vwap=Decimal(str(raw["vwap"])),
        buy_value=Decimal(str(raw["buy_value"])),
        sell_value=Decimal(str(raw["sell_value"])),
        put_buy_value=Decimal(str(raw["put_buy_value"])),
        put_sell_value=Decimal(str(raw["put_sell_value"])),
        cancel_buy_value=Decimal(str(raw["cancel_buy_value"])),
        cancel_sell_value=Decimal(str(raw["cancel_sell_value"])),
        spread_bbo=Decimal(str(raw["spread_bbo"])),
        book_imbalance=Decimal(str(raw["book_imbalance"])),
    )
