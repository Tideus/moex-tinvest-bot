"""Production port failure contracts; all traffic is confined to fake transport."""

from dataclasses import replace
from decimal import Decimal

import pytest
from test_tinvest_live import NOW, Transport, _broker, _intent, money

from moex_bot.domain import OrderRecord, OrderStatus, Side
from moex_bot.integrations.tinvest_live import TInvestLiveBroker, _money, _rows


@pytest.mark.parametrize("value", [None, {}, [1], [None]])
def test_repeated_fields_must_be_lists_of_objects(value: object) -> None:
    with pytest.raises(ValueError, match="invalid"):
        _rows({"items": value}, "items")


@pytest.mark.parametrize("value", [None, {}, {"currency": "usd", "units": "1"}])
def test_money_requires_rub(value: object) -> None:
    with pytest.raises(ValueError, match="RUB"):
        _money(value)


def test_nonfinite_money_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "moex_bot.integrations.tinvest_live.money_value_to_decimal", lambda raw: Decimal("NaN")
    )
    with pytest.raises(ValueError, match="finite"):
        _money(money("1"))


def test_environment_and_currency_holdings() -> None:
    transport = Transport()
    transport.responses["GetPortfolio"]["positions"] = [
        {"quantity": {"units": "20"}, "instrumentType": "currency"},
    ]
    broker = _broker(transport)
    assert broker.environment == "prod"
    assert broker.capture(_intent()).positions == {}


@pytest.mark.parametrize(
    "token,account,timeout", [("", "iis", 10), ("t", " ", 10), ("t", "iis", 0)]
)
def test_production_broker_requires_credentials_and_timeout(
    token: str,
    account: str,
    timeout: float,
) -> None:
    with pytest.raises(ValueError):
        TInvestLiveBroker(token, account, Transport(), {}, timeout_seconds=timeout)


@pytest.mark.parametrize(
    "fault",
    [
        "untrusted",
        "account",
        "metadata",
        "derivative",
        "book_uid",
        "currency",
        "duplicate",
        "zero",
        "cash",
    ],
)
def test_capture_remaining_boundaries(fault: str) -> None:
    transport = Transport()
    broker = _broker(transport)
    if fault == "untrusted":
        broker.instruments_by_uid = {}
    elif fault == "account":
        transport.responses["GetAccounts"] = {"accounts": []}
    elif fault == "metadata":
        transport.responses["GetInstrumentBy"] = {"instrument": None}
    elif fault == "derivative":
        transport.responses["GetPositions"]["futures"] = [{}]
    elif fault == "book_uid":
        transport.responses["GetOrderBook"]["instrumentUid"] = "bad"
    elif fault == "currency":
        transport.responses["GetOrderBook"]["priceCurrency"] = "usd"
    elif fault in {"duplicate", "zero"}:
        row = {
            "quantity": {"units": "0" if fault == "zero" else "1"},
            "instrumentType": "share",
            "instrumentUid": "uid",
            "currentPrice": money("100"),
        }
        transport.responses["GetPortfolio"]["positions"] = [row, row]
    else:
        transport.responses["GetWithdrawLimits"]["blocked"] = [money("1")]
    if fault in {"zero", "cash"}:
        state = broker.capture(_intent())
        assert state.cash_available == Decimal("39999" if fault == "cash" else "40000")
    else:
        with pytest.raises(ValueError):
            broker.capture(_intent())
    assert all(method != "PostOrder" for method, _ in transport.calls)


def test_turnover_counts_unique_trades_not_dividends() -> None:
    transport = Transport()
    row = {"id": "trade", "type": "OPERATION_TYPE_BUY", "payment": money("-100")}
    transport.responses["GetOperationsByCursor"]["items"] = [
        row,
        row,
        {"id": "div", "type": "OPERATION_TYPE_DIVIDEND", "payment": money("10")},
    ]
    assert _broker(transport)._turnover(NOW) == Decimal("100")


def test_turnover_pagination_limit_is_bounded() -> None:
    class Pages(Transport):
        def post(self, url: str, **kwargs: object) -> dict:
            return {"hasNext": True, "nextCursor": str(len(self.calls)), "items": []}

    transport = Pages()

    def page(method: str, payload: object) -> dict:
        transport.calls.append((method, {}))
        return {"hasNext": True, "nextCursor": str(len(transport.calls))}

    broker = _broker(transport)
    broker._post = page
    with pytest.raises(ValueError, match="limit exceeded"):
        broker._turnover(NOW)
    assert len(transport.calls) == 100


@pytest.mark.parametrize("change", [{"order_type": "market"}, {"confirm_margin_trade": True}])
def test_submit_rejects_unsafe_order_types(change: dict) -> None:
    transport = Transport()
    with pytest.raises(ValueError, match="non-margin"):
        _broker(transport).submit(replace(_intent(), **change))
    assert not transport.calls


def test_sell_submission_direction() -> None:
    transport = Transport()
    assert (
        _broker(transport).submit(replace(_intent(), side=Side.SELL)).status is OrderStatus.FILLED
    )
    assert transport.calls[-1][1]["direction"] == "ORDER_DIRECTION_SELL"


def test_lookup_network_failure_preserves_unknown_and_fill_floor() -> None:
    class Offline(Transport):
        def post(self, *args: object, **kwargs: object) -> dict:
            raise TimeoutError("offline")

    record = OrderRecord(_intent(), OrderStatus.PARTIALLY_FILLED, 2)
    result = _broker(Offline()).lookup(record)
    assert result.status is OrderStatus.UNKNOWN
    assert result.filled_lots == 2
    assert result.status_detail == "TimeoutError"
