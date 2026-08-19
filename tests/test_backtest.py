from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from moex_bot.backtest import BacktestSettings, run_backtest
from moex_bot.backtest_reporting import (
    OperationalEvidence,
    PromotionGates,
    assess_promotion,
    write_backtest_bundle,
)
from moex_bot.config import BotConfig, StrategyConfig
from moex_bot.domain import ExecutionMode
from moex_bot.integrations.moexalgo_data import HistoricalCandle
from moex_bot.shadow import UniverseEntry


def _series(start: date, days: int, *, step: str) -> tuple[HistoricalCandle, ...]:
    result = []
    price = Decimal("100")
    for offset in range(days):
        current = start + timedelta(days=offset)
        if current.weekday() >= 5:
            continue
        close = price + Decimal(step)
        result.append(
            HistoricalCandle(
                current,
                price,
                max(price, close) + 1,
                min(price, close) - 1,
                close,
                Decimal("100000"),
            )
        )
        price = close
    return tuple(result)


def _bot_config() -> BotConfig:
    return BotConfig(
        ExecutionMode.SHADOW,
        "RUB",
        345600,
        Decimal("0.50"),
        Decimal("10000"),
        Decimal("0.80"),
        Decimal("30000"),
        Decimal("100"),
        5,
        False,
        False,
        StrategyConfig(
            1,
            Decimal("0"),
            True,
            candle_period="1D",
            momentum_windows=(2, 3),
            trend_window=4,
            volatility_window=3,
            inverse_volatility_weights=True,
        ),
        min_cash_reserve_weight=Decimal("0.10"),
    )


def test_backtest_uses_next_session_costs_and_writes_comparison_bundle(tmp_path: Path) -> None:
    start = date(2026, 1, 1)
    settings = BacktestSettings(
        start,
        start + timedelta(days=120),
        start + timedelta(days=60),
        Decimal("300000"),
        Decimal("0.0005"),
        Decimal("5"),
        Decimal("5"),
        Decimal("2"),
        "IMOEX",
        "SNDX",
        False,
        False,
    )
    universe = (
        UniverseEntry(
            "AAA",
            "TQBR",
            "00000000-0000-0000-0000-000000000001",
            1,
            True,
            "issuer-a",
            "sector-a",
            "cluster-a",
            "share",
        ),
        UniverseEntry(
            "BBB",
            "TQBR",
            "00000000-0000-0000-0000-000000000002",
            1,
            True,
            "issuer-b",
            "sector-b",
            "cluster-b",
            "share",
        ),
    )
    candles = {
        "AAA": _series(start, 121, step="1"),
        "BBB": _series(start, 121, step="0.1"),
    }
    benchmark = _series(start, 121, step="0.2")
    base = run_backtest(
        bot_config=_bot_config(),
        settings=settings,
        universe=universe,
        candles=candles,
        benchmark_candles=benchmark,
    )
    stress = run_backtest(
        bot_config=_bot_config(),
        settings=settings,
        universe=universe,
        candles=candles,
        benchmark_candles=benchmark,
        cost_multiplier=Decimal("2"),
    )
    assert base.metrics.trades > 0
    assert base.metrics.commissions > 0
    assert base.equity_curve[0].trading_date > start
    gates = PromotionGates(
        20,
        1,
        Decimal("0"),
        Decimal("0"),
        Decimal("50"),
        True,
        True,
        True,
        8,
        50,
        0,
    )
    assessment = assess_promotion(base, stress, gates, OperationalEvidence())
    assert not assessment.passed
    write_backtest_bundle(
        output_dir=tmp_path,
        base=base,
        stress=stress,
        assessment=assessment,
    )
    assert (tmp_path / "REPORT.md").is_file()
    assert (tmp_path / "equity-curve.html").is_file()
    assert "BLOCKED" in (tmp_path / "REPORT.md").read_text(encoding="utf-8")


def test_backtest_can_open_and_mark_to_market_bounded_short() -> None:
    start = date(2026, 1, 1)
    settings = BacktestSettings(
        start,
        start + timedelta(days=120),
        start + timedelta(days=60),
        Decimal("300000"),
        Decimal("0.0005"),
        Decimal("5"),
        Decimal("5"),
        Decimal("2"),
        "IMOEX",
        "SNDX",
        False,
        False,
    )
    universe = (
        UniverseEntry(
            "BEAR",
            "TQBR",
            "00000000-0000-0000-0000-000000000003",
            1,
            True,
            "issuer-bear",
            "sector-bear",
            "cluster-bear",
            "share",
            True,
        ),
    )
    base = _bot_config()
    short_strategy = StrategyConfig(
        1,
        Decimal("0.01"),
        True,
        candle_period="1D",
        momentum_windows=(2, 3),
        trend_window=4,
        volatility_window=3,
        inverse_volatility_weights=True,
        shorts_enabled=True,
        short_top_n=1,
        max_short_momentum=Decimal("-0.01"),
        long_target_gross=Decimal("0.60"),
        short_target_gross=Decimal("0.20"),
    )
    config = BotConfig(
        mode=base.mode,
        base_currency=base.base_currency,
        max_data_age_seconds=base.max_data_age_seconds,
        max_position_weight=base.max_position_weight,
        max_order_notional=base.max_order_notional,
        max_gross_exposure=base.max_gross_exposure,
        max_daily_turnover=base.max_daily_turnover,
        min_trade_notional=base.min_trade_notional,
        max_open_orders=base.max_open_orders,
        allow_margin=True,
        live_interlock=False,
        strategy=short_strategy,
        min_cash_reserve_weight=base.min_cash_reserve_weight,
        max_short_position_weight=Decimal("0.20"),
        max_short_gross_exposure=Decimal("0.20"),
    )
    result = run_backtest(
        bot_config=config,
        settings=settings,
        universe=universe,
        candles={"BEAR": _series(start, 121, step="-0.5")},
        benchmark_candles=_series(start, 121, step="-0.2"),
    )
    assert any(item.side == "sell" for item in result.trades)
    assert result.metrics.return_pct > 0
