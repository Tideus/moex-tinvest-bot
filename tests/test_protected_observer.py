import copy
import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot import cli
from moex_bot.integrations.tinvest_sandbox import (
    GET_INSTRUMENT_PATH,
    GET_OPERATIONS_BY_CURSOR_PATH,
    GET_ORDERS_PATH,
    GET_PORTFOLIO_PATH,
    GET_POSITIONS_PATH,
    POST_ORDER_PATH,
    TInvestSandboxAccountService,
)
from moex_bot.live_operator import read_live_plan
from moex_bot.prod_long import load_prod_long_policy, prepare_prod_long_sleeve
from moex_bot.protected_observer import ProtectedObserverTransport
from moex_bot.service_config import PROD_REST, SANDBOX_REST
from moex_bot.validation import validate_project_configs

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_prod_long_policy(ROOT / "config/prod_long.json")
EXTERNAL = POLICY.protected_external_instruments[0]
FUTURE = {"instrumentUid": EXTERNAL.uid, "instrumentType": "futures",
          "quantity": {"units": "-2", "nano": 0},
          "currentPrice": {"units": "305", "nano": 500_000_000}}


class Transport:
    def __init__(self):
        self.calls = []
        self.responses = {
            GET_INSTRUMENT_PATH: {"instrument": {
                "uid": EXTERNAL.uid, "ticker": EXTERNAL.secid, "instrumentType": "futures"}},
            GET_PORTFOLIO_PATH: {
                "totalAmountPortfolio": {"units": "123456", "nano": 0, "currency": "rub"},
                "positions": [copy.deepcopy(FUTURE)]},
            GET_POSITIONS_PATH: {"money": [{"currency": "rub", "units": "45000", "nano": 0}],
                                 "blocked": [{"currency": "rub", "units": "500", "nano": 0}]},
            GET_ORDERS_PATH: {"orders": [{"orderId": "manual-order"}]},
            GET_OPERATIONS_BY_CURSOR_PATH: {
                "items": [{"id": "external-sale", "instrumentUid": EXTERNAL.uid,
                           "type": "OPERATION_TYPE_SELL", "quantity": "2",
                           "payment": {"currency": "rub", "units": "91", "nano": 0}}],
                "hasNext": False, "nextCursor": "unchanged"},
        }

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return copy.deepcopy(self.responses[url.removeprefix(PROD_REST)])


def wrapper(transport=None):
    return ProtectedObserverTransport(transport or Transport(),
                                     POLICY.protected_external_instruments, "iis-test")


def read(wrapped, path, **kwargs):
    return wrapped.post(PROD_REST + path, headers={}, payload={"accountId": "iis-test"},
                        timeout_seconds=1, **kwargs)


def test_external_futures_preserve_account_totals_orders_raw_records_and_never_enter_sleeve():
    transport = Transport()
    projected = wrapper(transport)
    service = TInvestSandboxAccountService("not-a-secret", projected)
    snapshot = service.broker_snapshot(
        "iis-test", {}, base_url=PROD_REST, portfolio_path=GET_PORTFOLIO_PATH,
        positions_path=GET_POSITIONS_PATH, orders_path=GET_ORDERS_PATH, source="t_invest_prod")
    operations = service.operations(
        "iis-test", {}, base_url=PROD_REST, operations_path=GET_OPERATIONS_BY_CURSOR_PATH,
        from_time=datetime(2026, 11, 10, tzinfo=UTC),
        to_time=datetime(2026, 11, 11, tzinfo=UTC))
    assert operations == ()
    assert snapshot.reported_equity == Decimal("123456")
    assert snapshot.cash_available == Decimal("45000")
    assert snapshot.cash_blocked == Decimal("500")
    assert snapshot.open_orders == 1
    assert snapshot.positions_lots == {}
    payload = snapshot.as_portfolio_payload(daily_turnover=Decimal(0))
    projected.annotate(payload)
    assert projected.positions[0]["broker_record"] == FUTURE
    assert "current_value" not in projected.positions[0]
    assert projected.operations[0]["broker_record"]["payment"]["units"] == "91"
    assert payload["source"] == "t_invest_prod_observer_raw"
    sleeve = prepare_prod_long_sleeve(policy=POLICY, broker_snapshot=payload,
                                      expected_account_id="iis-test")
    assert sleeve["positions"] == {}
    assert sleeve["cash"] == "40000"
    assert sleeve["account_reported_equity"] == "123456"
    assert sleeve["execution_allowed"] is False
    assert sleeve["source"] == "t_invest_prod_observer_external"
    assert "KCG7" not in POLICY.allowed_secids | POLICY.managed_secids
    assert len([url for url, _ in transport.calls if url.endswith(GET_INSTRUMENT_PATH)]) == 1


@pytest.mark.parametrize("url,account", [
    (PROD_REST + POST_ORDER_PATH, "iis-test"),
    (SANDBOX_REST + GET_PORTFOLIO_PATH, "iis-test"),
    (PROD_REST + GET_PORTFOLIO_PATH, "other-account"),
    ("https://untrusted.example/rest" + GET_PORTFOLIO_PATH, "iis-test"),
])
def test_observer_forbids_mutations_wrong_account_and_environment(url, account):
    transport = Transport()
    with pytest.raises(ValueError, match="only bound production"):
        wrapper(transport).post(url, headers={}, payload={"accountId": account}, timeout_seconds=1)
    assert transport.calls == []


