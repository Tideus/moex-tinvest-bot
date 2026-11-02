from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from moex_bot.domain import Instrument, OrderStatus
from moex_bot.long_auto_sandbox import SandboxLongBroker, SandboxOnlyTransport


@pytest.fixture
def case():
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    instrument = Instrument("SBER", "uid", "TQBR", 10, Decimal(".01"))
    c = SimpleNamespace(
        instrument=instrument,
        limit_price=Decimal("100"),
        stop_trigger=Decimal("95"),
        order_request_id=str(uuid4()),
        stop_request_id=str(uuid4()),
        lots=1,
        valid_until=now + timedelta(minutes=3),
    )

    class Transport:
        def __init__(self):
            self.posts = []
            self.holding = False
            self.stop = False
            self.fault = None

        def post(self, url, *, headers, payload, timeout_seconds):
            assert url.startswith("https://sandbox-invest-public-api.tbank.ru/rest/")
            method = url.rsplit("/", 1)[1]
            self.posts.append((method, payload))
            if method == "GetSandboxAccounts":
                return {"accounts": [{"id": "sandbox-only", "status": "ACCOUNT_STATUS_OPEN"}]}
            if method == "GetInstrumentBy":
                return {
                    "instrument": {
                        "uid": "uid",
                        "ticker": "SBER",
                        "classCode": "TQBR",
                        "lot": 10,
                        "currency": "rub",
                        "minPriceIncrement": {"nano": 10000000},
                        "apiTradeAvailableFlag": True,
                        "buyAvailableFlag": True,
                        "sellAvailableFlag": True,
                    }
                }
            if method == "GetTradingStatus":
                return {
                    "instrumentUid": "uid",
                    "limitOrderAvailableFlag": True,
                    "apiTradeAvailableFlag": True,
                    "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
                }
            if method == "GetOrderBook":
                return {
                    "instrumentUid": "uid",
                    "orderbookTs": now.isoformat(),
                    "bids": [{"price": {"units": "100"}, "quantity": 10}],
                    "asks": [{"price": {"units": "100", "nano": 10000000}, "quantity": 10}],
                }
            if method == "GetSandboxPositions":
                return {
                    "securities": [{"instrumentUid": "uid", "balance": "10", "blocked": "0"}]
                    if self.holding
                    else []
                }
            if method == "GetSandboxOrders":
                return {"orders": []}
            if method == "GetSandboxOrderPrice":
                return {
                    "lotsRequested": "1",
                    "initialOrderAmount": {"units": "1000", "currency": "rub"},
                    "totalOrderAmount": {"units": "1001", "currency": "rub"},
                }
            if method == "GetSandboxWithdrawLimits":
                return {"money": [{"units": "10000", "currency": "rub"}]}
            if method in {"PostSandboxOrder", "GetSandboxOrderState"}:
                self.holding = True
                return {
                    "instrumentUid": "other" if self.fault == "parent" else "uid",
                    "orderRequestId": c.order_request_id,
                    "orderId": "exchange",
                    "direction": "ORDER_DIRECTION_BUY",
                    "lotsRequested": "1",
                    "lotsExecuted": "1",
                    "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                }
            if method == "PostSandboxStopOrder":
                self.stop = True
                if self.fault == "lost":
                    raise TimeoutError("lost")
                return {"stopOrderId": "stop", "orderRequestId": c.stop_request_id}
            if method == "GetSandboxStopOrders":
                if self.fault == "unsupported":
                    raise ValueError("native sandbox stop unsupported")
                return {
                    "stopOrders": [
                        {
                            "instrumentUid": "uid",
                            "stopOrderId": "stop",
                            "lotsRequested": "1",
                            "direction": "STOP_ORDER_DIRECTION_SELL",
                            "orderType": "STOP_ORDER_TYPE_STOP_LOSS",
                            "status": "STOP_ORDER_STATUS_ACTIVE",
                            "currency": "rub",
                            "stopPrice": {"units": "95", "currency": "rub"},
                        }
                    ]
                    if self.stop
                    else []
                }
            raise AssertionError(method)

    t = Transport()
    b = SandboxLongBroker("fake", "sandbox-only", transport=t, clock=lambda: now)
    return c, b, t


def test_same_adapter_entry_and_native_protection(case):
    c, broker, transport = case
    order = broker.submit(c)
    assert order.status is OrderStatus.FILLED
    assert broker.protect(c, order)["verified"] is True
    assert [m for m, _ in transport.posts].count("PostSandboxOrder") == 1
    assert [m for m, _ in transport.posts].count("PostSandboxStopOrder") == 1


@pytest.mark.parametrize("fault", ["expired", "holding", "unsupported", "prod_id"])
def test_preflight_blocks_before_buy(case, monkeypatch, fault):
    c, broker, transport = case
    if fault == "expired":
        c.valid_until -= timedelta(minutes=10)
    elif fault == "holding":
        transport.holding = True
    elif fault == "unsupported":
        transport.fault = fault
    else:
        monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "sandbox-only")
        with pytest.raises(ValueError):
            SandboxLongBroker("fake", "sandbox-only", transport=transport)
        return
    with pytest.raises(ValueError):
        broker.submit(c)
    assert "PostSandboxOrder" not in [m for m, _ in transport.posts]


