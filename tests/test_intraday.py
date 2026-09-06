import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from moex_bot.cli import _verified_instruments_by_secid
from moex_bot.domain import Instrument, Side
from moex_bot.integrations.algopack_intraday import IntradaySuperCandle
from moex_bot.intraday import (
    IntradaySignal,
    IntradayStateStore,
    build_intraday_plan,
    render_intraday_report,
)
from moex_bot.intraday_config import load_intraday_config
from moex_bot.validation import load_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOSCOW = ZoneInfo("Europe/Moscow")


def _instrument(
    secid: str = "SBER", uid: str = "e6123145-9665-43e0-8413-cd61b8aa9b13"
) -> Instrument:
    return Instrument(
        secid,
        uid,
        "TQBR",
        1,
        Decimal("0.01"),
        short_enabled=True,
    )


def _bars(
    start: datetime, *, falling: bool = False, secid: str = "SBER"
) -> tuple[IntradaySuperCandle, ...]:
    prices = (Decimal("100"), Decimal("100.2"), Decimal("100.5"))
    if falling:
        prices = tuple(reversed(prices))
    result = []
    for index, price in enumerate(prices):
        buy, sell = (Decimal("800"), Decimal("200"))
        put_b, put_s = (Decimal("900"), Decimal("100"))
        book = Decimal("0.4")
        if falling:
            buy, sell = sell, buy
            put_b, put_s = put_s, put_b
            book = -book
        result.append(
            IntradaySuperCandle(
                secid,
                start + timedelta(minutes=index * 5),
                price,
                price,
                buy,
                sell,
                put_b,
                put_s,
                Decimal("0"),
                Decimal("0"),
                Decimal("0.10"),
                book,
            )
        )
    return tuple(result)


def _portfolio(*, positions: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source": "t_invest_sandbox",
        "account_id": "intraday",
        "cash": "300000",
        "reported_equity": "300000",
        "daily_turnover": "0",
        "open_orders": 0,
        "positions": positions or {},
    }


def test_intraday_momentum_builds_bounded_buy_and_deduplicates(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    assert config.min_abs_order_flow == Decimal("0.002")
    assert config.max_spread_bbo == Decimal("3.0")
    store = IntradayStateStore(tmp_path / "intraday.sqlite3")
    as_of = datetime(2026, 8, 20, 10, 30, tzinfo=MOSCOW)
    bars = _bars(as_of - timedelta(minutes=15))
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=store,
        bars=bars,
        as_of=as_of,
    )
    assert plan.orders[0].side is Side.BUY
    assert plan.orders[0].notional <= Decimal("10000")
    repeated = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=store,
        bars=bars,
        as_of=as_of,
    )
    assert repeated.orders == ()
    assert "INTRADAY SANDBOX" in render_intraday_report(plan)
    assert plan.signal_diagnostics == {
        "instruments_evaluated": 1,
        "signals_passed": 1,
    }


def test_intraday_falling_flow_opens_verified_short(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    as_of = datetime(2026, 8, 20, 11, 0, tzinfo=MOSCOW)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=IntradayStateStore(tmp_path / "short.sqlite3"),
        bars=_bars(as_of - timedelta(minutes=15), falling=True),
        as_of=as_of,
    )
    assert plan.orders[0].side is Side.SELL
    assert plan.orders[0].confirm_margin_trade


def test_force_flat_closes_position_and_plan_is_execution_compatible(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    as_of = datetime(2026, 8, 20, 18, 45, tzinfo=MOSCOW)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(positions={"SBER": {"lots": 3, "blocked_lots": 0}}),
        store=IntradayStateStore(tmp_path / "flat.sqlite3"),
        bars=_bars(as_of - timedelta(minutes=15)),
        as_of=as_of,
    )
    assert plan.phase == "force_flat"
    assert plan.orders[0].side is Side.SELL
    assert plan.orders[0].lots == 3
    assert plan.orders[0].order_type == "market"
    output = tmp_path / "plan.json"
    plan.write(output, analysis_input={"supercandles": [{"secid": "SBER"}]})
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["run_id"].startswith("intraday-")
    assert raw["orders"][0]["status"] == "validated"
    assert raw["analysis_input"]["supercandles"][0]["secid"] == "SBER"


def test_force_flat_uses_broker_value_when_algopack_is_empty(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    as_of = datetime(2026, 8, 20, 18, 50, tzinfo=MOSCOW)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(
            positions={
                "SBER": {
                    "lots": 3,
                    "blocked_lots": 0,
                    "current_value": "9030",
                }
            }
        ),
        store=IntradayStateStore(tmp_path / "flat-no-flow.sqlite3"),
        bars=(),
        as_of=as_of,
    )
    assert plan.quality_passed
    assert plan.quality_errors == ()
    assert plan.orders[0].side is Side.SELL
    assert plan.orders[0].limit_price == Decimal("3010")
    assert "broker valuation" in plan.notes[-1]


def test_after_hours_without_positions_does_not_fail_on_empty_algopack(
    tmp_path: Path,
) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=IntradayStateStore(tmp_path / "flat-empty.sqlite3"),
        bars=(),
        as_of=datetime(2026, 8, 20, 18, 55, tzinfo=MOSCOW),
    )
    assert plan.phase == "force_flat"
    assert plan.quality_passed
    assert plan.orders == ()


