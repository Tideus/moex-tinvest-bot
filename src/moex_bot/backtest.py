from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from .adapters import DryRunExecutionAdapter
from .config import BotConfig
from .domain import Instrument, MarketObservation, OrderRecord, PortfolioSnapshot, Position, Side
from .harness import TradingHarness
from .integrations.moexalgo_data import HistoricalCandle
from .shadow import UniverseEntry


@dataclass(frozen=True, slots=True)
class BacktestSettings:
    start_date: date
    end_date: date
    oos_start_date: date
    initial_cash: Decimal
    commission_rate: Decimal
    half_spread_bps: Decimal
    slippage_bps: Decimal
    cost_stress_multiplier: Decimal
    benchmark_secid: str
    benchmark_board: str
    survivorship_safe: bool
    dividends_included: bool
    short_financing_rate_annual: Decimal = Decimal("0")


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


@dataclass(frozen=True, slots=True)
class EquityPoint:
    trading_date: date
    strategy_equity: Decimal
    benchmark_equity: Decimal | None


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    start_date: date
    end_date: date
    sessions: int
    trades: int
    unfilled_orders: int
    start_equity: Decimal
    end_equity: Decimal
    return_pct: Decimal
    benchmark_return_pct: Decimal | None
    excess_return_pct: Decimal | None
    max_drawdown_pct: Decimal
    annualized_sharpe: Decimal
    turnover: Decimal
    commissions: Decimal
    short_financing: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    settings: BacktestSettings
    metrics: BacktestMetrics
    oos_metrics: BacktestMetrics
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    security_pnl: Mapping[str, Decimal]
    evidence_flags: Mapping[str, bool]


class _MemoryAudit:
    def write(self, event: dict[str, object]) -> None:
        del event


def load_backtest_settings(path: Path) -> BacktestSettings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("backtest configuration must be a JSON object")
    settings = BacktestSettings(
        start_date=date.fromisoformat(str(raw["start_date"])),
        end_date=date.fromisoformat(str(raw["end_date"])),
        oos_start_date=date.fromisoformat(str(raw["oos_start_date"])),
        initial_cash=Decimal(str(raw["initial_cash"])),
        commission_rate=Decimal(str(raw["commission_rate"])),
        half_spread_bps=Decimal(str(raw["half_spread_bps"])),
        slippage_bps=Decimal(str(raw["slippage_bps"])),
        cost_stress_multiplier=Decimal(str(raw["cost_stress_multiplier"])),
        benchmark_secid=str(raw.get("benchmark_secid", "IMOEX")),
        benchmark_board=str(raw.get("benchmark_board", "SNDX")),
        survivorship_safe=_bool(raw.get("survivorship_safe", False), "survivorship_safe"),
        dividends_included=_bool(raw.get("dividends_included", False), "dividends_included"),
        short_financing_rate_annual=Decimal(
            str(raw.get("short_financing_rate_annual", "0"))
        ),
    )
    if not settings.start_date < settings.end_date:
        raise ValueError("backtest start_date must be before end_date")
    if not settings.start_date <= settings.oos_start_date < settings.end_date:
        raise ValueError("oos_start_date must be inside the backtest range")
    for label, value in (
        ("initial_cash", settings.initial_cash),
        ("cost_stress_multiplier", settings.cost_stress_multiplier),
    ):
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{label} must be positive and finite")
    for label, value in (
        ("commission_rate", settings.commission_rate),
        ("half_spread_bps", settings.half_spread_bps),
        ("slippage_bps", settings.slippage_bps),
        ("short_financing_rate_annual", settings.short_financing_rate_annual),
    ):
        if not value.is_finite() or value < 0:
            raise ValueError(f"{label} must be non-negative and finite")
    return settings


