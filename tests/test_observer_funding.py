import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.domain import Instrument, OrderIntent, Side
from moex_bot.observer_funding import (
    MAX_LOTS,
    ORDER_PRICE,
    WITHDRAW,
    ObserverFundingGate,
    ObserverFundsReader,
    verified_funding,
)
from moex_bot.prod_long import load_prod_long_policy, prepare_prod_long_sleeve
from moex_bot.reporting import render_shadow_report
from moex_bot.shadow import UniverseEntry, run_hourly_shadow

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


def money(value):
    amount = D(value)
    return {
        "currency": "rub",
        "units": str(int(amount)),
        "nano": int((amount - int(amount)) * 10**9),
    }


class Transport:
    def __init__(self):
        self.calls = []
        self.responses = {
            WITHDRAW: {
                "money": [money("54887.15")],
                "blocked": [money("1000")],
                "blockedGuarantee": [money("35000")],
            },
            MAX_LOTS: {
                "currency": "rub",
                "buyLimits": {"buyMaxLots": "100", "buyMoneyAmount": money("18887.15")},
                "buyMarginLimits": {"buyMaxLots": "10000"},
            },
            ORDER_PRICE: {
                "lotsRequested": "1",
                "initialOrderAmount": money("3000"),
                "totalOrderAmount": money("3001.5"),
            },
        }

    def post(self, url, *, headers, payload, timeout_seconds):
        path = url.removeprefix("https://invest-public-api.tbank.ru/rest")
        self.calls.append((path, copy.deepcopy(payload)))
        assert path in self.responses, "unexpected or mutating API"
        result = self.responses[path]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def evidence_now(account="prod-iis", cash="41302.62"):
    transport = Transport()
    transport.responses[WITHDRAW] = {"money": [money(cash)]}
    return ObserverFundsReader("test", account, transport).snapshot()


def setup_gate(*, budget="18887.15", reserve="1888.715"):
    now = datetime.now(UTC)
    transport = Transport()
    reader = ObserverFundsReader("test-secret", "prod-iis", transport, clock=lambda: now)
    evidence = reader.snapshot()
    return ObserverFundingGate(reader, evidence, D(budget), D(reserve)), transport


def intent(ticker="SBER"):
    return OrderIntent(
        "request-" + ticker,
        Instrument(ticker, "uid-" + ticker, "TQBR", 10, D("0.01")),
        Side.BUY,
        1,
        D("300"),
        D("3000"),
        "test",
    )


def test_guarantee_reduces_sleeve_and_preserves_manual_holdings():
    gate, _ = setup_gate()
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    snapshot = {
        "source": "t_invest_prod",
        "account_id": "prod-iis",
        "cash": "54887.15",
        "positions": {"GAZP": {"lots": 1}},
        "observer_funding": gate.evidence,
    }
    sleeve = prepare_prod_long_sleeve(
        policy=policy, broker_snapshot=snapshot, expected_account_id="prod-iis"
    )
    assert sleeve["cash"] == "18887.15"
    assert sleeve["positions"] == {}
    assert sleeve["observer"]["protected_secids"] == ["GAZP"]
    assert snapshot["positions"] == {"GAZP": {"lots": 1}}


@pytest.mark.parametrize(
    "withdraw,positions,expected",
    [
        ("50000", "60000", "40000"),
        ("1000", "50000", "1000"),
        ("50000", "1000", "1000"),
        ("-10", "1000", "0"),
        ("0", "1000", "0"),
    ],
)
def test_caps_use_lower_broker_cash_without_inventing_funds(withdraw, positions, expected):
    snapshot = {
        "source": "t_invest_prod",
        "account_id": "prod-iis",
        "cash": positions,
        "positions": {},
        "observer_funding": evidence_now(cash=withdraw),
    }
    result = prepare_prod_long_sleeve(
        policy=load_prod_long_policy(ROOT / "config/prod_long.json"),
        broker_snapshot=snapshot,
        expected_account_id="prod-iis",
    )
    assert result["cash"] == expected


@pytest.mark.parametrize("mutation", ["missing", "stale", "future", "account", "nan", "tampered"])
def test_evidence_fails_closed(mutation):
    now = datetime.now(UTC)
    evidence = evidence_now()
    if mutation == "missing":
        evidence = None
    if mutation == "stale":
        evidence["started_at"] = (now - timedelta(minutes=6)).isoformat()
    if mutation == "future":
        evidence["observed_at"] = (now + timedelta(minutes=6)).isoformat()
    if mutation == "account":
        evidence["account_binding"] = "wrong"
    if mutation == "nan":
        evidence["money_rub"] = "NaN"
    if mutation == "tampered":
        evidence["withdraw_conservative_rub"] = "1000000"
    with pytest.raises(ValueError):
        verified_funding(evidence, "prod-iis", datetime.now(UTC))


@pytest.mark.parametrize(
    "field,value",
    [
        ("money", None),
        ("money", [{"currency": "rub", "units": "NaN"}]),
        ("blockedGuarantee", "bad"),
        ("blockedGuarantee", [money("-1")]),
        ("blocked", [money("1"), money("2")]),
    ],
)
def test_invalid_broker_cash_is_not_zero(field, value):
    transport = Transport()
    transport.responses[WITHDRAW][field] = value
    with pytest.raises(ValueError):
        ObserverFundsReader("test", "prod-iis", transport).snapshot()


