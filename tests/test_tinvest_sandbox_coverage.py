"""Sandbox HTTP contract failure cases; all transports are local fakes."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from test_tinvest_sandbox import RecordingTransport, _intent

from moex_bot.domain import OrderRecord, OrderStatus
from moex_bot.integrations.tinvest_sandbox import (
    TInvestSandboxAccountService,
    TInvestSandboxExecutionAdapter,
    UrlLibJsonTransport,
    _rub_total,
    _trade_operation,
    money_value_to_decimal,
)


@pytest.mark.parametrize("decoded", [{"ok": True}, []])
def test_native_http_transport_decodes_only_objects(decoded: object) -> None:
    with patch(
        "moex_bot.integrations.tinvest_sandbox.urlopen",
        return_value=io.BytesIO(json.dumps(decoded).encode()),
    ) as opening:

        def call() -> object:
            return UrlLibJsonTransport().post(
                "https://example.test/rest", headers={}, payload={"x": 1}, timeout_seconds=1
            )

        if isinstance(decoded, dict):
            assert call() == decoded
            assert opening.call_args.args[0].method == "POST"
        else:
            with pytest.raises(ValueError):
                call()


@pytest.mark.parametrize(
    "fault",
    [
        "token",
        "timeout",
        "accounts",
        "account",
        "id",
        "name",
        "openid",
        "state",
        "close",
        "money",
        "moneyitem",
        "paynan",
        "paymax",
        "balance",
        "currency",
    ],
)
def test_account_response_validation(fault: str) -> None:
    transport = RecordingTransport()
    service = TInvestSandboxAccountService("token", transport)
    with pytest.raises(ValueError):
        if fault == "token":
            TInvestSandboxAccountService("", transport)
        elif fault == "timeout":
            TInvestSandboxAccountService("token", transport, 0)
        elif fault in {"accounts", "account", "id"}:
            transport.response = {
                "accounts": {} if fault == "accounts" else [None if fault == "account" else {}]
            }
            service.accounts()
        elif fault in {"name", "openid"}:
            service.open_account("" if fault == "name" else "new")
        elif fault == "state":
            service.account_state("")
        elif fault == "close":
            service.close_account("")
        elif fault in {"money", "moneyitem"}:
            transport.response = {"money": {} if fault == "money" else [None]}
            service.available_rub_balance("a")
        else:
            transport.response = {"balance": {"currency": "usd"}} if fault == "currency" else {}
            service.pay_in(
                "a",
                Decimal("NaN")
                if fault == "paynan"
                else Decimal("30000001")
                if fault == "paymax"
                else Decimal(1),
            )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"id": "id", "date": "2026-10-30T10:00:00Z", "type": "OPERATION_TYPE_BUY"},
        {"id": "id", "date": "2026-10-30T10:00:00Z", "tradesInfo": {"trades": {}}},
        {"id": "id", "date": "2026-10-30T10:00:00Z", "tradesInfo": {"trades": [None]}},
        {"id": "id", "date": "2026-10-30T10:00:00Z", "tradesInfo": {"trades": [{"price": []}]}},
    ],
)
def test_operation_rejects_invalid_records(raw: object) -> None:
    with pytest.raises(ValueError):
        _trade_operation(raw, {})


def test_money_and_currency_filters() -> None:
    with pytest.raises(ValueError):
        money_value_to_decimal({"units": "bad"})
    for items in ({}, [None]):
        with pytest.raises(ValueError):
            _rub_total(items, label="test")
    assert _rub_total([{"currency": "usd"}], label="test") == 0
    raw = {
        "id": "id",
        "date": "2026-10-30T10:00:00Z",
        "type": "OPERATION_TYPE_OUTPUT",
        "payment": {"units": "2"},
        "commission": [],
        "tradesInfo": [],
    }
    assert _trade_operation(raw, {}).external_cashflow == -2


@pytest.mark.parametrize(
    "fault",
    ["token", "timeout", "poll", "uuid", "instrument", "uid", "api", "short", "filled", "quantity"],
)
def test_adapter_contract_blocks_invalid_requests_and_results(fault: str) -> None:
    transport = RecordingTransport()
    adapter = TInvestSandboxExecutionAdapter("token", "a", transport)
    intent = _intent()
    with pytest.raises(ValueError):
        if fault == "token":
            TInvestSandboxExecutionAdapter("", "a", transport)
        elif fault == "timeout":
            TInvestSandboxExecutionAdapter("t", "a", transport, timeout_seconds=0)
        elif fault == "poll":
            TInvestSandboxExecutionAdapter("t", "a", transport, poll_interval_seconds=0)
        elif fault == "uuid":
            adapter.submit(replace(intent, order_request_id="bad"))
        elif fault in {"instrument", "uid", "api", "short"}:
            transport.response = (
                {
                    "instrument": {
                        "uid": "wrong" if fault == "uid" else intent.instrument.uid,
                        "apiTradeAvailableFlag": fault != "api",
                        "shortEnabledFlag": False,
                    }
                }
                if fault != "instrument"
                else {}
            )
            adapter.submit(replace(intent, confirm_margin_trade=True))
        else:
            transport.response = {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL"
                if fault == "filled"
                else "unknown",
                "lotsExecuted": "3",
            }
            adapter.submit(intent)


@pytest.mark.parametrize(
    "field,value", [("instrumentUid", "wrong"), ("orderRequestId", "wrong"), ("lotsRequested", 999)]
)
def test_lookup_checks_identity(field: str, value: object) -> None:
    adapter = TInvestSandboxExecutionAdapter("t", "a", RecordingTransport({field: value}))
    with pytest.raises(ValueError, match="mismatched"):
        adapter.lookup(OrderRecord(_intent(), OrderStatus.UNKNOWN))


def test_environment_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fake")
    monkeypatch.setenv("CUSTOM_ACCOUNT", "account")
    assert TInvestSandboxAccountService.from_environment().token == "fake"
    assert (
        TInvestSandboxExecutionAdapter.from_environment(account_id_env="CUSTOM_ACCOUNT").account_id
        == "account"
    )
    assert (
        TInvestSandboxExecutionAdapter.from_environment(account_id_env="CUSTOM_ACCOUNT").mode.value
        == "sandbox"
    )


def test_pagination_and_http_errors() -> None:
    service = TInvestSandboxAccountService("t", RecordingTransport({"items": {}, "hasNext": True}))
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="ordered"):
        service.operations("a", {}, from_time=now, to_time=now)
    with pytest.raises(ValueError, match="unexpected operations"):
        service.operations("a", {}, from_time=now, to_time=now + timedelta(seconds=1))
    service.transport.response = {"hasNext": True}
    with pytest.raises(ValueError, match="cursor did not advance"):
        service.operations("a", {}, from_time=now, to_time=now + timedelta(seconds=1))
    error = HTTPError("https://example.test", 401, "denied", {}, None)
    assert TInvestSandboxExecutionAdapter._error_detail(error) == "HTTPError:HTTP 401"


@pytest.mark.parametrize(
    "fault",
    [
        "account",
        "loading",
        "equity",
        "positions",
        "position",
        "quantity",
        "unknown",
        "fraction",
        "blocked",
        "blockednegative",
        "orders",
        "noprice",
    ],
)
def test_portfolio_snapshot_validation(fault: str) -> None:
    instrument = _intent().instrument
    position = {
        "quantity": {"units": "20"},
        "instrumentUid": instrument.uid,
        "blockedLots": {"units": "0"},
    }
    portfolio = {"totalAmountPortfolio": {"units": "300000"}, "positions": [position]}
    positions, orders = {}, {}
    if fault == "loading":
        positions["limitsLoadingInProgress"] = True
    elif fault == "equity":
        portfolio.pop("totalAmountPortfolio")
    elif fault == "positions":
        portfolio["positions"] = {}
    elif fault == "position":
        portfolio["positions"] = [None]
    elif fault == "quantity":
        position["quantity"] = None
    elif fault == "unknown":
        position["instrumentUid"] = "unknown"
    elif fault == "fraction":
        position["quantity"] = {"units": "11"}
    elif fault == "blocked":
        position["blockedLots"] = []
    elif fault == "blockednegative":
        position["blockedLots"] = {"units": "-1"}
    elif fault == "orders":
        orders["orders"] = {}
    responses = iter((portfolio, positions, orders))
    transport = RecordingTransport()
    with patch.object(transport, "post", side_effect=lambda *a, **k: next(responses)):
        service = TInvestSandboxAccountService("t", transport)
        if fault == "noprice":
            snap = service.broker_snapshot("a", {instrument.uid: instrument})
            assert snap.as_portfolio_payload()["positions"]["SBER"]["lots"] == 2
        else:
            with pytest.raises(ValueError):
                service.broker_snapshot(
                    "" if fault == "account" else "a", {instrument.uid: instrument}
                )


@pytest.mark.parametrize("fault", ["account", "orders", "record", "id", "remaining"])
def test_cancel_reconciliation_invalid_response(fault: str) -> None:
    transport = RecordingTransport(
        {"orders": {} if fault == "orders" else [None if fault == "record" else {}]}
    )
    if fault == "remaining":
        transport.response = {"orders": [{"orderId": "still-active"}]}
    with pytest.raises(ValueError):
        TInvestSandboxAccountService("t", transport).reconcile_cancel_active_orders(
            "" if fault == "account" else "a"
        )


@pytest.mark.parametrize(
    "responses",
    [
        [TimeoutError(), {}, TimeoutError()],
        [{}, TimeoutError()],
        [{}, {}, TimeoutError()],
        [{}, {}, {}],
        [{"lotsExecuted": "3"}],
        [{"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": "0"}],
        [{}, {}, {"lotsExecuted": "3"}],
        [{}, {}, {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": "0"}],
    ],
)
def test_settlement_network_failures_and_invalid_fills(responses: list) -> None:
    record = OrderRecord(_intent(), OrderStatus.ACCEPTED, broker_order_id="broker")
    transport = RecordingTransport()
    values = iter(responses)

    def post(*args: object, **kwargs: object) -> object:
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    invalid = any(
        isinstance(value, dict)
        and (
            value.get("lotsExecuted") == "3"
            or value.get("executionReportStatus") == "EXECUTION_REPORT_STATUS_FILL"
        )
        for value in responses
    )
    with patch.object(transport, "post", side_effect=post):
        adapter = TInvestSandboxExecutionAdapter("t", "a", transport)
        if invalid:
            with pytest.raises(ValueError):
                adapter.settle((record,), ttl_seconds=0)
        else:
            assert adapter.settle((record,), ttl_seconds=0)[0].status is OrderStatus.UNKNOWN


def test_negative_ttl_and_no_pending() -> None:
    adapter = TInvestSandboxExecutionAdapter("t", "a", RecordingTransport())
    with pytest.raises(ValueError):
        adapter.settle((), ttl_seconds=-1)
    assert adapter.settle((), ttl_seconds=0) == ()


def test_fixed_sandbox_lifecycle_reads_and_matching_accounts() -> None:
    transport = RecordingTransport()
    service = TInvestSandboxAccountService("t", transport)
    assert set(service.account_state("sandbox")) == {
        "portfolio",
        "positions",
        "orders",
        "stop_orders",
    }
    service.close_account("sandbox")
    assert transport.call["url"].endswith("/CloseSandboxAccount")
    transport.response = {"accounts": [{"id": "other", "name": "other"}, {"id": "configured"}]}
    assert service.ensure_account("configured", account_name="new").account_id == "configured"
    transport.response = {"money": [{"currency": "usd"}]}
    assert service.available_rub_balance("a") == 0


def test_pagination_bound_is_finite_and_duplicates_are_ignored() -> None:
    transport = RecordingTransport()
    service = TInvestSandboxAccountService("t", transport)
    now = datetime.now(UTC)
    pages = 0

    def post(*args: object, **kwargs: object) -> dict:
        nonlocal pages
        pages += 1
        return {
            "hasNext": True,
            "nextCursor": str(pages),
            "items": [{"id": "duplicate", "date": "2026-10-30T10:00:00Z", "type": "OTHER"}],
        }

    with (
        patch.object(transport, "post", side_effect=post),
        pytest.raises(ValueError, match="100 pages"),
    ):
        service.operations("a", {}, from_time=now, to_time=now + timedelta(seconds=1))
    assert pages == 100


def test_cashflows_sells_zero_and_currency_positions() -> None:
    instrument = _intent().instrument
    for operation_type, expected in [("OPERATION_TYPE_INPUT", 2), ("OPERATION_TYPE_SELL", 0)]:
        operation = _trade_operation(
            {
                "id": "x",
                "date": "2026-10-30T10:00:00Z",
                "type": operation_type,
                "instrumentUid": instrument.uid,
                "payment": {"units": "2"},
            },
            {instrument.uid: instrument},
        )
        assert operation.external_cashflow == expected
    responses = iter(
        [
            {
                "totalAmountPortfolio": {"units": "1"},
                "positions": [
                    {"quantity": {"units": "0"}},
                    {"quantity": {"units": "1"}, "instrumentType": "currency"},
                ],
            },
            {},
            {},
        ]
    )
    transport = RecordingTransport()
    with patch.object(transport, "post", side_effect=lambda *a, **k: next(responses)):
        assert (
            not TInvestSandboxAccountService("t", transport).broker_snapshot("a", {}).positions_lots
        )


def test_submit_http_failure_lookup_success_and_poll_sleep() -> None:
    transport = RecordingTransport()
    adapter = TInvestSandboxExecutionAdapter("t", "a", transport)
    error = HTTPError("https://example.test", 401, "denied", {}, None)
    with patch.object(transport, "post", side_effect=error):
        assert adapter.submit(_intent()).status_detail == "HTTPError:HTTP 401"
    assert adapter.lookup(OrderRecord(_intent(), OrderStatus.UNKNOWN)).status is OrderStatus.UNKNOWN
    responses = iter(
        [{}, {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": "2"}]
    )
    sleeps = []
    adapter.monotonic = lambda: 0
    adapter.sleeper = sleeps.append
    with patch.object(transport, "post", side_effect=lambda *a, **k: next(responses)):
        record = OrderRecord(_intent(), OrderStatus.ACCEPTED, broker_order_id="id")
        assert adapter.settle((record,), ttl_seconds=1)[0].status is OrderStatus.FILLED
    assert sleeps == [1]


def test_valid_lookup_optional_identity_and_reuse_after_unknown_binding() -> None:
    intent = _intent()
    transport = RecordingTransport(
        {
            "instrumentUid": intent.instrument.uid,
            "orderRequestId": intent.order_request_id,
            "lotsRequested": intent.lots,
            "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
            "orderId": "existing",
        }
    )
    adapter = TInvestSandboxExecutionAdapter("t", "a", transport)
    assert adapter.lookup(OrderRecord(intent, OrderStatus.UNKNOWN)).broker_order_id == "existing"
    transport.response = {"accounts": [{"id": "old", "name": "reuse"}]}
    assert (
        TInvestSandboxAccountService("t", transport)
        .ensure_account("lost", account_name="reuse")
        .account_id
        == "old"
    )
    assert (
        TInvestSandboxAccountService("t", transport)
        .ensure_account(None, account_name="reuse")
        .account_id
        == "old"
    )
    raw = {"id": "id", "date": "2026-10-30T10:00:00Z", "payment": []}
    assert _trade_operation(raw, {}).external_cashflow == 0