def run_backtest(
    *,
    bot_config: BotConfig,
    settings: BacktestSettings,
    universe: tuple[UniverseEntry, ...],
    candles: Mapping[str, Sequence[HistoricalCandle]],
    benchmark_candles: Sequence[HistoricalCandle],
    cost_multiplier: Decimal = Decimal("1"),
) -> BacktestResult:
    if cost_multiplier <= 0:
        raise ValueError("cost multiplier must be positive")
    instruments = {
        item.secid: Instrument(
            item.secid,
            item.t_invest_uid,
            item.board,
            item.lot_size_verified,
            Decimal("0.01"),
            issuer_id=item.issuer_id,
            sector=item.sector,
            risk_cluster=item.risk_cluster,
            asset_class=item.asset_class,
            short_enabled=item.short_enabled_verified,
        )
        for item in universe
    }
    indexed = {
        secid: {item.trading_date: item for item in series}
        for secid, series in candles.items()
    }
    histories: dict[str, list[HistoricalCandle]] = {secid: [] for secid in instruments}
    dates = sorted(
        {
            item.trading_date
            for series in candles.values()
            for item in series
            if settings.start_date <= item.trading_date <= settings.end_date
        }
    )
    benchmark_by_date = {item.trading_date: item.close for item in benchmark_candles}
    cash = settings.initial_cash
    positions: dict[str, int] = {}
    pending: tuple[OrderRecord, ...] = ()
    trades: list[BacktestTrade] = []
    points: list[EquityPoint] = []
    unfilled = 0
    started = False
    benchmark_base: Decimal | None = None
    last_benchmark: Decimal | None = None
    last_closes: dict[str, Decimal] = {}
    financing_costs: list[tuple[date, Decimal]] = []
    previous_trading_date: date | None = None

    for trading_date in dates:
        if previous_trading_date is not None:
            elapsed_days = (trading_date - previous_trading_date).days
            financing = sum(
                (
                    abs(Decimal(lots * instruments[secid].lot_size))
                    * last_closes[secid]
                    * settings.short_financing_rate_annual
                    * cost_multiplier
                    * Decimal(elapsed_days)
                    / Decimal("365")
                    for secid, lots in positions.items()
                    if lots < 0 and secid in last_closes
                ),
                Decimal("0"),
            )
            cash -= financing
            financing_costs.append((trading_date, financing))
        day_rows = {
            secid: rows[trading_date]
            for secid, rows in indexed.items()
            if trading_date in rows
        }
        for secid, row in day_rows.items():
            histories[secid].append(row)
        cash, positions, filled, missed = _execute_pending(
            pending,
            day_rows,
            cash,
            positions,
            bot_config,
            settings,
            cost_multiplier,
        )
        trades.extend(filled)
        unfilled += missed
        last_closes.update({secid: row.close for secid, row in day_rows.items()})
        market = _market_for_date(histories, instruments, bot_config, trading_date)
        if market:
            started = True
        if started:
            strategy_equity = cash + sum(
                Decimal(lots * instruments[secid].lot_size) * last_closes[secid]
                for secid, lots in positions.items()
                if secid in last_closes
            )
            if trading_date in benchmark_by_date:
                last_benchmark = benchmark_by_date[trading_date]
                benchmark_base = benchmark_base or last_benchmark
            benchmark_equity = (
                None
                if benchmark_base is None or last_benchmark is None
                else settings.initial_cash * last_benchmark / benchmark_base
            )
            points.append(EquityPoint(trading_date, strategy_equity, benchmark_equity))
        if not market:
            pending = ()
            continue
        portfolio_positions = {
            secid: Position(instruments[secid], lots)
            for secid, lots in positions.items()
            if lots != 0
        }
        portfolio = PortfolioSnapshot(cash, portfolio_positions, source="backtest")
        as_of = datetime.combine(trading_date, datetime.min.time(), tzinfo=UTC)
        harness_config = replace(
            bot_config,
            max_data_age_seconds=max(bot_config.max_data_age_seconds, 86400),
        )
        result = TradingHarness(
            harness_config,
            DryRunExecutionAdapter(harness_config.mode),
            _MemoryAudit(),
        ).run(
            run_id=f"backtest-{trading_date.isoformat()}",
            as_of=as_of,
            market=market,
            portfolio=portfolio,
            geo_events=(),
        )
        pending = result.orders
        previous_trading_date = trading_date

    if len(points) < 2:
        raise ValueError("backtest produced fewer than two equity points")
    metrics = _metrics(
        tuple(points), tuple(trades), unfilled, tuple(financing_costs)
    )
    oos_points = tuple(item for item in points if item.trading_date >= settings.oos_start_date)
    if len(oos_points) < 2:
        raise ValueError("backtest produced fewer than two OOS equity points")
    oos_trades = tuple(item for item in trades if item.trading_date >= settings.oos_start_date)
    oos_metrics = _metrics(
        oos_points,
        oos_trades,
        0,
        tuple(item for item in financing_costs if item[0] >= settings.oos_start_date),
    )
    security_pnl = _security_pnl(trades, positions, last_closes, instruments)
    return BacktestResult(
        settings=settings,
        metrics=metrics,
        oos_metrics=oos_metrics,
        trades=tuple(trades),
        equity_curve=tuple(points),
        security_pnl=security_pnl,
        evidence_flags={
            "survivorship_safe": settings.survivorship_safe,
            "dividends_included": settings.dividends_included,
            "next_session_execution": True,
            "spread_slippage_commission_included": True,
        },
    )


