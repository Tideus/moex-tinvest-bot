from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_backtest import _bot_config, _series

from moex_bot.adapters import DryRunExecutionAdapter
from moex_bot.backtest import _MemoryAudit, load_backtest_settings, run_backtest
from moex_bot.backtest_channel import ChannelReplayHarness, channel_levels
from moex_bot.domain import Instrument, MarketObservation, PortfolioSnapshot, Position
from moex_bot.integrations.moexalgo_data import HistoricalCandle
from moex_bot.shadow import UniverseEntry

NOW = datetime(2026, 5, 20, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def candle(offset=0, high=110, low=90, close=100):
    return HistoricalCandle(
        NOW.date() + timedelta(days=offset), D(close), D(high), D(low), D(close), D(100000)
    )


def make(book, held=None, cfg=None):
    cfg = cfg or _bot_config()
    market = {}
    for symbol, bars in book.items():
        i = Instrument(symbol, symbol, "TQBR", 1, D(".01"))
        market[symbol] = MarketObservation(
            i, bars[-1].close, D(101), D(0), D(".02"), NOW, True, True
        )
    model = ChannelReplayHarness(cfg, DryRunExecutionAdapter(cfg.mode), _MemoryAudit(), book)
    wallet = PortfolioSnapshot(
        D(300000), {s: Position(market[s].instrument, n) for s, n in (held or {}).items()}
    )
    return model, market, wallet


def test_window_lengths_end_before_decision_and_strict_boundaries():
    prev = [candle(-21, high=200, low=70), candle(-20, high=130, low=75)]
    prev += [candle(i) for i in range(-19, -10)] + [candle(-10, low=80)]
    prev += [candle(i) for i in range(-9, 0)]
    assert channel_levels(prev[:19]) is None
    assert channel_levels(prev) == (130, 80)
    book = {"A": prev + [candle(high=500, low=50, close=130)]}
    model, mkt, p = make(book)
    assert not model._select_targets(mkt, p, NOW)  # equality, including intrabar wick, is no entry
    mkt["A"] = replace(mkt["A"], price=D(131))
    assert model._select_targets(mkt, p, NOW)[0].secid == "A"
    held = replace(p, positions={"A": Position(mkt["A"].instrument, 3)})
    for price, expected in [(100, True), (80, True), (79, False), (100, False)]:
        mkt["A"] = replace(mkt["A"], price=D(price))
        assert bool(model._select_targets(mkt, held, NOW)) is expected
    # Fully flat: no outstanding exit latch; a new valid breakout may re-enter.
    mkt["A"] = replace(mkt["A"], price=D(150))
    assert model._select_targets(mkt, p, NOW)[0].rationale.endswith("breakout")


def test_deterministic_slot_priority_common_sizing_and_stale_rows():
    hist = [candle(i) for i in range(-20, 0)]
    book = {s: hist + [candle(high=200, close=c)] for s, c in (("C", 125), ("B", 130), ("A", 130))}
    cfg = _bot_config()
    cfg = replace(cfg, strategy=replace(cfg.strategy, top_n=2))
    model, mkt, p = make(book, cfg=cfg)
    mkt["B"] = replace(mkt["B"], volatility=D(".04"))
    targets = model._select_targets(mkt, p, NOW)
    assert [t.secid for t in targets] == ["A", "B"]
    assert abs(targets[0].weight - 2 * targets[1].weight) <= D("1e-27")
    model, mkt, p = make(book, held={"C": 1}, cfg=cfg)
    assert [t.secid for t in model._select_targets(mkt, p, NOW)] == ["C", "A"]
    book["A"] = hist + [candle(-1, high=200, close=140)]
    book["B"] = hist[:7] + [candle(high=200, close=155)]
    assert [t.secid for t in model._select_targets(mkt, p, NOW)] == ["C"]


def test_research_only_holdout_starts_flat_and_is_prefix_invariant():
    start = date(2026, 1, 1)
    cfg = _bot_config()
    series = _series(start, 100, step="2")
    first = series[30].trading_date
    s = replace(
        load_backtest_settings(ROOT / "config/backtest.json"),
        start_date=start,
        end_date=series[-1].trading_date,
        oos_start_date=first,
    )
    args = dict(
        bot_config=cfg,
        settings=s,
        candles={"AAA": series},
        benchmark_candles=series,
        universe=(UniverseEntry("AAA", "TQBR", "uid", 1, True, "i", "s", "c", "share"),),
        channel_long=True,
        first_decision_date=first,
    )
    full = run_backtest(**args)
    assert full.metrics.start_date == first and full.metrics.start_equity == s.initial_cash
    assert full.trades[0].trading_date == series[31].trading_date
    cutoff = series[50].trading_date
    short = run_backtest(**(args | {"settings": replace(s, end_date=cutoff)}))
    assert list(short.trades) == [t for t in full.trades if t.trading_date <= cutoff]
    assert list(short.equity_curve) == [e for e in full.equity_curve if e.trading_date <= cutoff]
    for strategy in (
        replace(cfg.strategy, shorts_enabled=True),
        replace(cfg.strategy, candle_period="1h"),
    ):
        with pytest.raises(ValueError, match="long-only daily"):
            run_backtest(**(args | {"bot_config": replace(cfg, strategy=strategy)}))
