"""Operator input boundary tests; no broker or systemd mutations."""

import json
from dataclasses import replace
from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.intraday_config import _validate, load_intraday_config
from moex_bot.runtime_config import (
    load_runtime_config,
    materialize_runtime_defaults,
    set_runtime_environment,
    set_sandbox_orders_enabled,
)
from moex_bot.service_config import TInvestEnvironment

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_stage", "prod"),
        ("candle_minutes", 1),
        ("scan_interval_minutes", 1),
        ("max_capital_weight", D("0")),
        ("max_daily_loss_weight", D("0.02")),
        ("max_position_notional_rub", D("0")),
        ("max_daily_turnover_rub", D("0")),
        ("max_concurrent_positions", 3),
        ("max_entries_per_day", 4),
        ("allow_overnight", True),
        ("order_type", "market"),
        ("order_ttl_seconds", 1),
        ("chase_price", True),
        ("estimated_round_trip_cost_bps", D("Infinity")),
        ("min_edge_after_cost_bps", D("0")),
        ("stop_loss_bps", D("1001")),
        ("take_profit_bps", D("1")),
        ("max_holding_minutes", 0),
        ("enabled_strategies", ()),
        ("enabled_strategies", ("",)),
        ("new_entries_start_moscow", time(23)),
        ("history_bars", 13),
        ("min_price_move", D("Infinity")),
        ("min_abs_trade_imbalance", D("0")),
        ("min_abs_order_flow", D("2")),
        ("min_abs_book_imbalance", D("0")),
        ("max_spread_bbo", D("Infinity")),
        ("max_spread_bbo", D("0")),
        ("max_spread_bbo", D("101")),
    ],
)
def test_intraday_rejects_unsafe_settings(field, value):
    config = load_intraday_config(ROOT / "config/intraday.json")
    with pytest.raises(ValueError):
        _validate(replace(config, **{field: value}))


@pytest.mark.parametrize(
    "location,value",
    [
        ((), []),
        (("risk",), []),
        (("strategies",), {}),
        (("strategies",), [False]),
        (("strategies",), [{"enabled": "true"}]),
        (("candle_minutes",), True),
        (("risk", "allow_short"), 1),
        (("strategies",), [{"enabled": False}]),
    ],
)
def test_intraday_parse_boundaries(tmp_path, location, value):
    raw = json.loads((ROOT / "config/intraday.json").read_text())
    if not location:
        raw = value
    else:
        current = raw
        for part in location[:-1]:
            current = current[part]
        current[location[-1]] = value
    path = tmp_path / "intraday.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_intraday_config(path)


@pytest.mark.parametrize(
    "location,value",
    [
        ((), []),
        (("schedule",), []),
        (("sandbox_orders_enabled",), "true"),
        (("sandbox_max_orders_per_cycle",), 11),
        (("sandbox_max_orders_per_cycle",), True),
        (("schedule", "timezone"), "Not/AZone"),
        (("schedule", "shadow_on_calendar"), ""),
        (("schedule", "shadow_on_calendar"), "daily\n[Service]"),
        (("schedule", "shadow_on_calendar"), "daily\r"),
        (("schedule", "shadow_on_calendar"), "a" * 81),
        (("schedule", "daily_report_on_calendar"), ""),
        (("schedule", "daily_report_on_calendar"), "daily\n"),
        (("schedule", "daily_report_on_calendar"), "daily\r"),
        (("schedule", "daily_report_on_calendar"), "a" * 81),
        (("schedule", "health_interval"), "0min"),
        (("schedule", "health_on_boot"), "abc"),
        (("schedule", "shadow_randomized_delay_seconds"), -1),
        (("schedule", "diagnostics_interval_seconds"), 9),
    ],
)
def test_runtime_parse_boundaries(tmp_path, location, value):
    raw = json.loads((ROOT / "config/runtime.json").read_text())
    if not location:
        raw = value
    else:
        current = raw
        for part in location[:-1]:
            current = current[part]
        current[location[-1]] = value
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_runtime_config(path)


def test_runtime_rejects_sandbox_enabled_in_prod(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(dict(t_invest_environment="prod", sandbox_orders_enabled=True)))
    with pytest.raises(ValueError, match="sandbox orders require"):
        load_runtime_config(path)
    with pytest.raises(ValueError, match="sandbox orders require"):
        set_sandbox_orders_enabled(path, True)


@pytest.mark.parametrize(
    "operation",
    [
        lambda p: set_runtime_environment(p, TInvestEnvironment.SANDBOX),
        lambda p: set_sandbox_orders_enabled(p, False),
        materialize_runtime_defaults,
    ],
)
def test_runtime_edit_refuses_non_object(tmp_path, operation):
    path = tmp_path / "runtime.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        operation(path)


def test_runtime_creates_new_environment_and_preserves_false(tmp_path):
    path = tmp_path / "new" / "runtime.json"
    set_runtime_environment(path, TInvestEnvironment.SANDBOX)
    assert load_runtime_config(path).environment is TInvestEnvironment.SANDBOX
    assert not materialize_runtime_defaults(path)
    set_sandbox_orders_enabled(path, False)
    assert not load_runtime_config(path).sandbox_orders_enabled
