"""Boundary contracts for immutable models and operator configuration."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.account_profiles import load_account_registry
from moex_bot.config import _integer_tuple, load_config
from moex_bot.domain import (
    GeoEvent,
    Instrument,
    MarketObservation,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    Side,
)

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_currency", "USD"),
        ("max_data_age_seconds", 0),
        ("max_position_weight", D("0")),
        ("max_gross_exposure", D("2")),
        ("min_cash_reserve_weight", D("-1")),
        ("max_sector_weight", D("Infinity")),
        ("max_risk_cluster_weight", D("0")),
        ("max_order_notional", D("0")),
        ("max_daily_turnover", D("Infinity")),
        ("min_trade_notional", D("0")),
        ("max_open_orders", 0),
        ("allow_margin", True),
        ("live_interlock", True),
    ],
)
def test_bot_config_invalid_boundaries(field, value):
    config = replace(load_config(ROOT / "config/shadow.json"), **{field: value})
    assert config.validate(), field


@pytest.mark.parametrize(
    "field,value",
    [
        ("top_n", 0),
        ("min_momentum", D("Infinity")),
        ("min_momentum", D("-1")),
        ("candle_period", "5m"),
        ("momentum_windows", ()),
        ("momentum_windows", (0,)),
        ("momentum_windows", (5, 3)),
        ("trend_window", 1),
        ("volatility_window", 1),
        ("volatility_floor", D("Infinity")),
        ("volatility_floor", D("0")),
        ("exit_rank_buffer", -1),
        ("rebalance_hours_moscow", ()),
        ("rebalance_hours_moscow", (24,)),
        ("rebalance_hours_moscow", (10, 10)),
        ("short_top_n", 1),
        ("long_target_gross", D("2")),
        ("short_target_gross", D("1")),
        ("long_target_gross", D("Infinity")),
        ("flow_min_trade_imbalance", D("-2")),
        ("flow_min_order_flow", D("2")),
        ("flow_min_book_imbalance", D("Infinity")),
        ("flow_max_spread_bbo", D("Infinity")),
        ("flow_max_spread_bbo", D("0")),
        ("flow_max_spread_bbo", D("101")),
    ],
)
def test_strategy_config_invalid_boundaries(field, value):
    config = load_config(ROOT / "config/shadow.json")
    strategy = replace(
        config.strategy, shorts_enabled=False, short_top_n=0, short_target_gross=D("0")
    )
    strategy = replace(strategy, **{field: value})
    assert replace(config, strategy=strategy).validate(), field


def test_short_configuration_constraints():
    base = load_config(ROOT / "config/shadow.json")
    strategy = replace(
        base.strategy,
        shorts_enabled=True,
        short_top_n=0,
        max_short_momentum=D("0"),
        short_target_gross=D("0"),
    )
    config = replace(
        base,
        strategy=strategy,
        allow_margin=False,
        max_short_position_weight=D("0"),
        max_short_gross_exposure=D("0"),
    )
    errors = " ".join(config.validate())
    for term in (
        "shorts require",
        "short_top_n",
        "max_short_momentum",
        "max_short_position_weight",
        "max_short_gross_exposure",
        "short_target_gross",
    ):
        assert term in errors
    valid = replace(
        config,
        allow_margin=True,
        max_short_position_weight=D("0.1"),
        max_short_gross_exposure=D("0.2"),
        strategy=replace(
            strategy, short_top_n=1, max_short_momentum=D("-0.1"), short_target_gross=D("0.2")
        ),
    )
    assert not valid.validate()


@pytest.mark.parametrize("value", [(1,), [True], [None], ["bad"]])
def test_integer_array_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="JSON integer array"):
        _integer_tuple(value, "test")


def test_config_loader_validates_after_parse(tmp_path):
    raw = json.loads((ROOT / "config/shadow.json").read_text())
    raw["max_open_orders"] = 0
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="max_open_orders"):
        load_config(path)


@pytest.fixture
def instrument():
    return Instrument(
        "SBER",
        "uid",
        "TQBR",
        10,
        D("0.01"),
        issuer_id="sber",
        sector="finance",
        risk_cluster="bank",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("secid", ""),
        ("lot_size", 0),
        ("tick_size", D("0")),
        ("issuer_id", ""),
        ("sector", ""),
        ("risk_cluster", ""),
    ],
)
def test_instrument_rejects_invalid_identity(instrument, field, value):
    with pytest.raises(ValueError):
        replace(instrument, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", D("0")),
        ("trend", D("0")),
        ("volatility", D("-1")),
        ("observed_at", datetime(2026, 1, 1)),
    ],
)
def test_market_observation_boundaries(instrument, field, value):
    observation = MarketObservation(
        instrument, D("100"), D("90"), D("0.1"), D("0.01"), datetime.now(UTC), True, True
    )
    with pytest.raises(ValueError):
        replace(observation, **{field: value})


@pytest.mark.parametrize("lots,blocked", [(1, -1), (-1, 2)])
def test_position_blocked_lots(instrument, lots, blocked):
    with pytest.raises(ValueError):
        Position(instrument, lots, blocked)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cash": D("NaN")},
        {"cash": D("-1")},
        {"blocked_cash": D("Infinity")},
        {"blocked_cash": D("-1")},
        {"open_orders": -1},
        {"reported_equity": D("NaN")},
    ],
)
def test_portfolio_rejects_invalid_amounts(kwargs):
    with pytest.raises(ValueError):
        PortfolioSnapshot(**({"cash": D("100")} | kwargs))


def test_portfolio_requires_price_for_every_holding(instrument):
    portfolio = PortfolioSnapshot(D("100"), {"SBER": Position(instrument, 1)})
    with pytest.raises(ValueError, match="missing price"):
        portfolio.equity({})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"severity": -1},
        {"severity": 6},
        {"confidence": D("-0.1")},
        {"confidence": D("1.1")},
        {"observed_at": datetime(2026, 1, 1)},
    ],
)
def test_geo_event_boundaries(kwargs):
    values = dict(
        event_id="e",
        severity=2,
        confidence=D("1"),
        source_tier="primary",
        confirmed=True,
        affected_secids=frozenset(),
        observed_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError):
        GeoEvent(**(values | kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lots": 0},
        {"limit_price": D("0")},
        {"notional": D("0")},
        {"order_type": "stop"},
    ],
)
def test_order_intent_boundaries(instrument, kwargs):
    values = dict(
        order_request_id="id",
        instrument=instrument,
        side=Side.BUY,
        lots=1,
        limit_price=D("100"),
        notional=D("1000"),
        rationale="test",
    )
    with pytest.raises(ValueError):
        OrderIntent(**(values | kwargs))


@pytest.mark.parametrize(
    "location,value",
    [
        (("version",), 2),
        (("version",), True),
        (("base_currency",), "USD"),
        (("profiles",), []),
        (("profiles", 0), {}),
        (("profiles", 0, "profile_id"), ""),
        (("profiles", 1, "purpose"), "long"),
        (("profiles", 0, "account_id_env"), "bad-name"),
        (("profiles", 0, "environment"), "prod"),
        (("profiles", 0, "target_balance_rub"), "0"),
        (("profiles", 0, "target_balance_rub"), "Infinity"),
        (("profiles", 0, "strategies"), []),
        (("profiles", 0, "strategies", 0), "bad"),
        (("profiles", 0, "strategies", 0, "strategy_id"), ""),
        (("profiles", 0, "strategies", 0, "config_path"), "/etc/config"),
        (("profiles", 0, "strategies", 0, "config_path"), "config/../secret"),
        (("profiles", 0, "strategies", 0, "config_path"), "other/file"),
        (("profiles", 0, "order_execution_enabled"), "true"),
    ],
)
def test_account_registry_boundaries(tmp_path, location, value):
    raw = json.loads((ROOT / "config/accounts.json").read_text())
    current = raw
    for key in location[:-1]:
        current = current[key]
    current[location[-1]] = value
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_account_registry(path)


def test_registry_missing_profiles_and_unknown_id(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="profiles array"):
        load_account_registry(path)
    registry = load_account_registry(ROOT / "config/accounts.json")
    with pytest.raises(KeyError):
        registry.by_id("not-present")
