"""Mixed IIS regression. All account data and all broker methods here are fakes."""

import copy
import hashlib
import json
from decimal import Decimal
from unittest.mock import Mock

import pytest
from test_long_auto_signal import decision as decision
from test_observer_funding import evidence_now
from test_one_shot_iis_funding import ROOT, bound_broker
from test_prod_long_once import case as case
from test_protected_observer import FUTURE, POLICY, Transport
from test_tinvest_live import money

from moex_bot import cli
from moex_bot.integrations.tinvest_sandbox import (
    GET_OPERATIONS_BY_CURSOR_PATH,
    GET_ORDERS_PATH,
    GET_POSITIONS_PATH,
)
from moex_bot.live_operator import read_live_plan
from moex_bot.live_protection import NativeProtectionBroker
from moex_bot.long_auto_signal import select_candidate
from moex_bot.observer_funding import WITHDRAW
from moex_bot.one_shot_manual_future import KCG7, RAW_SOURCE, SLEEVE_SOURCE
from moex_bot.prod_long import prepare_prod_long_sleeve
from moex_bot.prod_long_once import run_once


def balances(n):
    return {"instrumentUid": KCG7.uid, "ticker": "KCG7", "balance": str(n), "blocked": "0"}


def mixed(n=-2):
    b, i, t = bound_broker()
    b.protected_futures = POLICY.protected_external_instruments
    t.responses["GetPortfolio"]["positions"] = [dict(FUTURE, quantity={"units": str(n)})]
    t.responses["GetPositions"]["futures"] = [balances(n)]
    t.responses["GetStopOrders"] = {
        "stopOrders": [{"instrumentUid": KCG7.uid, "stopOrderId": "manual"}]
    }

    def read(url, **kw):
        if url.endswith("/GetInstrumentBy") and kw["payload"]["id"] == KCG7.uid:
            t.calls.append(("GetInstrumentBy", kw["payload"]))
            return {"instrument": {"uid": KCG7.uid, "ticker": "KCG7", "instrumentType": "futures"}}
        return t.post(url, **kw)

    b.transport = Mock(post=read)
    return b, i, t


@pytest.mark.parametrize("n", [-2, 3])
def test_whole_iis_risk_and_manual_stop_survive_equity_reconciliation(n):
    b, i, t = mixed(n)
    before = copy.deepcopy(t.responses)
    state = b.capture(i)
    assert state.positions == {} and state.stop_uids == {KCG7.uid}
    assert state.equity == 160000 and state.cash_available == 5800
    assert state.estimated_total == 1001 and t.responses == before
    assert all(method.startswith("Get") for method, _ in t.calls)


@pytest.mark.parametrize(
    "damage",
    [
        "one_sided_position",
        "one_sided_portfolio",
        "other_future",
        "duplicate",
        "different_sign",
        "blocked",
        "portfolio_blocked",
        "blocked_lots",
        "fractional",
        "unavailable_go",
        "options",
        "metadata",
        "account",
        "as_security",
        "position_uid",
        "off_policy",
        "in_universe",
        "aggregate_only",
        "loading",
        "missing_balance",
        "unrelated_class",
        "portfolio_type",
    ],
)
def test_ambiguous_mixed_iis_never_admits_a_buy(damage):
    b, i, t = mixed()
    p, s = t.responses["GetPortfolio"], t.responses["GetPositions"]
    row = s["futures"][0]
    if damage == "one_sided_position":
        s["futures"] = []
    elif damage == "one_sided_portfolio":
        p["positions"] = []
    elif damage == "other_future":
        p["positions"][0]["instrumentUid"] = row["instrumentUid"] = "other"
    elif damage == "duplicate":
        s["futures"].append(dict(row))
    elif damage == "different_sign":
        row["balance"] = "2"
    elif damage == "blocked":
        row["blocked"] = "1"
    elif damage == "portfolio_blocked":
        p["positions"][0]["blocked"] = True
    elif damage == "blocked_lots":
        p["positions"][0]["blockedLots"] = {"units": "1"}
    elif damage == "fractional":
        p["positions"][0]["quantity"]["nano"] = 1
    elif damage == "unavailable_go":
        del t.responses["GetWithdrawLimits"]["blockedGuarantee"]
    elif damage == "options":
        s["options"] = [{"instrumentUid": "option"}]
    elif damage == "metadata":
        b.transport = t  # fake now returns SBER for both lookups
    elif damage == "account":
        s["accountId"] = "different-account"
    elif damage == "as_security":
        s["securities"] = [row]
    elif damage == "position_uid":
        row["positionUid"], p["positions"][0]["positionUid"] = "A", "B"
    elif damage == "off_policy":
        b.protected_futures = ()
    elif damage == "in_universe":
        b.instruments_by_uid[KCG7.uid] = i.instrument
    elif damage == "aggregate_only":
        s["futures"], p["positions"], p["totalAmountFutures"] = [], [], money("50")
    elif damage == "loading":
        s["limitsLoadingInProgress"] = True
    elif damage == "missing_balance":
        del row["balance"]
    elif damage == "unrelated_class":
        row["classCode"] = "TQBR"
    elif damage == "portfolio_type":
        p["positions"][0]["instrumentType"] = "currency"
    with pytest.raises(ValueError):
        b.capture(i)
    assert all(method.startswith("Get") for method, _ in t.calls)