@pytest.mark.parametrize("fault", ["parent", "lost"])
def test_ambiguity_never_retries_mutation(case, fault):
    c, broker, transport = case
    if fault == "parent":
        transport.fault = fault
        with pytest.raises(ValueError):
            broker.submit(c)
        assert "PostSandboxStopOrder" not in [m for m, _ in transport.posts]
    else:
        filled = broker.submit(c)
        transport.fault = fault
        with pytest.raises(TimeoutError):
            broker.protect(c, filled)
        assert [m for m, _ in transport.posts].count("PostSandboxStopOrder") == 1


def test_fixed_transport_refuses_prod_before_network():
    with pytest.raises(ValueError):
        SandboxOnlyTransport().post(
            "https://invest-public-api.tbank.ru/rest/OrdersService/PostOrder",
            headers={},
            payload={},
            timeout_seconds=1,
        )


@pytest.mark.parametrize("fault", ["uid", "lot", "tick", "cash", "fees", "status", "stale", "book"])
def test_fresh_real_contract_preflight_negatives(case, fault):
    c, broker, transport = case
    original = transport.post

    def wrapped(url, **kwargs):
        raw = original(url, **kwargs)
        method = url.rsplit("/", 1)[1]
        if method == "GetInstrumentBy":
            if fault == "uid":
                raw["instrument"]["uid"] = "foreign"
            elif fault == "lot":
                raw["instrument"]["lot"] = 100
            elif fault == "tick":
                raw["instrument"]["minPriceIncrement"] = {"units": "1"}
        elif method == "GetSandboxWithdrawLimits" and fault == "cash":
            raw["money"][0]["units"] = "1"
        elif method == "GetSandboxOrderPrice" and fault == "fees":
            raw["totalOrderAmount"]["units"] = "10001"
        elif method == "GetTradingStatus" and fault == "status":
            raw["limitOrderAvailableFlag"] = False
        elif method == "GetOrderBook":
            if fault == "stale":
                raw["orderbookTs"] = "2000-01-01T00:00:00Z"
            elif fault == "book":
                raw["asks"] = []
        return raw

    transport.post = wrapped
    with pytest.raises(ValueError):
        broker.submit(c)
    assert "PostSandboxOrder" not in [m for m, _ in transport.posts]


def test_verified_own_pending_remainder_cancel_and_readback(case):
    from dataclasses import replace

    c, broker, transport = case
    filled = broker.submit(c)
    pending = replace(filled, status=OrderStatus.ACCEPTED, filled_lots=0)
    original = transport.post
    count = 0

    def wrapped(url, **kwargs):
        nonlocal count
        method = url.rsplit("/", 1)[1]
        if method == "CancelSandboxOrder":
            assert kwargs["payload"]["orderId"] == "exchange"
            transport.posts.append((method, kwargs["payload"]))
            return {"time": "2026-11-02T09:00:00Z"}
        raw = original(url, **kwargs)
        if method == "GetSandboxOrderState":
            count += 1
            raw["lotsExecuted"] = "0"
            raw["executionReportStatus"] = (
                "EXECUTION_REPORT_STATUS_NEW" if count == 1 else "EXECUTION_REPORT_STATUS_CANCELLED"
            )
        return raw

    transport.post = wrapped
    result = broker.cancel_unfilled_verified(pending)
    assert result.status is OrderStatus.CANCELLED
    assert [m for m, _ in transport.posts].count("CancelSandboxOrder") == 1