def _market_for_date(
    histories: Mapping[str, Sequence[HistoricalCandle]],
    instruments: Mapping[str, Instrument],
    config: BotConfig,
    trading_date: date,
) -> dict[str, MarketObservation]:
    strategy = config.strategy
    required = max(
        strategy.trend_window,
        strategy.volatility_window + 1,
        max(strategy.momentum_windows) + 1,
    )
    market: dict[str, MarketObservation] = {}
    for secid, rows in histories.items():
        if len(rows) < required:
            continue
        closes = [item.close for item in rows]
        price = closes[-1]
        momentum = sum(
            (price / closes[-(window + 1)] - 1 for window in strategy.momentum_windows),
            Decimal("0"),
        ) / Decimal(len(strategy.momentum_windows))
        trend = sum(closes[-strategy.trend_window :], Decimal("0")) / Decimal(
            strategy.trend_window
        )
        volatility_closes = closes[-(strategy.volatility_window + 1) :]
        returns = [
            volatility_closes[index] / volatility_closes[index - 1] - 1
            for index in range(1, len(volatility_closes))
        ]
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum(((item - mean) ** 2 for item in returns), Decimal("0")) / Decimal(
            len(returns)
        )
        market[secid] = MarketObservation(
            instruments[secid],
            price,
            trend,
            momentum,
            variance.sqrt(),
            datetime.combine(rows[-1].trading_date, datetime.min.time(), tzinfo=UTC),
            True,
            True,
        )
    return market