@pytest.mark.parametrize("damage", [None, "no_go", "wrong_qty", "manual_trade"])
def test_raw_snapshot_to_sleeve_retains_contracts_but_not_as_spendable_cash(
    damage,
    monkeypatch,
    tmp_path,
):
    t = Transport()
    t.responses[GET_POSITIONS_PATH]["futures"] = [balances(-2 if damage != "wrong_qty" else 2)]
    if damage != "no_go":
        t.responses[WITHDRAW]["blockedGuarantee"] = [money("7000")]
    if damage != "manual_trade":
        t.responses[GET_OPERATIONS_BY_CURSOR_PATH]["items"] = []
    else:
        t.responses[GET_OPERATIONS_BY_CURSOR_PATH]["items"][0]["date"] = "2026-11-13T07:00:00Z"
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "iis-test")
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "offline")
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: t)
    dest = tmp_path / "raw.json"
    rc = cli.broker_portfolio_snapshot(
        universe_path=ROOT / "config/universe.json",
        runtime_path=ROOT / "config/prod_runtime.json",
        services_path=ROOT / "config/services.json",
        output_path=dest,
        one_shot_funding=True,
    )
    if damage:
        assert rc != 0 and not dest.exists()
        return
    raw = json.loads(dest.read_bytes())
    sleeve = prepare_prod_long_sleeve(
        policy=POLICY, broker_snapshot=raw, expected_account_id="iis-test"
    )
    assert raw["reported_equity"] == "123456" and raw["source"] == RAW_SOURCE
    assert sleeve["manual_kcg7_contracts"] == -2 and Decimal(sleeve["cash"]) == 38000
    assert sleeve["positions"] == {} and sleeve["open_orders"] == len(
        t.responses[GET_ORDERS_PATH]["orders"]
    )
    assert sleeve["source"] == SLEEVE_SOURCE
    with pytest.raises(ValueError):
        prepare_prod_long_sleeve(
            policy=POLICY,
            broker_snapshot=dict(raw, manual_kcg7_contracts=True),
            expected_account_id="iis-test",
        )


def bound_mixed_case(case):
    b, _, t = mixed()
    c = case["candidate"]
    b.account_id, b.instruments_by_uid = c.account_id, {c.instrument.uid: c.instrument}
    t.responses["GetAccounts"]["accounts"][0]["id"] = c.account_id
    t.responses["GetInstrumentBy"]["instrument"]["uid"] = c.instrument.uid
    for key in ("GetOrderBook", "GetTradingStatus", "PostOrder"):
        t.responses[key]["instrumentUid"] = c.instrument.uid
    parent = dict(
        t.responses["PostOrder"],
        lotsRequested="1",
        lotsExecuted="1",
        direction="ORDER_DIRECTION_BUY",
        orderRequestId=c.order_request_id,
    )
    t.responses["PostOrder"] = t.responses["GetOrderState"] = parent
    return b, t, c, parent


