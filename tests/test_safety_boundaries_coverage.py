import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from moex_bot.adapters import DryRunExecutionAdapter
from moex_bot.config import load_config
from moex_bot.domain import (
    ExecutionMode,
    GeoEvent,
    GeoRiskLevel,
    GeoRiskSnapshot,
    Instrument,
    MarketObservation,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    Side,
)
from moex_bot.env_file import upsert_env_values
from moex_bot.geo import assess_geo_risk
from moex_bot.integration_health import probe_algopack
from moex_bot.notification_policy import load_notification_policy, should_send_long_morning
from moex_bot.quality import validate_market
from moex_bot.risk import evaluate_intent
from moex_bot.scheduler import is_conservative_stock_window, next_hourly_run

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
NOW = datetime(2026, 10, 30, 10, tzinfo=UTC)


def test_dry_run_refuses_live():
    with pytest.raises(ValueError, match="live execution"):
        DryRunExecutionAdapter(ExecutionMode.LIVE)


@pytest.mark.parametrize("minute", [-1, 60])
def test_scheduler_invalid_minutes(minute):
    with pytest.raises(ValueError):
        next_hourly_run(NOW, minute)


@pytest.mark.parametrize("function", [next_hourly_run, is_conservative_stock_window])
def test_scheduler_requires_timezone(function):
    with pytest.raises(ValueError):
        function(datetime(2026, 10, 30))


def test_scheduler_upcoming_same_hour():
    assert next_hourly_run(NOW.replace(minute=1)).minute == 5


def test_geo_event_order_never_deescalates():
    def event(severity, confirmed=True):
        return GeoEvent(
            str(severity), severity, D("1"), "primary", confirmed, frozenset({"SBER"}), NOW
        )

    assert assess_geo_risk([event(4)]).level is GeoRiskLevel.HIGH
    risk = assess_geo_risk([event(5), event(4), event(2), event(1), event(5, False)])
    assert risk.level is GeoRiskLevel.CRITICAL
    assert risk.multiplier == 0
    elevated = assess_geo_risk([event(2), event(2), event(1)])
    assert elevated.level is GeoRiskLevel.ELEVATED


@pytest.mark.parametrize("moment,market", [(datetime(2026, 1, 1), {}), (NOW, {})])
def test_market_quality_missing_input(moment, market):
    assert not validate_market(market, moment, 60).passed


def test_market_quality_checks_identity_age_and_completeness():
    instrument = Instrument("SBER", "uid", "TQBR", 1, D("0.01"))
    observation = MarketObservation(
        instrument, D("100"), D("90"), D("0.1"), D("0"), NOW + timedelta(seconds=1), False, True
    )
    errors = validate_market({"WRONG": observation}, NOW, 60).errors
    assert len(errors) == 3
    stale = replace(observation, observed_at=NOW - timedelta(seconds=61), complete=True)
    assert "stale" in " ".join(validate_market({"SBER": stale}, NOW, 60).errors)


@pytest.mark.parametrize("key,value", [("bad-name", "x"), ("KEY", "x\ny"), ("KEY", "x\ry")])
def test_env_rejects_injected_input(tmp_path, key, value):
    with pytest.raises(ValueError):
        upsert_env_values(tmp_path / "bot.env", {key: value})


def test_env_cleans_temporary_file_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "bot.env"
    path.write_text("KEEP=x\n")

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        upsert_env_values(path, {"NEW": "y"})
    assert path.read_text() == "KEEP=x\n"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "location,value",
    [
        ((), []),
        (("long",), []),
        (("timezone",), "Invalid/Zone"),
        (("long", "morning_analysis_hour"), 24),
        (("long", "morning_analysis_hour"), True),
        (("long", "evening_report_enabled"), "true"),
        (("audit", "persist_every_cycle"), False),
    ],
)
def test_notification_policy_rejects_invalid_input(tmp_path, location, value):
    raw = json.loads((ROOT / "config/notifications.json").read_text())
    if not location:
        raw = value
    else:
        current = raw
        for key in location[:-1]:
            current = current[key]
        current[location[-1]] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_notification_policy(path)


def test_notifications_reject_naive_time():
    policy = load_notification_policy(ROOT / "config/notifications.json")
    with pytest.raises(ValueError):
        should_send_long_morning(policy, datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "body,status",
    [
        (b"notjson", "invalid_response"),
        (b"\xff", "invalid_response"),
        (b"[]", "invalid_response"),
        (b"{}", "ok"),
    ],
)
def test_algopack_probe_response_shapes(body, status):
    response = probe_algopack(
        base_url="https://test", token="safe", as_of=NOW, opener=lambda *a, **k: io.BytesIO(body)
    )
    assert response.status == status


@pytest.mark.parametrize(
    "error,status",
    [
        (HTTPError("https://test", 403, "denied", {}, None), "http_403"),
        (TimeoutError(), "network"),
        (URLError("unavailable"), "network"),
    ],
)
def test_algopack_probe_errors(error, status):
    def fail(*args, **kwargs):
        raise error

    assert (
        probe_algopack(base_url="https://test", token="safe", as_of=NOW, opener=fail).status
        == status
    )


def test_algopack_probe_requires_aware_time_and_positive_timeout():
    with pytest.raises(ValueError):
        probe_algopack(base_url="https://test", token="safe", as_of=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        probe_algopack(base_url="https://test", token="safe", as_of=NOW, timeout=0)


@pytest.mark.parametrize(
    "case",
    [
        "turnover",
        "open_orders",
        "broker_orders",
        "short",
        "non_short_margin",
        "cover_blocked",
        "zero_equity",
        "critical",
    ],
)
def test_risk_gate_boundary_reasons(case):
    config = load_config(ROOT / "config/shadow.json")
    instrument = Instrument("AAA", "uid", "TQBR", 1, D("0.01"))
    portfolio = PortfolioSnapshot(D("100000"))
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, D("1"), frozenset(), ())
    intent = OrderIntent("id", instrument, Side.BUY, 1, D("1000"), D("1000"), "test")
    equity = D("100000")
    if case == "turnover":
        portfolio = replace(portfolio, daily_turnover=config.max_daily_turnover)
    elif case == "open_orders":
        portfolio = replace(portfolio, open_orders=config.max_open_orders)
    elif case == "broker_orders":
        portfolio = replace(portfolio, open_orders=1, source="t_invest_sandbox")
    elif case == "short":
        intent = replace(intent, side=Side.SELL)
    elif case == "non_short_margin":
        intent = replace(intent, confirm_margin_trade=True)
    elif case == "cover_blocked":
        portfolio = replace(portfolio, positions={"AAA": Position(instrument, -2, 2)})
    elif case == "zero_equity":
        equity = D("0")
    elif case == "critical":
        geo = replace(geo, level=GeoRiskLevel.CRITICAL)
    decision = evaluate_intent(intent, portfolio, config, geo, equity, D("0"))
    assert not decision.allowed
    assert decision.intent is None
    assert decision.reasons


def test_risk_reversal_is_not_a_reducing_exit():
    config = load_config(ROOT / "config/shadow.json")
    instrument = Instrument("AAA", "uid", "TQBR", 1, D("0.01"))
    portfolio = PortfolioSnapshot(D("100000"), {"AAA": Position(instrument, 1)})
    intent = OrderIntent("reverse", instrument, Side.SELL, 2, D("1000"), D("2000"), "test")
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, D("1"), frozenset(), ())
    result = evaluate_intent(intent, portfolio, config, geo, D("100000"), D("1000"))
    assert not result.allowed
    assert "position reversal must close the current side before opening another" in result.reasons
