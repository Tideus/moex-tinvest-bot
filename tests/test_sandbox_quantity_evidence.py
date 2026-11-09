"""2026-11-09 GAZP: sandbox reported 80 shares as lotsExecuted for 8 lots."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.execution_journal import ExecutionJournal
from moex_bot.integrations.tinvest_sandbox import TInvestSandboxExecutionAdapter
from moex_bot.intraday import IntradayStateStore, build_intraday_plan
from moex_bot.intraday_config import load_intraday_config
from moex_bot.sandbox_execution import reconcile_execution_journal


def intent(side: Side = Side.SELL) -> OrderIntent:
    return OrderIntent(
        "94be6407-8394-46f6-ae30-eda37b9b0feb",
        Instrument("GAZP", "gazp-uid", "TQBR", 10, Decimal(".01"), short_enabled=True),
        side, 8, Decimal("113.86"), Decimal("9108.80"), "signal",
    )


def money(value: str) -> dict[str, object]:
    amount = Decimal(value)
    return {"currency": "rub", "units": str(int(amount)),
            "nano": int((amount - int(amount)) * 10**9)}


class EvidenceTransport:
    def __init__(self, order: OrderIntent) -> None:
        at = "2026-11-09T09:10:46.065930Z"
        side = order.side.value.upper()
        self.calls: list[str] = []
        self.state: dict[str, Any] = {
            "orderRequestId": order.order_request_id, "instrumentUid": order.instrument.uid,
            "orderId": "exchange-order", "direction": f"ORDER_DIRECTION_{side}",
            "lotsRequested": "8", "lotsExecuted": "80", "orderDate": at,
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
            "totalOrderAmount": money("9109.60"), "initialSecurityPrice": money("113.87"),
        }
        self.operation: dict[str, Any] = {
            "brokerAccountId": "sandbox", "instrumentUid": order.instrument.uid,
            "date": at, "type": f"OPERATION_TYPE_{side}",
            "state": "OPERATION_STATE_EXECUTED", "quantity": "80", "quantityDone": "80",
            "quantityRest": "0", "price": money("113.87"),
            "payment": money("9109.60" if order.side is Side.SELL else "-9109.60"),
            "tradesInfo": {"trades": [{"num": "trade", "date": at, "quantity": "80",
                                       "price": money("113.87")}]},
        }
        self.response: dict[str, Any] = {"hasNext": False, "items": [self.operation]}
        self.timeout = False

    def post(self, url: str, **kwargs: Any) -> dict[str, Any]:
        assert url.startswith("https://sandbox-invest-public-api.tbank.ru/rest/")
        assert kwargs["payload"]["accountId"] == "sandbox"
        self.calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("GetSandboxOperationsByCursor"):
            assert kwargs["payload"]["withoutTrades"] is False
            if self.timeout:
                raise TimeoutError("read only")
            return self.response
        assert self.calls[-1] in {"GetSandboxOrderState", "GetOrderState", "PostOrder"}
        return self.state


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
@pytest.mark.parametrize("method", ["lookup", "settle", "submit"])
def test_full_shares_require_independent_evidence(side: Side, method: str) -> None:
    order = intent(side)
    transport = EvidenceTransport(order)
    adapter = TInvestSandboxExecutionAdapter("secret", "sandbox", transport)
    record = OrderRecord(order, OrderStatus.ACCEPTED, broker_order_id="exchange-order")
    if method == "lookup":
        result = adapter.lookup(record)
    elif method == "settle":
        result = adapter.settle((record,), ttl_seconds=0)[0]
    else:
        result = adapter.submit(order)
    assert result.status is OrderStatus.FILLED and result.filled_lots == 8
    assert result.status_detail == "sandbox_units_reconciled_with_operations"
    assert transport.calls.count("PostOrder") == (1 if method == "submit" else 0)
    assert "CancelSandboxOrder" not in transport.calls


@pytest.mark.parametrize(("section", "field", "value"), [
    ("state", "lotsExecuted", "79"), ("state", "lotsExecuted", "90"),
    ("state", "executionReportStatus", "EXECUTION_REPORT_STATUS_PARTIALLYFILL"),
    ("state", "instrumentUid", "other"), ("state", "orderRequestId", "other"),
    ("state", "orderId", "other"), ("state", "direction", "ORDER_DIRECTION_BUY"),
    ("state", "lotsRequested", "9"), ("state", "orderDate", "bad"),
    ("state", "orderDate", "2026-11-09T09:10:46"),
    ("state", "totalOrderAmount", money("9110")),
    ("state", "initialSecurityPrice", money("113")),
    ("operation", "brokerAccountId", "other"), ("operation", "instrumentUid", "other"),
    ("operation", "type", "OPERATION_TYPE_BUY"),
    ("operation", "state", "OPERATION_STATE_CANCELED"),
    ("operation", "quantity", "81"), ("operation", "quantityDone", "70"),
    ("operation", "quantityRest", "1"), ("operation", "payment", money("-9109.60")),
    ("operation", "price", money("0")), ("operation", "tradesInfo", {}),
    ("operation", "date", "2026-11-09T09:10:46.065931Z"),
    ("response", "hasNext", True), ("response", "items", []),
])
def test_conflicting_evidence_keeps_account_locked(
    tmp_path: Path, section: str, field: str, value: Any,
) -> None:
    journal = ExecutionJournal(tmp_path / "journal.sqlite3")
    original = intent()
    request = journal.reserve("sandbox", original, "p1", datetime.now(UTC), residual_exit=False)
    order = replace(original, order_request_id=str(request))
    journal.record("sandbox", OrderRecord(order, OrderStatus.ACCEPTED,
                                          broker_order_id="exchange-order"))
    transport = EvidenceTransport(order)
    getattr(transport, section)[field] = value
    with pytest.raises(ValueError):
        reconcile_execution_journal(journal.path,
                                    TInvestSandboxExecutionAdapter("t", "sandbox", transport))
    assert journal.unresolved("sandbox")
    assert "PostOrder" not in transport.calls and "CancelSandboxOrder" not in transport.calls


@pytest.mark.parametrize("mode", ["duplicate", "missing", "timeout", "trade_units"])
def test_incomplete_evidence_never_guesses(mode: str) -> None:
    order = intent()
    transport = EvidenceTransport(order)
    if mode == "duplicate":
        transport.response["items"].append(deepcopy(transport.operation))
    elif mode == "missing":
        del transport.state["orderRequestId"]
    elif mode == "timeout":
        transport.timeout = True
    else:
        transport.operation["tradesInfo"]["trades"][0]["quantity"] = "8"
    with pytest.raises((ValueError, TimeoutError)):
        TInvestSandboxExecutionAdapter("t", "sandbox", transport).lookup(
            OrderRecord(order, OrderStatus.ACCEPTED, broker_order_id="exchange-order"))
    assert "PostOrder" not in transport.calls


def test_restart_reconciles_once_and_same_plan_cannot_resubmit(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = ExecutionJournal(path)
    original = intent()
    at = datetime.now(UTC)
    request = journal.reserve("sandbox", original, "p1", at, residual_exit=False)
    order = replace(original, order_request_id=str(request))
    journal.record("sandbox", OrderRecord(order, OrderStatus.ACCEPTED,
                                          broker_order_id="exchange-order"))
    transport = EvidenceTransport(order)
    adapter = TInvestSandboxExecutionAdapter("t", "sandbox", transport)
    assert reconcile_execution_journal(path, adapter) == 0
    calls = list(transport.calls)
    assert reconcile_execution_journal(path, adapter) == 0
    assert transport.calls == calls
    assert ExecutionJournal(path).reserve("sandbox", original, "p1", at,
                                         residual_exit=False) is None
    assert "PostOrder" not in calls and "CancelSandboxOrder" not in calls


def test_normal_lots_and_partial_fills_do_not_use_operations() -> None:
    order = intent()
    transport = EvidenceTransport(order)
    transport.state.update(lotsExecuted="3",
                           executionReportStatus="EXECUTION_REPORT_STATUS_PARTIALLYFILL")
    result = TInvestSandboxExecutionAdapter("t", "sandbox", transport).lookup(
        OrderRecord(order, OrderStatus.ACCEPTED, broker_order_id="exchange-order"))
    assert result.filled_lots == 3 and result.status is OrderStatus.PARTIALLY_FILLED
    assert transport.calls == ["GetSandboxOrderState"]


def test_force_flat_closes_observed_long_and_short_without_reopening(tmp_path: Path) -> None:
    config = load_intraday_config(Path(__file__).resolve().parents[1] / "config/intraday.json")
    instruments = {
        "GAZP": intent().instrument,
        "PLZL": Instrument("PLZL", "plzl-uid", "TQBR", 1, Decimal(".1")),
    }
    portfolio: dict[str, Any] = {
        "source": "t_invest_sandbox", "account_id": "sandbox", "cash": "300000",
        "reported_equity": "300000", "open_orders": 0,
        "positions": {"GAZP": {"lots": -8, "blocked_lots": 0, "current_value": "-9109.6"},
                      "PLZL": {"lots": 9, "blocked_lots": 0, "current_value": "10011.6"}},
    }
    store = IntradayStateStore(tmp_path / "strategy.sqlite3")
    at = datetime(2026, 11, 9, 15, 40, tzinfo=UTC)
    plan = build_intraday_plan(config=config, instruments=instruments, portfolio=portfolio,
                               store=store, bars=(), as_of=at)
    assert plan.quality_passed and plan.phase == "force_flat"
    assert {(x.instrument.secid, x.side, x.lots) for x in plan.orders} == {
        ("GAZP", Side.BUY, 8), ("PLZL", Side.SELL, 9),
    }
    assert all(x.order_type == "market" and not x.confirm_margin_trade for x in plan.orders)
    # A subsequent broker snapshot, not an assumed fill, removes positions.
    portfolio["positions"] = {}
    repeated = build_intraday_plan(config=config, instruments=instruments, portfolio=portfolio,
                                   store=store, bars=(), as_of=at)
    assert repeated.orders == ()