def test_second_capture_detects_changed_kcg7_before_dispatch(case):
    b, t, _, _ = bound_mixed_case(case)
    old = b.transport.post

    def changed(url, **kw):
        if url.endswith("/GetPositions") and any(m == "GetPositions" for m, _ in t.calls):
            t.responses["GetPositions"]["futures"][0]["balance"] = "0"
        return old(url, **kw)

    b.transport = Mock(post=changed)
    case.update(broker=b)
    with pytest.raises(ValueError, match="quantities disagree"):
        run_once(**case)
    assert not (case["claim_directory"] / "buy-dispatch.json").exists()
    assert all(m.startswith("Get") for m, _ in t.calls)


@pytest.mark.parametrize("typ", ["OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL_MARGIN"])
def test_manual_futures_trade_in_current_turnover_window_blocks_live_capture(typ):
    b, i, t = mixed()
    t.responses["GetOperationsByCursor"]["items"] = [
        {"type": typ, "id": "manual-trade", "instrumentUid": KCG7.uid, "payment": money("1")}
    ]
    with pytest.raises(ValueError, match="external trade"):
        b.capture(i)
    assert all(m.startswith("Get") for m, _ in t.calls)


@pytest.mark.parametrize(
    "source,valid",
    [
        (SLEEVE_SOURCE, True),
        ("t_invest_prod_observer_external", False),
    ],
)
def test_only_dedicated_selector_accepts_reconciled_sleeve(decision, source, valid):
    account = decision["account_id"]
    ts = decision["now"].isoformat()
    funds = dict(
        evidence_now(account, "9000"),
        started_at=ts,
        observed_at=ts,
        guarantee_rub="100",
        withdraw_conservative_rub="8900",
    )
    plan = json.loads(decision["plan"].read_bytes())
    sleeve = json.loads(decision["portfolio"].read_bytes())
    sleeve.update(source=source, manual_kcg7_contracts=-2, cash="8900", observer_funding=funds)
    decision["portfolio"].write_text(json.dumps(sleeve))
    plan["portfolio_input"]["source"] = source
    plan["portfolio_input_sha256"] = hashlib.sha256(decision["portfolio"].read_bytes()).hexdigest()
    decision["plan"].write_text(json.dumps(plan))
    decision["environment"] = "prod-preview"
    if valid:
        candidate = select_candidate(**decision)
        assert candidate and candidate.instrument.secid == "SBER"
    else:
        with pytest.raises(ValueError, match="provenance"):
            select_candidate(**decision)
    with pytest.raises(ValueError):
        read_live_plan(decision["plan"], 0, decision["now"], account)


def test_one_lot_and_native_stop_only_on_equity_with_manual_coffee_stop(case):
    b, t, c, parent = bound_mixed_case(case)
    old = b.transport.post

    def scripted(url, **kw):
        method = url.rsplit("/", 1)[-1]
        if method == "PostOrder":
            parent["orderRequestId"] = kw["payload"]["orderId"]
            # Broker starts reporting the equity only AFTER receiving the bound fake BUY.
            t.responses["GetPositions"]["securities"] = [
                {"instrumentUid": c.instrument.uid, "balance": "10", "blocked": "0"}
            ]
        if method == "PostStopOrder":
            q = kw["payload"]
            t.calls.append((method, q))
            t.responses["GetStopOrders"]["stopOrders"].append(
                {
                    "stopOrderId": "fake-stop",
                    "instrumentUid": q["instrumentId"],
                    "lotsRequested": q["quantity"],
                    "direction": q["direction"],
                    "currency": "rub",
                    "orderType": q["stopOrderType"],
                    "status": "STOP_ORDER_STATUS_ACTIVE",
                    "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET",
                    "stopPrice": {**q["stopPrice"], "currency": "rub"},
                }
            )
            return {"stopOrderId": "fake-stop", "orderRequestId": q["orderId"]}
        return old(url, **kw)

    b.transport = Mock(post=scripted)
    case.update(broker=b, stops=NativeProtectionBroker("offline", c.account_id, b.transport))
    assert run_once(**case)["state"] == "FILLED_PROTECTED"
    assert run_once(**case)["state"] == "ALREADY_CLAIMED"
    assert [
        (m, q["instrumentId"], q["quantity"]) for m, q in t.calls if not m.startswith("Get")
    ] == [("PostOrder", c.instrument.uid, "1"), ("PostStopOrder", c.instrument.uid, "1")]
    assert t.responses["GetStopOrders"]["stopOrders"][0]["stopOrderId"] == "manual"
    assert t.responses["GetPositions"]["futures"] == [balances(-2)]