def test_intraday_universe_is_indexed_by_moex_secid_not_tinvest_uid() -> None:
    instruments = _verified_instruments_by_secid(
        load_universe(PROJECT_ROOT / "config" / "universe.json")
    )
    assert "SBER" in instruments
    assert instruments["SBER"].uid == "e6123145-9665-43e0-8413-cd61b8aa9b13"
    assert instruments["SBER"].secid == "SBER"


def test_existing_gross_exposure_is_subtracted_from_intraday_capital(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    as_of = datetime(2026, 8, 20, 11, 0, tzinfo=MOSCOW)
    sber = _bars(as_of - timedelta(minutes=15))
    other = _bars(as_of - timedelta(minutes=15), secid="TEST")
    store = IntradayStateStore(tmp_path / "gross.sqlite3")
    store.mark_signal(
        IntradaySignal(
            "SBER",
            Side.BUY,
            as_of - timedelta(minutes=15),
            Decimal("100.5"),
            Decimal("0.005"),
            Decimal("0.6"),
            Decimal("0.8"),
            Decimal("0.4"),
            Decimal("0.1"),
        )
    )
    plan = build_intraday_plan(
        config=config,
        instruments={
            "SBER": _instrument(),
            "TEST": _instrument("TEST", "00000000-0000-0000-0000-000000000001"),
        },
        portfolio=_portfolio(positions={"SBER": {"lots": 300, "blocked_lots": 0}}),
        store=store,
        bars=sber + other,
        as_of=as_of,
    )
    assert plan.signals
    assert plan.orders == ()


def test_empty_supercandles_record_actionable_quality_error(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    as_of = datetime(2026, 8, 20, 11, 0, tzinfo=MOSCOW)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=IntradayStateStore(tmp_path / "empty.sqlite3"),
        bars=(),
        as_of=as_of,
    )
    assert not plan.quality_passed
    assert plan.quality_errors == ("no completed TradeStats/OrderStats/OBStats",)
    output = tmp_path / "empty-plan.json"
    plan.write(output)
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["quality"]["errors"] == ["no completed TradeStats/OrderStats/OBStats"]
    assert raw["signal_diagnostics"]["failed_history"] == 1


def test_open_position_is_closed_by_stop_loss_before_session_end(tmp_path: Path) -> None:
    config = replace(
        load_intraday_config(PROJECT_ROOT / "config" / "intraday.json"),
        stop_loss_bps=Decimal("40"),
    )
    store = IntradayStateStore(tmp_path / "stop.sqlite3")
    entered_at = datetime(2026, 8, 20, 10, 15, tzinfo=MOSCOW)
    entry_plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=store,
        bars=_bars(entered_at),
        as_of=entered_at + timedelta(minutes=15),
    )
    assert entry_plan.orders
    exit_plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(
            positions={"SBER": {"lots": 10, "current_value": "980"}}
        ),
        store=store,
        bars=_bars(entered_at + timedelta(minutes=5), falling=True),
        as_of=entered_at + timedelta(minutes=20),
    )
    assert exit_plan.phase == "position_exit"
    assert exit_plan.orders[0].side is Side.SELL
    assert exit_plan.orders[0].order_type == "market"
    assert "stop_loss" in exit_plan.notes[0]


def test_open_position_is_closed_after_max_holding_time(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    store = IntradayStateStore(tmp_path / "holding.sqlite3")
    entered_at = datetime(2026, 8, 20, 10, 15, tzinfo=MOSCOW)
    signal = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=store,
        bars=_bars(entered_at),
        as_of=entered_at + timedelta(minutes=15),
    )
    assert signal.orders
    as_of = entered_at + timedelta(minutes=config.max_holding_minutes + 15)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(
            positions={"SBER": {"lots": 10, "current_value": "1005"}}
        ),
        store=store,
        bars=_bars(as_of - timedelta(minutes=15)),
        as_of=as_of,
    )
    assert plan.phase == "position_exit"
    assert "max_holding" in plan.notes[0]


def test_signal_requires_edge_after_spread_and_estimated_costs(tmp_path: Path) -> None:
    config = replace(
        load_intraday_config(PROJECT_ROOT / "config" / "intraday.json"),
        min_edge_after_cost_bps=Decimal("100"),
    )
    as_of = datetime(2026, 8, 20, 11, 0, tzinfo=MOSCOW)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=IntradayStateStore(tmp_path / "cost.sqlite3"),
        bars=_bars(as_of - timedelta(minutes=15)),
        as_of=as_of,
    )
    assert plan.orders == ()
    assert plan.signal_diagnostics is not None
    assert plan.signal_diagnostics["failed_cost_edge"] == 1


def test_intraday_state_store_migrates_legacy_signal_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE intraday_signals ("
            "trade_day TEXT NOT NULL, secid TEXT NOT NULL, observed_at TEXT NOT NULL, "
            "side TEXT NOT NULL, PRIMARY KEY(secid, observed_at, side))"
        )
    store = IntradayStateStore(path)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(intraday_signals)")}
    assert "price" in columns
    assert store.latest_signal("2026-08-20", "SBER") is None


def test_unmanaged_intraday_position_is_closed_fail_safe(tmp_path: Path) -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    as_of = datetime(2026, 8, 20, 11, 0, tzinfo=MOSCOW)
    plan = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(
            positions={"SBER": {"lots": -2, "current_value": "200"}}
        ),
        store=IntradayStateStore(tmp_path / "unmanaged.sqlite3"),
        bars=_bars(as_of - timedelta(minutes=15)),
        as_of=as_of,
    )
    assert plan.phase == "position_exit"
    assert plan.orders[0].side is Side.BUY
    assert "missing_entry_state" in plan.notes[0]