def _execute_pending(
    pending: tuple[OrderRecord, ...],
    rows: Mapping[str, HistoricalCandle],
    cash: Decimal,
    positions: Mapping[str, int],
    config: BotConfig,
    settings: BacktestSettings,
    cost_multiplier: Decimal,
) -> tuple[Decimal, dict[str, int], tuple[BacktestTrade, ...], int]:
    next_positions = dict(positions)
    result: list[BacktestTrade] = []
    missed = 0
    rate = (
        settings.half_spread_bps + settings.slippage_bps
    ) / Decimal("10000") * cost_multiplier
    commission_rate = settings.commission_rate * cost_multiplier
    for record in pending:
        intent = record.intent
        candle = rows.get(intent.instrument.secid)
        if candle is None:
            missed += 1
            continue
        if intent.side is Side.BUY:
            if candle.open <= intent.limit_price:
                reference = candle.open
            elif candle.low <= intent.limit_price:
                reference = intent.limit_price
            else:
                missed += 1
                continue
            fill_price = reference * (Decimal("1") + rate)
        else:
            if candle.open >= intent.limit_price:
                reference = candle.open
            elif candle.high >= intent.limit_price:
                reference = intent.limit_price
            else:
                missed += 1
                continue
            fill_price = reference * (Decimal("1") - rate)
        units = intent.lots * intent.instrument.lot_size
        gross = fill_price * units
        commission = gross * commission_rate
        current_lots = next_positions.get(intent.instrument.secid, 0)
        if intent.side is Side.BUY:
            required = gross + commission
            resulting_lots = current_lots + intent.lots
            reducing_short = current_lots < 0 and abs(resulting_lots) < abs(current_lots)
            reserve = (
                Decimal("0")
                if reducing_short
                else settings.initial_cash * config.min_cash_reserve_weight
            )
            if required > max(Decimal("0"), cash - reserve):
                missed += 1
                continue
            cash -= required
            if resulting_lots:
                next_positions[intent.instrument.secid] = resulting_lots
            else:
                next_positions.pop(intent.instrument.secid, None)
        else:
            cash += gross - commission
            remaining = current_lots - intent.lots
            if remaining:
                next_positions[intent.instrument.secid] = remaining
            else:
                next_positions.pop(intent.instrument.secid, None)
        result.append(
            BacktestTrade(
                candle.trading_date,
                intent.instrument.secid,
                intent.side.value,
                intent.lots,
                units,
                intent.limit_price,
                fill_price,
                gross,
                commission,
            )
        )
    return cash, next_positions, tuple(result), missed


def _metrics(
    points: Sequence[EquityPoint],
    trades: Sequence[BacktestTrade],
    unfilled: int,
    financing_costs: Sequence[tuple[date, Decimal]],
) -> BacktestMetrics:
    start, end = points[0], points[-1]
    strategy_return = (end.strategy_equity / start.strategy_equity - 1) * 100
    benchmark_return = (
        None
        if start.benchmark_equity is None or end.benchmark_equity is None
        else (end.benchmark_equity / start.benchmark_equity - 1) * 100
    )
    returns = [
        points[index].strategy_equity / points[index - 1].strategy_equity - 1
        for index in range(1, len(points))
    ]
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), Decimal("0")) / Decimal(
        len(returns)
    )
    volatility = variance.sqrt()
    sharpe = Decimal("0") if volatility == 0 else mean / volatility * Decimal(252).sqrt()
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for point in points:
        peak = max(peak, point.strategy_equity)
        if peak > 0:
            max_drawdown = max(
                max_drawdown, (peak - point.strategy_equity) / peak * 100
            )
    turnover = sum((item.gross for item in trades), Decimal("0"))
    commissions = sum((item.commission for item in trades), Decimal("0"))
    short_financing = sum((item[1] for item in financing_costs), Decimal("0"))
    return BacktestMetrics(
        start.trading_date,
        end.trading_date,
        len(points),
        len(trades),
        unfilled,
        start.strategy_equity,
        end.strategy_equity,
        strategy_return,
        benchmark_return,
        None if benchmark_return is None else strategy_return - benchmark_return,
        max_drawdown,
        sharpe,
        turnover,
        commissions,
        short_financing,
    )


def _security_pnl(
    trades: Sequence[BacktestTrade],
    positions: Mapping[str, int],
    last_closes: Mapping[str, Decimal],
    instruments: Mapping[str, Instrument],
) -> dict[str, Decimal]:
    pnl: dict[str, Decimal] = {}
    for trade in trades:
        direction = Decimal("-1") if trade.side == "buy" else Decimal("1")
        pnl[trade.secid] = pnl.get(trade.secid, Decimal("0")) + direction * trade.gross
        pnl[trade.secid] -= trade.commission
    for secid, lots in positions.items():
        if secid in last_closes:
            pnl[secid] = pnl.get(secid, Decimal("0")) + Decimal(
                lots * instruments[secid].lot_size
            ) * last_closes[secid]
    return dict(sorted(pnl.items()))


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def serialize_backtest(result: BacktestResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, default=str, indent=2) + "\n",
        encoding="utf-8",
    )
