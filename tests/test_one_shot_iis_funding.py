"""Real adapters with scripted read-only responses; no keys, network or live orders."""
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_protected_observer import Transport as SnapshotTransport
from test_tinvest_live import NOW, Transport, _intent, money

from moex_bot import cli
from moex_bot.integrations.tinvest_sandbox import GET_OPERATIONS_BY_CURSOR_PATH, GET_PORTFOLIO_PATH
from moex_bot.prod_long import load_prod_long_policy, prepare_prod_long_sleeve
from moex_bot.prod_long_once_operator import OneShotIisBroker

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


def bound_broker():
    transport = Transport()
    it = replace(_intent(), lots=1, notional=D("1000"))
    transport.responses.update({
        "GetPositions": {"money": [money("7000")]},
        "GetWithdrawLimits": {"money": [money("10000")], "blocked": [money("3000")],
                              "blockedGuarantee": [money("3000")]},
        "GetMaxLots": {"currency": "rub", "buyLimits": {"buyMaxLots": "2",
                                                        "buyMoneyAmount": money("6500")}},
        "GetOrderPrice": {"lotsRequested": "1", "initialOrderAmount": money("1000"),
                          "totalOrderAmount": money("1001")},
    })
    broker = OneShotIisBroker("unused", "iis", transport, {"uid": it.instrument},
                              clock=lambda: NOW, capital_limit=D(40000), reserve_weight=D(".1"))
    return broker, it, transport


def test_exact_limit_includes_fees_reserve_and_counts_overlapping_go_once():
    b, i, t = bound_broker()
    state = b.capture(i)
    assert (state.cash_available, state.estimated_total) == (D(5800), D(1001))
    methods = dict(t.calls)
    assert methods["GetMaxLots"] == {
        "accountId": "iis", "instrumentId": i.instrument.uid, "price": {"units": "100", "nano": 0}}
    assert methods["GetOrderPrice"]["quantity"] == "1"
    assert all(m.startswith("Get") for m, _ in t.calls)
    t.responses["GetMaxLots"]["buyLimits"]["buyMaxLots"] = "0"
    with pytest.raises(ValueError, match="insufficient own-money"):
        b.capture(i)
    assert not any(m in {"PostOrder", "PostStopOrder"} for m, _ in t.calls)


@pytest.mark.parametrize("key,body", [
    ("GetMaxLots", {"currency": "rub", "buyMarginLimits": {"buyMaxLots": "9000"}}),
    ("GetMaxLots", {"currency": "usd", "buyLimits": {}}),
    ("GetPositions", {"money": None}), ("GetWithdrawLimits", {"money": None}),
    ("GetPositions", {"futures": [{"ticker": "KCG7"}]}),
])
def test_live_cash_rejects_incomplete_data_margin_only_and_derivatives(key, body):
    b, i, t = bound_broker()
    t.responses[key] = body
    with pytest.raises(ValueError):
        b.capture(i)
    assert all(m.startswith("Get") for m, _ in t.calls)


@pytest.mark.parametrize("field,value", [
    ("lotsRequested", 1.9), ("lotsRequested", True),
    ("totalOrderAmount", {"currency": "rub", "units": "1000", "nano": 1000000000}),
    ("initialOrderAmount", {"currency": "usd", "units": "1000"}),
])
def test_price_estimate_must_be_well_formed(field, value):
    b, i, t = bound_broker()
    t.responses["GetOrderPrice"][field] = value
    with pytest.raises(ValueError):
        b.capture(i)
    assert all(m.startswith("Get") for m, _ in t.calls)


@pytest.mark.parametrize("change", [{"balance": "10"}, {"blocked": "1"}, {"exchangeBlocked": True}])
def test_positions_disagreement_cannot_add_to_manual_position(change):
    b, i, t = bound_broker()
    t.responses["GetPositions"]["securities"] = [{"instrumentUid": "uid", "balance": "0", **change}]
    with pytest.raises(ValueError, match="held or blocked"):
        b.capture(i)
    assert all(m.startswith("Get") for m, _ in t.calls)


@pytest.mark.parametrize("runtime,projected,future,ok", [
    ("prod_runtime", False, False, True), ("runtime", False, False, False),
    ("prod_runtime", True, False, False), ("prod_runtime", False, True, False),
])
def test_one_shot_snapshot_requires_unprojected_production_path(
    tmp_path, monkeypatch, runtime, projected, future, ok,
):
    t = SnapshotTransport()
    if not future:
        t.responses[GET_PORTFOLIO_PATH]["positions"] = []
        t.responses[GET_OPERATIONS_BY_CURSOR_PATH]["items"] = []
    for mode in ["PROD", "SANDBOX"]:
        monkeypatch.setenv("T_INVEST_" + mode + "_TOKEN", "unused")
        monkeypatch.setenv("T_INVEST_" + mode + "_ACCOUNT_ID", "iis-test")
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: t)
    out = tmp_path / "snap.json"
    policy = ROOT / "config/prod_long.json"
    rc = cli.broker_portfolio_snapshot(
        universe_path=ROOT / "config/universe.json", output_path=out,
        runtime_path=ROOT / f"config/{runtime}.json", services_path=ROOT / "config/services.json",
        observer_policy_path=policy if projected else None, one_shot_funding=True)
    assert rc == (0 if ok else 2)
    assert out.exists() == ok
    if ok:
        raw = json.loads(out.read_bytes())
        assert raw["source"] == "t_invest_prod"
        sleeve = prepare_prod_long_sleeve(policy=load_prod_long_policy(policy), broker_snapshot=raw,
                                         expected_account_id="iis-test")
        assert sleeve["source"] == "t_invest_prod_observer" and D(sleeve["cash"]) == 40000
    if projected or runtime == "runtime":
        assert not t.calls


def test_one_shot_runner_supplies_funding_at_both_plan_steps(tmp_path, monkeypatch):
    script = (ROOT / "scripts/ubuntu/run-long-once-prod.sh").read_text()
    assert "--one-shot-funding" in script and "--observer-policy" not in script
    assert '--report-environment "PROD ONE-SHOT PLAN"' in script
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "iis-test")
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "unused")
    hourly = Mock(side_effect=ValueError("stop after checking production reader"))
    monkeypatch.setattr(cli, "run_hourly_shadow", hourly)
    monkeypatch.setattr(cli.MoexAlgoReadOnlyAdapter, "from_environment", lambda **kw: Mock())
    assert cli.hourly_shadow(
        config_path=ROOT / "config/prod_shadow.json", universe_path=ROOT / "config/universe.json",
        portfolio_path=tmp_path / "portfolio.json", geo_path=tmp_path / "geo.json",
        output_path=tmp_path / "plan.json", as_of=datetime(2026, 11, 13, 12, tzinfo=UTC),
        require_token=False, report_environment="PROD ONE-SHOT PLAN",
    ) == 2
    assert hourly.call_args.kwargs["observer_reader"].account_id == "iis-test"