@pytest.mark.parametrize("field,value", [("uid", "unknown"), ("ticker", "SBER"),
                                         ("instrumentType", "share")])
def test_instrument_metadata_must_match(field, value):
    transport = Transport()
    transport.responses[GET_INSTRUMENT_PATH]["instrument"][field] = value
    with pytest.raises(ValueError, match="identity mismatch"):
        read(wrapper(transport), GET_PORTFOLIO_PATH)


def test_unknown_nonzero_position_still_blocks_strict_parser():
    transport = Transport()
    transport.responses[GET_PORTFOLIO_PATH]["positions"].append({
        **FUTURE, "instrumentUid": "unknown-future"})
    service = TInvestSandboxAccountService("unused", wrapper(transport))
    with pytest.raises(ValueError, match="outside verified universe"):
        service.broker_snapshot("iis-test", {}, base_url=PROD_REST,
                                portfolio_path=GET_PORTFOLIO_PATH,
                                positions_path=GET_POSITIONS_PATH, orders_path=GET_ORDERS_PATH)


def test_pagination_and_unrelated_operations_retained():
    transport = Transport()
    data = transport.responses[GET_OPERATIONS_BY_CURSOR_PATH]
    data["hasNext"] = True
    data["items"].append({"id": "cash-flow", "type": "OPERATION_TYPE_INPUT"})
    result = read(wrapper(transport), GET_OPERATIONS_BY_CURSOR_PATH)
    assert result == {"items": [{"id": "cash-flow", "type": "OPERATION_TYPE_INPUT"}],
                      "hasNext": True, "nextCursor": "unchanged"}


@pytest.mark.parametrize("change", ["tradable", "unprotected", "duplicate", "type", "uid"])
def test_policy_rejects_unsafe_external_mapping(tmp_path, change):
    raw = json.loads((ROOT / "config/prod_long.json").read_text())
    item = raw["protected_external_instruments"][0]
    if change == "tradable":
        raw["allowed_secids"][0] = "KCG7"
    elif change == "unprotected":
        raw["protected_secids"].remove("KCG7")
    elif change == "duplicate":
        raw["protected_external_instruments"].append(item.copy())
    elif change == "type":
        item["instrument_type"] = "share"
    else:
        item["uid"] = "invalid"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_prod_long_policy(path)


def test_cli_observer_policy_rejects_sandbox_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "unused")
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "sandbox-test")
    monkeypatch.setattr(cli.UrlLibJsonTransport, "post",
                        lambda *a, **k: pytest.fail("must not reach network"))
    assert cli.broker_portfolio_snapshot(
        universe_path=ROOT / "config/universe.json", output_path=tmp_path / "snapshot.json",
        runtime_path=ROOT / "config/runtime.json", services_path=ROOT / "config/services.json",
        observer_policy_path=ROOT / "config/prod_long.json") == 2
    assert not (tmp_path / "snapshot.json").exists()


@pytest.mark.parametrize("source", ["t_invest_prod_observer_raw",
                                    "t_invest_prod_observer_external"])
def test_external_projection_cannot_be_used_as_live_plan(tmp_path, source):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"quality": {"passed": True}, "portfolio_input": {
        "source": source, "account_id": "iis-test"}}))
    with pytest.raises(ValueError, match="cannot be submitted to production"):
        read_live_plan(path, 0, datetime(2026, 11, 10, tzinfo=UTC), "iis-test")


def test_project_validation_only_exempts_declared_external_protected_instruments(tmp_path):
    for name in ("config", "examples"):
        shutil.copytree(ROOT / name, tmp_path / name)
    assert "config/prod_long.json" in validate_project_configs(tmp_path)
    path = tmp_path / "config/prod_long.json"
    raw = json.loads(path.read_text())
    raw["protected_secids"].append("UNKNOWN")
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="must belong to verified universe"):
        validate_project_configs(tmp_path)
    raw["protected_secids"].remove("UNKNOWN")
    raw["protected_external_instruments"][0]["secid"] = "GAZP"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="must not enter trading universe"):
        validate_project_configs(tmp_path)


def test_cli_production_observer_persists_external_records_without_trading(tmp_path, monkeypatch):
    transport = Transport()
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "unused")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "iis-test")
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: transport)
    output = tmp_path / "snapshot.json"
    assert cli.broker_portfolio_snapshot(
        universe_path=ROOT / "config/universe.json", output_path=output,
        runtime_path=ROOT / "config/prod_runtime.json", services_path=ROOT / "config/services.json",
        observer_policy_path=ROOT / "config/prod_long.json") == 0
    payload = json.loads(output.read_text())
    assert payload["protected_external_positions"][0]["secid"] == "KCG7"
    assert payload["source"] == "t_invest_prod_observer_raw"
    assert payload["positions"] == {}
    assert payload["daily_turnover"] == "0"
    assert len(payload["protected_external_operations"]) == 1
    assert all("Get" in url for url, _ in transport.calls)


@pytest.mark.parametrize("field,value", [("secid", "UNKNOWN"), ("protected", False),
                                         ("execution_allowed", True)])
def test_sleeve_rejects_unbound_external_records(field, value):
    projected = wrapper()
    read(projected, GET_PORTFOLIO_PATH)
    payload = {"account_id": "iis-test", "positions": {}, "cash": "40000"}
    projected.annotate(payload)
    projected.positions[0][field] = value
    with pytest.raises(ValueError, match="unclassified external"):
        prepare_prod_long_sleeve(policy=POLICY, broker_snapshot=payload,
                                 expected_account_id="iis-test")
