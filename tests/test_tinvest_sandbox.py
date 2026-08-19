from decimal import Decimal
from typing import Any
from uuid import uuid4

from moex_bot.domain import Instrument, OrderIntent, OrderStatus, Side
from moex_bot.integrations.tinvest_sandbox import (
    SANDBOX_BASE_URL,
    TInvestSandboxAccountService,
    TInvestSandboxExecutionAdapter,
    decimal_to_quotation,
)


class RecordingTransport:
    def __init__(self, response: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.response = response or {}
        self.fail = fail
        self.call: dict[str, Any] | None = None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.call = {"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds}
        if self.fail:
            raise TimeoutError
        return self.response


def _intent() -> OrderIntent:
    instrument = Instrument(
        "SBER",
        "e6123145-9665-43e0-8413-cd61b8aa9b13",
        "TQBR",
        10,
        Decimal("0.01"),
    )
    return OrderIntent(
        str(uuid4()),
        instrument,
        Side.BUY,
        2,
        Decimal("312.34"),
        Decimal("6246.80"),
        "contract test",
    )


def test_decimal_to_quotation_preserves_nanos() -> None:
    assert decimal_to_quotation(Decimal("312.345678901")) == {
        "units": "312",
        "nano": 345678901,
    }


def test_sandbox_contract_uses_lots_uid_uuid_and_no_margin() -> None:
    transport = RecordingTransport(
        {"executionReportStatus": "EXECUTION_REPORT_STATUS_NEW", "orderId": "broker-1"}
    )
    adapter = TInvestSandboxExecutionAdapter("sandbox-token", "sandbox-account", transport)
    record = adapter.submit(_intent())
    assert record.status is OrderStatus.ACCEPTED
    assert record.broker_order_id == "broker-1"
    assert transport.call is not None
    assert transport.call["url"].startswith(SANDBOX_BASE_URL)
    assert "sandbox-invest-public-api.tbank.ru" in transport.call["url"]
    payload = transport.call["payload"]
    assert payload["quantity"] == "2"
    assert payload["instrumentId"] == "e6123145-9665-43e0-8413-cd61b8aa9b13"
    assert payload["confirmMarginTrade"] is False
    assert payload["orderType"] == "ORDER_TYPE_LIMIT"


def test_timeout_becomes_unknown_without_blind_retry() -> None:
    transport = RecordingTransport(fail=True)
    adapter = TInvestSandboxExecutionAdapter("sandbox-token", "sandbox-account", transport)
    record = adapter.submit(_intent())
    assert record.status is OrderStatus.UNKNOWN
    assert transport.call is not None


def test_mandatory_flat_market_order_omits_limit_price() -> None:
    transport = RecordingTransport(
        {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": "2"}
    )
    base = _intent()
    intent = OrderIntent(
        base.order_request_id,
        base.instrument,
        base.side,
        base.lots,
        base.limit_price,
        base.notional,
        "mandatory flat",
        order_type="market",
    )
    record = TInvestSandboxExecutionAdapter("token", "account", transport).submit(intent)
    assert record.status is OrderStatus.FILLED
    assert transport.call is not None
    assert transport.call["payload"]["orderType"] == "ORDER_TYPE_MARKET"
    assert "price" not in transport.call["payload"]


def test_intraday_reconciliation_cancels_and_verifies_all_active_orders() -> None:
    class SequenceTransport(RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.responses = iter(
                (
                    {"orders": [{"orderId": "order-1"}]},
                    {},
                    {"orders": []},
                )
            )
            self.calls: list[dict[str, Any]] = []

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            payload: dict[str, object],
            timeout_seconds: float,
        ) -> dict[str, Any]:
            self.calls.append({"url": url, "payload": payload})
            return next(self.responses)

    transport = SequenceTransport()
    service = TInvestSandboxAccountService("token", transport)
    assert service.reconcile_cancel_active_orders("intraday") == ("order-1",)
    assert "CancelSandboxOrder" in transport.calls[1]["url"]


def test_sandbox_short_passes_explicit_margin_confirmation() -> None:
    class ShortTransport(RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[dict[str, Any]] = []

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            payload: dict[str, object],
            timeout_seconds: float,
        ) -> dict[str, Any]:
            self.calls.append({"url": url, "payload": payload})
            if "GetInstrumentBy" in url:
                return {
                    "instrument": {
                        "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                        "apiTradeAvailableFlag": True,
                        "shortEnabledFlag": True,
                    }
                }
            self.call = {"url": url, "headers": headers, "payload": payload}
            return {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "orderId": "short-1",
            }

    transport = ShortTransport()
    adapter = TInvestSandboxExecutionAdapter("sandbox-token", "sandbox-account", transport)
    original = _intent()
    short_intent = OrderIntent(
        original.order_request_id,
        original.instrument,
        Side.SELL,
        original.lots,
        original.limit_price,
        original.notional,
        "direction=short",
        confirm_margin_trade=True,
    )
    adapter.submit(short_intent)
    assert transport.call is not None
    assert transport.call["payload"]["confirmMarginTrade"] is True
    assert len(transport.calls) == 2
