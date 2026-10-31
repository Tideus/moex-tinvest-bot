from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.integrations.tinvest_live import TInvestLiveBroker
from moex_bot.service_config import PROD_REST

NOW = datetime(2026, 10, 30, 10, tzinfo=UTC)


def _intent() -> OrderIntent:
    return OrderIntent(
        str(uuid4()),
        Instrument(
            "SBER",
            "uid",
            "TQBR",
            10,
            Decimal(".01"),
            issuer_id="sber",
            sector="finance",
            risk_cluster="domestic",
        ),
        Side.BUY,
        5,
        Decimal("100"),
        Decimal("5000"),
        "live transport contract",
    )


def money(units: str) -> dict:
    return {"currency": "rub", "units": units, "nano": 0}


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_post = False
        self.responses = {
            "GetAccounts": {
                "accounts": [
                    {
                        "id": "iis",
                        "type": "ACCOUNT_TYPE_TINKOFF_IIS",
                        "status": "ACCOUNT_STATUS_OPEN",
                        "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                    }
                ]
            },
            "GetInstrumentBy": {
                "instrument": {
                    "uid": "uid",
                    "ticker": "SBER",
                    "classCode": "TQBR",
                    "lot": 10,
                    "currency": "rub",
                    "minPriceIncrement": {"units": "0", "nano": 10000000},
                    "instrumentType": "share",
                    "apiTradeAvailableFlag": True,
                    "forIisFlag": True,
                    "buyAvailableFlag": True,
                }
            },
            "GetPortfolio": {"totalAmountPortfolio": money("160000"), "dailyYield": money("0")},
            "GetPositions": {"limitsLoadingInProgress": False},
            "GetWithdrawLimits": {"money": [money("40000")]},
            "GetOrders": {},
            "GetStopOrders": {},
            "GetOperationsByCursor": {"hasNext": False},
            "GetOrderPrice": {
                "lotsRequested": "5",
                "initialOrderAmount": money("5000"),
                "totalOrderAmount": money("5005"),
            },
            "GetTradingStatus": {
                "instrumentUid": "uid",
                "apiTradeAvailableFlag": True,
                "limitOrderAvailableFlag": True,
                "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
            },
            "GetOrderBook": {
                "instrumentUid": "uid",
                "orderbookTs": NOW.isoformat(),
                "bids": [{"price": {"units": "100"}, "quantity": "10"}],
                "asks": [{"price": {"units": "100", "nano": 100000000}, "quantity": "10"}],
            },
            "PostOrder": {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": "5",
                "instrumentUid": "uid",
                "orderId": "exchange-id",
            },
            "GetOrderState": {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lotsExecuted": "2",
                "lotsRequested": "5",
                "instrumentUid": "uid",
            },
        }

    def post(self, url: str, *, headers: dict, payload: dict, timeout_seconds: float) -> dict:
        method = url.rsplit("/", 1)[-1]
        assert url.startswith(PROD_REST + "/") and "sandbox" not in url
        self.calls.append((method, payload))
        if method == "PostOrder" and self.fail_post:
            raise TimeoutError("unknown delivery")
        return self.responses[method]


def _broker(transport: Transport) -> TInvestLiveBroker:
    intent = _intent()
    return TInvestLiveBroker(
        "test-token", "iis", transport, {"uid": intent.instrument}, clock=lambda: NOW
    )


def test_real_port_capture_uses_production_read_methods_only() -> None:
    transport = Transport()
    state = _broker(transport).capture(_intent())
    assert state.account_type == "ACCOUNT_TYPE_TINKOFF_IIS"
    assert state.cash_available == Decimal("40000") and state.estimated_total == Decimal("5005")
    assert not any(method in {"PostOrder", "CancelOrder"} for method, _ in transport.calls)


@pytest.mark.parametrize(
    "fault",
    [
        "uid",
        "lot",
        "daily_pnl_missing",
        "loading",
        "empty_book",
        "cost_quantity",
        "unknown_share",
        "pagination",
    ],
)
def test_live_capture_fails_closed_on_incomplete_or_changed_data(fault: str) -> None:
    transport = Transport()
    if fault == "uid":
        transport.responses["GetInstrumentBy"]["instrument"]["uid"] = "different"
    if fault == "lot":
        transport.responses["GetInstrumentBy"]["instrument"]["lot"] = 1
    if fault == "daily_pnl_missing":
        del transport.responses["GetPortfolio"]["dailyYield"]
    if fault == "loading":
        transport.responses["GetPositions"]["limitsLoadingInProgress"] = True
    if fault == "empty_book":
        transport.responses["GetOrderBook"]["asks"] = []
    if fault == "cost_quantity":
        transport.responses["GetOrderPrice"]["lotsRequested"] = "1"
    if fault == "unknown_share":
        transport.responses["GetPortfolio"]["positions"] = [
            {"quantity": {"units": "1"}, "instrumentType": "share", "instrumentUid": "unknown"}
        ]
    if fault == "pagination":
        transport.responses["GetOperationsByCursor"] = {"hasNext": True, "nextCursor": ""}
    with pytest.raises((ValueError, KeyError)):
        _broker(transport).capture(_intent())
    assert not any(method == "PostOrder" for method, _ in transport.calls)


def test_live_submit_uses_lots_uid_limit_day_no_margin_and_unknown_is_not_retried() -> None:
    transport = Transport()
    broker = _broker(transport)
    intent = _intent()
    result = broker.submit(intent)
    assert result.status is OrderStatus.FILLED
    payload = transport.calls[-1][1]
    assert payload["quantity"] == "5" and payload["instrumentId"] == "uid"
    assert payload["orderId"] == intent.order_request_id and payload["confirmMarginTrade"] is False
    assert (
        payload["timeInForce"] == "TIME_IN_FORCE_DAY" and payload["orderType"] == "ORDER_TYPE_LIMIT"
    )
    transport.fail_post = True
    assert broker.submit(intent).status is OrderStatus.UNKNOWN
    assert len(transport.calls) == 2


def test_live_reconcile_is_read_only_by_request_uuid() -> None:
    transport = Transport()
    broker = _broker(transport)
    intent = _intent()
    result = broker.lookup(OrderRecord(intent, OrderStatus.UNKNOWN))
    assert result.status is OrderStatus.CANCELLED and result.filled_lots == 2
    assert transport.calls[-1] == (
        "GetOrderState",
        {
            "accountId": "iis",
            "orderId": intent.order_request_id,
            "orderIdType": "ORDER_ID_TYPE_REQUEST",
            "priceType": "PRICE_TYPE_CURRENCY",
        },
    )


@pytest.mark.parametrize(
    "field,value", [("lotsExecuted", "6"), ("instrumentUid", "wrong"), ("lotsRequested", "1")]
)
def test_live_result_identity_and_quantity_are_checked(field: str, value: str) -> None:
    transport = Transport()
    transport.responses["GetOrderState"][field] = value
    with pytest.raises(ValueError):
        _broker(transport).lookup(OrderRecord(_intent(), OrderStatus.UNKNOWN))