def test_buy_max_lots_uses_exact_price_and_not_margin():
    gate, transport = setup_gate()
    transport.responses[MAX_LOTS]["buyLimits"]["buyMaxLots"] = "0"
    assert "GetMaxLots" in gate.check(intent())[0]
    assert gate.spent == 0
    assert gate.checks[-1]["status"] == "REJECTED"
    _, request = next(c for c in transport.calls if c[0] == MAX_LOTS)
    assert request == {
        "accountId": "prod-iis",
        "instrumentId": "uid-SBER",
        "price": {"units": "300", "nano": 0},
    }


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"currency": "rub", "buyMarginLimits": {"buyMaxLots": "999"}},
        {"currency": "usd", "buyLimits": {}},
        {"currency": "rub", "buyLimits": {"buyMaxLots": "NaN", "buyMoneyAmount": money("1")}},
        {"currency": "rub", "buyLimits": {"buyMaxLots": "1.5", "buyMoneyAmount": money("1")}},
    ],
)
def test_missing_or_invalid_own_limits_never_fall_back_to_margin(response):
    gate, transport = setup_gate()
    transport.responses[MAX_LOTS] = response
    assert gate.check(intent())
    assert gate.checks[-1]["status"] == "UNAVAILABLE"
    assert gate.spent == 0


@pytest.mark.parametrize("stage", [MAX_LOTS, ORDER_PRICE])
def test_timeout_safe_reason_and_no_credentials(stage):
    gate, transport = setup_gate()
    transport.responses[stage] = TimeoutError("test-secret prod-iis private-details")
    reasons = str(gate.check(intent()))
    assert stage.rsplit("/", 1)[-1] in reasons
    assert "test-secret" not in reasons and "prod-iis" not in reasons
    assert gate.checks[-1]["status"] == "UNAVAILABLE"


def test_cumulative_cash_fees_reserve_and_rejection_does_not_consume_budget():
    gate, transport = setup_gate(budget="7000", reserve="1000")
    assert gate.check(intent()) == ()
    assert gate.spent == D("3001.5")
    assert gate.check(intent("VTBR"))  # 6003 > 6000 although nominal 6000 fits.
    assert gate.spent == D("3001.5")
    transport.responses[ORDER_PRICE]["totalOrderAmount"] = money("3000")
    gate.reserve = D("998.5")
    assert gate.check(intent("T")) == ()
    assert gate.spent == D("6001.5")


def test_current_broker_money_lower_than_snapshot_blocks_buy():
    gate, transport = setup_gate()
    transport.responses[MAX_LOTS]["buyLimits"]["buyMoneyAmount"] = money("1")
    assert gate.check(intent())
    assert gate.checks[-1]["status"] == "REJECTED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("lotsRequested", "2"),
        ("initialOrderAmount", money("2999")),
        ("totalOrderAmount", money("2999")),
        ("totalOrderAmount", {"currency": "usd", "units": "4000"}),
    ],
)
def test_bad_order_price_fails_closed(field, value):
    gate, transport = setup_gate()
    transport.responses[ORDER_PRICE][field] = value
    assert gate.check(intent())
    assert gate.checks[-1]["status"] == "UNAVAILABLE"


def test_no_mutations_or_account_override_and_sell_proceeds_not_used():
    gate, transport = setup_gate()
    with pytest.raises(ValueError):
        gate.reader.read("/PostOrder")
    with pytest.raises(ValueError):
        gate.reader.read(WITHDRAW, {"accountId": "other"})
    assert gate.check(replace(intent(), side=Side.SELL)) == ()
    assert gate.spent == 0
    assert len(transport.calls) == 1  # initial funding only


def test_shadow_integration_persists_funding_and_formats_report(tmp_path):
    from test_shadow import FakeMarketData, _config

    transport = Transport()
    transport.responses[ORDER_PRICE] = {
        "lotsRequested": "25",
        "initialOrderAmount": money("2750"),
        "totalOrderAmount": money("2751"),
    }
    reader = ObserverFundsReader("test", "prod-iis", transport)
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    sleeve = prepare_prod_long_sleeve(
        policy=policy,
        broker_snapshot={
            "source": "t_invest_prod",
            "account_id": "prod-iis",
            "cash": "54887.15",
            "positions": {},
            "observer_funding": reader.snapshot(),
        },
        expected_account_id="prod-iis",
    )
    portfolio, geo, output = [
        tmp_path / name for name in ("portfolio.json", "geo.json", "plan.json")
    ]
    portfolio.write_text(json.dumps(sleeve))
    now = datetime.now(UTC)
    geo.write_text(json.dumps({"feed_observed_at": now.isoformat(), "events": []}))
    config = _config()
    config = replace(
        config, strategy=replace(config.strategy, rebalance_hours_moscow=tuple(range(24)))
    )
    kwargs = dict(
        config=config,
        universe=(UniverseEntry("SBER", "TQBR", "uid", 1, True,
                                "sber", "financials", "domestic", "share"),),
        portfolio_path=portfolio,
        geo_path=geo,
        output_path=output,
        as_of=now,
        market_data=FakeMarketData(),
        observer_reader=reader,
    )
    result = run_hourly_shadow(**kwargs)
    assert len(result.orders) == 1
    assert result.observer_funding["checks"][0]["status"] == "PASSED"
    persisted = json.loads(output.read_text())
    assert persisted["observer_funding"]["execution_allowed"] is False
    report = render_shadow_report(result, now, environment_label="PROD OBSERVER")
    for text in ("НЕ ВЕСЬ ИИС", "ГО:", "GetMaxLots", "План с комиссией", "непрерывный"):
        assert text in report
    transport.responses[MAX_LOTS]["buyLimits"]["buyMaxLots"] = "0"
    result = run_hourly_shadow(**kwargs)
    assert not result.orders and len(result.rejected) == 1
    assert result.observer_funding["planned_total_with_fees_rub"] == "0"
    with pytest.raises(ValueError, match="requires a bound"):
        run_hourly_shadow(**{**kwargs, "observer_reader": None})