@pytest.mark.parametrize("fault", ["accounts", "loading", "orders", "stops", "price", "rows"])
def test_other_preflight_fail_closed_contracts(case, fault):
    c, broker, transport = case
    original = transport.post

    def wrapped(url, **kwargs):
        raw = original(url, **kwargs)
        method = url.rsplit("/", 1)[1]
        if method == "GetSandboxAccounts" and fault == "accounts":
            raw["accounts"] = []
        elif method == "GetSandboxPositions" and fault == "loading":
            raw["limitsLoadingInProgress"] = True
        elif method == "GetSandboxOrders" and fault == "orders":
            raw["orders"] = [{}]
        elif method == "GetSandboxStopOrders" and fault == "stops":
            raw["stopOrders"] = [{}]
        elif method == "GetOrderBook" and fault == "price":
            raw["bids"][0]["price"]["units"] = "1"
        elif method == "GetSandboxPositions" and fault == "rows":
            raw["securities"] = "invalid"
        return raw

    transport.post = wrapped
    with pytest.raises(ValueError):
        broker.preflight(c)


def test_slow_preflight_cannot_be_submitted(case):
    c, broker, _ = case
    now = broker.clock()

    def slow_book(candidate):
        broker.clock = lambda: now + timedelta(seconds=30)

    broker._book = slow_book
    with pytest.raises(ValueError, match="stale"):
        broker.submit(c)


