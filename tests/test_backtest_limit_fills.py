"""Regression tests for research-only DAY execution, not broker API tests."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_backtest import _bot_config, _series
from test_execution import _intent

from moex_bot.backtest import load_backtest_settings, run_backtest
from moex_bot.backtest_fills import Window, load_execution_windows, match_session
from moex_bot.domain import OrderRecord, OrderStatus, Side
from moex_bot.integrations.moexalgo_data import HistoricalCandle
from moex_bot.shadow import UniverseEntry

DAY = date(2026, 1, 5)
ROOT = Path(__file__).resolve().parents[1]


def bar(o=100, h=102, low=98, c=100, v=1000, start=0, end=300):
    return Window(start, end, HistoricalCandle(DAY, *map(lambda x: D(str(x)), (o, h, low, c, v))))


def order(side=Side.BUY, lots=3, limit="100", key="one"):
    return OrderRecord(
        replace(
            _intent(),
            side=side,
            lots=lots,
            limit_price=D(limit),
            notional=D(limit) * lots * 10,
            order_request_id=key,
        ),
        OrderStatus.VALIDATED,
    )


def match(
    orders,
    bars,
    *,
    latency=0,
    cash=D(20000),
    positions=None,
    fee=D(".0005"),
    impact=D(0),
    participation=D("0.1"),
    reserve=D(0),
):
    return match_session(
        DAY,
        orders,
        {"SBER": bars},
        cash,
        positions or {},
        latency=latency,
        commission=fee,
        impact=impact,
        participation=participation,
        reserve=reserve,
    )


@pytest.mark.parametrize(
    "side,w",
    [
        (Side.BUY, bar(o=101, h=104, low=100, c=101)),
        (Side.SELL, bar(o=98, h=100, low=97, c=99)),
        (Side.BUY, bar(o=103, h=104, low=102, c=103)),
        (Side.SELL, bar(o=97, h=98, low=96, c=97)),
    ],
)
def test_exact_touch_or_gap_away_does_not_fill(side, w):
    cash, pos, fills, (outcome,) = match([order(side)], [w])
    assert (cash, pos, fills) == (20000, {}, ())
    assert (outcome.status, outcome.expired_lots, outcome.last_reason) == (
        "expired",
        3,
        "no_trade_through",
    )


@pytest.mark.parametrize(
    "side,w,price",
    [
        (Side.BUY, bar(o=95, h=98, low=94, c=96), "95.10"),
        (Side.SELL, bar(o=105, h=107, low=104, c=106), "104.89"),
        (Side.BUY, bar(o=103, h=104, low=99, c=101), "100"),
        (Side.SELL, bar(o=97, h=102, low=96, c=99), "100"),
    ],
)
def test_gap_improvement_or_later_cross_never_breaks_limit(side, w, price):
    cash, _, (f,), (outcome,) = match([order(side)], [w], impact=D(".001"), positions={"SBER": 3})
    assert f.fill_price == D(price)
    assert f.fill_price <= f.signal_limit if side is Side.BUY else f.fill_price >= f.signal_limit
    assert cash == 20000 + (f.gross if side is Side.SELL else -f.gross) - f.commission
    assert outcome.status == "filled"


def test_one_volume_budget_whole_lots_partial_and_residual_expires():
    # 250 shares * 10% = 25 shares -> 2 lots TOTAL, not 2 per order.
    cash, pos, fills, outcomes = match([order(lots=1), order(key="two")], [bar(v=250)])
    assert pos == {"SBER": 2}
    assert [f.lots for f in fills] == [1, 1]
    assert cash == D("17999")
    assert [(r.status, r.filled_lots, r.expired_lots) for r in outcomes] == [
        ("filled", 1, 0),
        ("partial_expired", 1, 2),
    ]
    assert match([order()], [bar(v=0)])[3][0].last_reason == "volume_cap"


def test_latency_skips_pre_arrival_extreme_and_volume_but_accepts_next_complete_window():
    pre = bar(o=95, h=98, low=94, c=96, v=10000)
    after = bar(v=100, start=300, end=600)
    late = bar(v=100, start=600, end=900)
    assert match([order()], [pre], latency=1)[3][0].last_reason == "not_active_for_full_window"
    _, pos, fills, (out,) = match([order()], [pre, after, late], latency=300)
    assert [f.window_start_seconds for f in fills] == [300, 600]
    assert [f.fill_price for f in fills] == [100, 100]
    assert (pos, out.status, out.expired_lots) == ({"SBER": 2}, "partial_expired", 1)
    assert match([order(lots=1)], [after, late], latency=300)[2][0].lots == 1
    assert len(match([order(lots=1)], [after, late], latency=300)[2]) == 1


def test_partial_cash_cover_and_unexecutable_tick():
    sell = order(Side.SELL)
    assert match([sell], [bar()])[3][0].last_reason == "no_unreserved_position"
    assert match([sell], [bar()], positions={"SBER": 1})[1] == {}
    with pytest.raises(ValueError):
        bar(v="Infinity")
    cash, pos, (fill,), _ = match([order()], [bar()], cash=D("2001"), reserve=D(1000))
    assert fill.lots == 1 and cash == D("1000.5") and pos == {"SBER": 1}
    cash, pos, (fill,), _ = match(
        [order()], [bar()], cash=D("1000.5"), reserve=D(1000), positions={"SBER": -1}
    )
    assert fill.lots == 1 and cash == 0 and pos == {}
    w = bar(o=99.999, h=99.999, low=99.998, c=99.999)
    assert match([order()], [w])[3][0].last_reason == "no_executable_tick"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_volume_participation": D("NaN")},
        {"max_volume_participation": D("1.01")},
        {"arrival_delay_seconds": True},
        {"arrival_delay_seconds": -1},
    ],
)
def test_execution_config_validation(kwargs):
    with pytest.raises(ValueError):
        replace(load_backtest_settings(ROOT / "config/backtest.json"), **kwargs)


def test_invalid_windows_and_order_types(tmp_path):
    p = tmp_path / "windows.jsonl"
    for data in ("", "{}"):
        p.write_text(data)
        with pytest.raises(ValueError):
            load_execution_windows(p)
    with pytest.raises(ValueError):
        bar(start=True)
    with pytest.raises(ValueError):
        match([replace(order(), status=OrderStatus.PLANNED)], [bar()])
    with pytest.raises(ValueError):
        match([replace(order(), intent=replace(order().intent, order_type="market"))], [bar()])
    with pytest.raises(ValueError):
        match([order()], [bar(), bar()])


def test_end_to_end_base_and_oos_expiry_no_daily_fallback(tmp_path):
    cfg = _bot_config()
    series = _series(DAY, 40, step="0.1")
    s = replace(
        load_backtest_settings(ROOT / "config/backtest.json"),
        start_date=DAY,
        end_date=series[-1].trading_date,
        oos_start_date=series[10].trading_date,
        arrival_delay_seconds=12,
    )
    args = dict(
        bot_config=cfg,
        settings=s,
        candles={"AAA": series},
        benchmark_candles=series,
        universe=(UniverseEntry("AAA", "TQBR", "uid", 1, True, "i", "s", "c", "share"),),
    )
    data = [
        {
            "trading_date": c.trading_date.isoformat(),
            "secid": "AAA",
            "start_seconds": 12,
            "end_seconds": 300,
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "volume": 100,
        }
        for c in series
    ]
    p = tmp_path / "windows.jsonl"
    p.write_text("\n".join(map(json.dumps, data)))
    windows = load_execution_windows(p)
    for override in ({"secid": ""}, {"volume": "bad"}, {"trading_date": None}):
        p.write_text(json.dumps(data[0] | override))
        with pytest.raises(ValueError):
            load_execution_windows(p)
    run = run_backtest(**args, execution_windows=windows)
    assert run.metrics.partial_orders > 0 and run.oos_metrics.expired_lots > 0
    assert run.metrics.orders_with_fills == sum(o.filled_lots > 0 for o in run.order_outcomes)
    assert sum(f.lots for f in run.trades) == sum(o.filled_lots for o in run.order_outcomes)
    for supplied, reason in [(None, "not_active_for_full_window"), ({}, "missing_window")]:
        blocked = run_backtest(**args, execution_windows=supplied)
        assert not blocked.trades and blocked.pending_at_end
        assert blocked.oos_metrics.unfilled_orders == sum(
            o.trading_date >= s.oos_start_date for o in blocked.order_outcomes
        )
        assert {o.last_reason for o in blocked.order_outcomes} == {reason}
    for bad in (
        {DAY: {"BAD": [bar()]}},
        {DAY: {"AAA": [bar(h=1000)]}},
        {DAY: {"AAA": [Window(0, 1, replace(series[0], volume=D("1e20")))]}},
    ):
        with pytest.raises(ValueError):
            run_backtest(**args, execution_windows=bad)
    # A future window can change future fills only.
    cutoff = DAY + timedelta(days=17)
    truncated = run_backtest(
        **{**args, "settings": replace(s, end_date=cutoff)}, execution_windows=windows
    )
    assert list(truncated.trades) == [f for f in run.trades if f.trading_date <= cutoff]
