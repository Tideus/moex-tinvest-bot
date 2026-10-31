"""Backtest validation, fill timing and no-fill boundaries."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_backtest import _bot_config, _series
from test_execution import _intent
from test_harness import _config, _market

from moex_bot.adapters import DryRunExecutionAdapter, JsonlAuditLog
from moex_bot.backtest import (
    EquityPoint,
    _execute_pending,
    _metrics,
    _security_pnl,
    load_backtest_settings,
    run_backtest,
    serialize_backtest,
)
from moex_bot.domain import OrderRecord, OrderStatus, PortfolioSnapshot, Position, Side
from moex_bot.harness import TradingHarness
from moex_bot.shadow import UniverseEntry

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fault", ["shape", "range", "oos", "cash", "cost", "bool"])
def test_backtest_settings_reject_invalid_inputs(tmp_path: Path, fault: str) -> None:
    raw = json.loads((ROOT / "config/backtest.json").read_text())
    if fault == "shape":
        raw = []
    elif fault == "range":
        raw["end_date"] = raw["start_date"]
    elif fault == "oos":
        raw["oos_start_date"] = "2000-01-01"
    elif fault == "cash":
        raw["initial_cash"] = "NaN"
    elif fault == "cost":
        raw["commission_rate"] = "-1"
    elif fault == "bool":
        raw["survivorship_safe"] = "false"
    path = tmp_path / "config"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_backtest_settings(path)


@pytest.mark.parametrize(
    "fault",
    ["missing", "buy_touch", "buy_miss", "sell_touch", "sell_miss", "cash", "sell_close", "cover"],
)
def test_pending_orders_use_session_range_and_available_cash(fault: str) -> None:
    settings = load_backtest_settings(ROOT / "config/backtest.json")
    intent = replace(_intent(), limit_price=Decimal(100), notional=Decimal(2000))
    candle = _series(date(2026, 1, 1), 1, step="1")[0]
    if fault == "buy_touch":
        candle = replace(candle, open=Decimal(101), low=Decimal(99))
    elif fault == "buy_miss":
        candle = replace(candle, open=Decimal(102), low=Decimal(101))
    elif fault.startswith("sell"):
        intent = replace(intent, side=Side.SELL)
        candle = replace(
            candle,
            open=Decimal(99),
            close=Decimal(99),
            high=Decimal(99) if fault == "sell_miss" else Decimal(101),
        )
    positions = (
        {"SBER": 2 if fault == "sell_close" else -2} if fault in {"sell_close", "cover"} else {}
    )
    result = _execute_pending(
        (OrderRecord(intent, OrderStatus.VALIDATED),),
        {} if fault == "missing" else {"SBER": candle},
        Decimal(0) if fault == "cash" else Decimal(300000),
        positions,
        _bot_config(),
        settings,
        Decimal(1),
    )
    assert result[3] == int(fault in {"missing", "buy_miss", "sell_miss", "cash"})
    if fault in {"sell_close", "cover"}:
        assert not result[1]


@pytest.mark.parametrize("fault", ["cost", "points", "oos", "serialize"])
def test_backtest_requires_sufficient_points_and_serializes(tmp_path: Path, fault: str) -> None:
    settings = load_backtest_settings(ROOT / "config/backtest.json")
    settings = replace(
        settings,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 5, 1),
        oos_start_date=date(2026, 2, 1),
    )
    if fault == "oos":
        settings = replace(settings, oos_start_date=date(2026, 5, 1))
    universe = (
        UniverseEntry("AAA", "TQBR", "uid", 1, True, "issuer", "sector", "cluster", "share"),
    )
    kwargs = dict(
        bot_config=_bot_config(),
        settings=settings,
        universe=universe,
        candles={"AAA": _series(settings.start_date, 1 if fault == "points" else 120, step="1")},
        benchmark_candles=(),
        cost_multiplier=Decimal(0) if fault == "cost" else Decimal(1),
    )
    if fault != "serialize":
        with pytest.raises(ValueError):
            run_backtest(**kwargs)
    else:
        result = run_backtest(**kwargs)
        path = tmp_path / "backtest.json"
        serialize_backtest(result, path)
        assert json.loads(path.read_text())["metrics"]["benchmark_return_pct"] is None
    assert _security_pnl((), {"unknown": 1}, {}, {}) == {}


def test_harness_projects_full_exit_without_keeping_zero_position(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    market = _market(now)
    market["AAA"] = replace(market["AAA"], momentum=Decimal("-.1"))
    config = _config()
    harness = TradingHarness(
        config, DryRunExecutionAdapter(config.mode), JsonlAuditLog(tmp_path / "audit")
    )
    result = harness.run(
        run_id="close",
        as_of=now,
        market=market,
        portfolio=PortfolioSnapshot(
            Decimal(100000), {"AAA": Position(market["AAA"].instrument, 1)}
        ),
        geo_events=(),
    )
    assert any(order.intent.side is Side.SELL for order in result.orders)


def test_metrics_does_not_divide_drawdown_by_nonpositive_peak() -> None:
    points = (
        EquityPoint(date(2026, 1, 1), Decimal(-100), None),
        EquityPoint(date(2026, 1, 2), Decimal(-50), None),
    )
    metrics = _metrics(points, (), 0, ())
    assert metrics.max_drawdown_pct == 0 and metrics.return_pct == -50