@pytest.mark.parametrize(
    "fault",
    [
        "filled_count",
        "exchange_id",
        "pending_cancel",
        "terminal_cancel",
        "protection_parent",
        "holding_conflict",
        "stop_correlation",
        "stop_missing",
        "stop_fields",
    ],
)
def test_post_entry_contract_guards(case, fault):
    from dataclasses import replace

    c, broker, transport = case
    filled = broker.submit(c)
    if fault == "filled_count":
        with pytest.raises(ValueError):
            broker._decode(
                filled.intent,
                {
                    "instrumentUid": "uid",
                    "orderRequestId": c.order_request_id,
                    "orderId": "exchange",
                    "lotsRequested": "1",
                    "lotsExecuted": "0",
                    "direction": "ORDER_DIRECTION_BUY",
                    "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                },
            )
        return
    if fault == "exchange_id":
        with pytest.raises(ValueError):
            broker.lookup(replace(filled, broker_order_id="foreign"))
        return
    if fault == "terminal_cancel":
        assert broker.cancel_unfilled_verified(filled).status is OrderStatus.FILLED
        return
    if fault == "pending_cancel":
        broker.lookup = lambda rec: replace(rec, status=OrderStatus.ACCEPTED, filled_lots=0)
        broker._sandbox = lambda *a, **k: {}
        with pytest.raises(ValueError):
            broker.cancel_unfilled_verified(filled)
        return
    if fault == "protection_parent":
        c.order_request_id = str(uuid4())
        # Preserve the exact previous parent returned by the verified broker read.
        broker.lookup = lambda rec: filled
        with pytest.raises(ValueError):
            broker.protect(c, filled)
        return
    if fault == "holding_conflict":
        original_sandbox = broker._sandbox
        broker._sandbox = lambda method, *a, **k: (
            {"securities": []}
            if method == "GetSandboxPositions"
            else original_sandbox(method, *a, **k)
        )
        with pytest.raises(ValueError):
            broker.protect(c, filled)
        return
    response = {"stopOrderId": "stop", "orderRequestId": c.stop_request_id}
    if fault == "stop_correlation":
        response["orderRequestId"] = "other"
    elif fault == "stop_fields":
        transport.stop = True
        c.stop_trigger = Decimal("96")
    with pytest.raises(ValueError):
        broker.verify_protection(c, response)


@pytest.mark.parametrize("body", [b"{}", b"[]"])
def test_fixed_network_transport_decodes_mapping_only(monkeypatch, body):
    from moex_bot import long_auto_sandbox as module

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return body

    opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
    monkeypatch.setattr(module, "build_opener", lambda *args: opener)
    url = "https://sandbox-invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.SandboxService/GetSandboxAccounts"
    if body == b"[]":
        with pytest.raises(ValueError):
            SandboxOnlyTransport().post(url, headers={}, payload={}, timeout_seconds=1)
    else:
        assert SandboxOnlyTransport().post(url, headers={}, payload={}, timeout_seconds=1) == {}


def test_redirect_and_missing_credentials_fail_closed():
    from urllib.error import HTTPError

    from moex_bot.long_auto_sandbox import _NoRedirect

    with pytest.raises(HTTPError):
        _NoRedirect().redirect_request(
            SimpleNamespace(full_url="sandbox"), None, 302, "", {}, "prod"
        )
    with pytest.raises(ValueError):
        SandboxLongBroker("", "sandbox")


@pytest.mark.parametrize("fault", ["expired_signal", "moved_quote", "stale_quote"])
def test_filled_parent_still_gets_native_protection(case, fault):
    c, broker, transport = case
    filled = broker.submit(c)
    original = transport.post
    if fault == "expired_signal":
        c.valid_until -= timedelta(days=1)

    def wrapped(url, **kwargs):
        raw = original(url, **kwargs)
        if url.endswith("GetOrderBook"):
            if fault == "moved_quote":
                raw["bids"][0]["price"]["units"] = "110"
                raw["asks"][0]["price"]["units"] = "110"
            elif fault == "stale_quote":
                raw["orderbookTs"] = "2026-01-01T00:00:00Z"
        return raw

    transport.post = wrapped
    assert broker.protect(c, filled)["verified"] is True


@pytest.mark.parametrize("fault", ["auction", "no_api"])
def test_non_normal_trading_status_blocks_buy(case, fault):
    c, broker, transport = case
    original = transport.post

    def wrapped(url, **kwargs):
        raw = original(url, **kwargs)
        if url.endswith("GetTradingStatus"):
            if fault == "auction":
                raw["tradingStatus"] = "SECURITY_TRADING_STATUS_OPENING_AUCTION"
            else:
                raw["apiTradeAvailableFlag"] = False
        return raw

    transport.post = wrapped
    with pytest.raises(ValueError):
        broker.submit(c)
    assert "PostSandboxOrder" not in [m for m, _ in transport.posts]


def test_protobuf_empty_rows_allowed_unknown_error_shape_forbidden():
    from moex_bot.long_auto_sandbox import _rows

    assert _rows({}, "orders") == []
    with pytest.raises(ValueError):
        _rows({"error": "not an empty protobuf response"}, "orders")


@pytest.mark.parametrize("fault", ["cash_duplicate", "negative_blocked", "unknown_uid", "account"])
def test_adversarial_cash_and_positions_fail_before_buy(case, fault):
    c, broker, transport = case
    original = transport.post

    def wrapped(url, **kwargs):
        raw = original(url, **kwargs)
        if url.endswith("GetSandboxWithdrawLimits"):
            if fault == "cash_duplicate":
                raw["money"].append(dict(raw["money"][0]))
            elif fault == "negative_blocked":
                raw["blocked"] = [{"units": "-100", "currency": "rub"}]
        elif url.endswith("GetSandboxPositions"):
            if fault == "unknown_uid":
                raw["securities"] = [{"balance": "100", "blocked": "0"}]
            elif fault == "account":
                raw["accountId"] = "foreign"
        return raw

    transport.post = wrapped
    with pytest.raises(ValueError):
        broker.submit(c)
    assert "PostSandboxOrder" not in [m for m, _ in transport.posts]


def test_stop_limit_child_cannot_masquerade_as_market(case):
    c, broker, transport = case
    transport.stop = True
    original = transport.post

    def wrapped(url, **kwargs):
        raw = original(url, **kwargs)
        if url.endswith("GetSandboxStopOrders"):
            raw["stopOrders"][0]["exchangeOrderType"] = "EXCHANGE_ORDER_TYPE_LIMIT"
        return raw

    transport.post = wrapped
    with pytest.raises(ValueError):
        broker.verify_protection(c, {"stopOrderId": "stop", "orderRequestId": c.stop_request_id})


def test_secondary_positions_binding_and_actual_entry_gate(case):
    c, broker, transport = case
    original = broker._sandbox
    broker._sandbox = lambda method, *args, **kwargs: (
        {"accountId": "foreign", "securities": []}
        if method == "GetSandboxPositions"
        else original(method, *args, **kwargs)
    )
    with pytest.raises(ValueError, match="positions belong"):
        broker.preflight(c)
    broker._sandbox = original

    def revoked():
        raise ValueError("entry permission revoked at final boundary")

    broker.entry_gate = revoked
    with pytest.raises(ValueError, match="entry permission revoked"):
        broker.submit(c)
    assert "PostSandboxOrder" not in [method for method, _ in transport.posts]


@pytest.mark.parametrize("seconds", [12, 181])
def test_local_gate_cannot_let_expired_book_or_signal_reach_dispatch(case, seconds):
    c, broker, transport = case
    start = broker.clock()
    broker.entry_gate = lambda: setattr(broker, "clock", lambda: start + timedelta(seconds=seconds))
    with pytest.raises(ValueError):
        broker.submit(c)
    assert not any(m == "PostSandboxOrder" for m, _ in transport.posts)
