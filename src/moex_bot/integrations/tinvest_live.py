"""Production-only REST port. The sandbox adapter/host is never reused for mutations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from ..live_execution import LiveHolding, LiveState
from ..service_config import PROD_REST
from .tinvest_sandbox import (
    JsonPostTransport,
    decimal_to_quotation,
    money_value_to_decimal,
    quotation_to_decimal,
)

PREFIX = "/tinkoff.public.invest.api.contract.v1."


def _rows(raw: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    value = raw.get(name, [])  # Protobuf JSON may omit empty repeated fields.
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"live response has invalid {name}")
    return value


def _money(raw: object) -> Decimal:
    if not isinstance(raw, Mapping) or str(raw.get("currency", "")).upper() != "RUB":
        raise ValueError("live response requires explicit RUB MoneyValue")
    value = money_value_to_decimal(raw)
    if not value.is_finite():
        raise ValueError("live money value is not finite")
    return value


@dataclass
class TInvestLiveBroker:
    token: str
    account_id: str
    transport: JsonPostTransport
    instruments_by_uid: dict[str, Instrument]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        if not self.token.strip() or not self.account_id.strip() or self.timeout_seconds <= 0:
            raise ValueError("production token/account/timeout required")

    @property
    def environment(self) -> str:
        return "prod"

    def _post(self, method: str, payload: Mapping[str, object]) -> Mapping[str, Any]:
        return self.transport.post(
            PROD_REST + PREFIX + method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )

    def capture(self, intent: OrderIntent) -> LiveState:
        started = self.clock()
        if self.instruments_by_uid.get(intent.instrument.uid) != intent.instrument:
            raise ValueError("production intent is not in the trusted instrument universe")
        accounts = _rows(self._post("UsersService/GetAccounts", {}), "accounts")
        matches = [a for a in accounts if a.get("id") == self.account_id]
        if len(matches) != 1:
            raise ValueError("production account identity cannot be verified")
        account = matches[0]
        raw = self._post(
            "InstrumentsService/GetInstrumentBy",
            {"idType": "INSTRUMENT_ID_TYPE_UID", "id": intent.instrument.uid},
        ).get("instrument")
        if not isinstance(raw, Mapping):
            raise ValueError("live instrument metadata missing")
        inst = intent.instrument
        identity = (
            raw.get("uid"),
            raw.get("ticker"),
            raw.get("classCode"),
            int(raw.get("lot", 0)),
            str(raw.get("currency", "")).upper(),
            quotation_to_decimal(raw.get("minPriceIncrement", {})),
            str(raw.get("instrumentType", "")).lower(),
        )
        if identity != (
            inst.uid,
            inst.secid,
            inst.board,
            inst.lot_size,
            inst.currency,
            inst.tick_size,
            "share",
        ):
            raise ValueError("live UID/ticker/class/lot/tick/currency/type mismatch")
        flags = (
            raw.get("apiTradeAvailableFlag") is True
            and raw.get("forIisFlag") is True
            and raw.get("forQualInvestorFlag") is not True
            and not raw.get("requiredTests")
            and raw.get("buyAvailableFlag" if intent.side is Side.BUY else "sellAvailableFlag")
            is True
        )
        payload = {"accountId": self.account_id}
        portfolio = self._post("OperationsService/GetPortfolio", {**payload, "currency": "RUB"})
        positions = self._post("OperationsService/GetPositions", payload)
        if positions.get("limitsLoadingInProgress", False) is not False:
            raise ValueError("production position limits are not verified as loaded")
        if positions.get("futures") or positions.get("options"):
            raise ValueError("initial live canary does not support existing derivatives")
        free = {
            str(row["instrumentUid"]): int(row["balance"])
            if row.get("exchangeBlocked") is not True
            else 0
            for row in _rows(positions, "securities")
        }
        holdings: dict[str, LiveHolding] = {}
        for row in _rows(portfolio, "positions"):
            quantity = quotation_to_decimal(row["quantity"])
            if quantity == 0:
                continue
            if str(row.get("instrumentType", "")).lower() == "currency":
                continue
            known = self.instruments_by_uid.get(str(row.get("instrumentUid", "")))
            if known is None or known.asset_class != "share" or quantity != int(quantity):
                raise ValueError("live holding outside verified share universe")
            if known.uid in holdings:
                raise ValueError("duplicate live portfolio instrument")
            holdings[known.uid] = LiveHolding(
                known,
                int(quantity),
                0 if row.get("blocked") is True else free.get(known.uid, 0),
                _money(row["currentPrice"]),
            )
        withdraw = self._post("OperationsService/GetWithdrawLimits", payload)
        cash = sum(
            (
                _money(row)
                for row in _rows(withdraw, "money")
                if str(row.get("currency", "")).upper() == "RUB"
            ),
            Decimal("0"),
        )
        for field in ("blocked", "blockedGuarantee"):
            cash -= sum(
                (
                    _money(row)
                    for row in _rows(withdraw, field)
                    if str(row.get("currency", "")).upper() == "RUB"
                ),
                Decimal("0"),
            )
        orders = _rows(self._post("OrdersService/GetOrders", payload), "orders")
        stops = _rows(self._post("StopOrdersService/GetStopOrders", payload), "stopOrders")
        turnover = self._turnover(started)
        estimate = self._post(
            "OrdersService/GetOrderPrice",
            {
                **payload,
                "instrumentId": inst.uid,
                "quantity": str(intent.lots),
                "direction": "ORDER_DIRECTION_BUY"
                if intent.side is Side.BUY
                else "ORDER_DIRECTION_SELL",
                "price": decimal_to_quotation(intent.limit_price),
            },
        )
        if (
            int(estimate.get("lotsRequested", 0)) != intent.lots
            or _money(estimate.get("initialOrderAmount")) != intent.notional
        ):
            raise ValueError("broker order-price quantity/notional mismatch")
        status = self._post("MarketDataService/GetTradingStatus", {"instrumentId": inst.uid})
        book = self._post("MarketDataService/GetOrderBook", {"instrumentId": inst.uid, "depth": 1})
        if book.get("instrumentUid") != inst.uid or status.get("instrumentUid") != inst.uid:
            raise ValueError("market data identity mismatch")
        if str(book.get("priceCurrency", "RUB")).upper() != "RUB":
            raise ValueError("order book currency mismatch")
        bids, asks = _rows(book, "bids"), _rows(book, "asks")
        if not bids or not asks or int(bids[0]["quantity"]) <= 0 or int(asks[0]["quantity"]) <= 0:
            raise ValueError("empty production order book")
        tradable = (
            flags
            and status.get("apiTradeAvailableFlag") is True
            and status.get("limitOrderAvailableFlag") is True
            and status.get("tradingStatus") == "SECURITY_TRADING_STATUS_NORMAL_TRADING"
        )
        return LiveState(
            self.account_id,
            str(account.get("type", "")),
            str(account.get("status", "")),
            str(account.get("accessLevel", "")),
            started,
            self.clock(),
            inst,
            tradable,
            datetime.fromisoformat(str(book["orderbookTs"]).replace("Z", "+00:00")),
            quotation_to_decimal(bids[0]["price"]),
            quotation_to_decimal(asks[0]["price"]),
            cash,
            _money(portfolio.get("totalAmountPortfolio")),
            _money(portfolio.get("dailyYield")),
            turnover,
            _money(estimate.get("totalOrderAmount")),
            holdings,
            len(orders),
            frozenset(str(row["instrumentUid"]) for row in stops),
        )

    def _turnover(self, now: datetime) -> Decimal:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(now.astimezone(zone).date(), time.min, zone)
        cursor = ""
        seen: set[str] = set()
        result = Decimal("0")
        for _ in range(100):
            response = self._post(
                "OperationsService/GetOperationsByCursor",
                {
                    "accountId": self.account_id,
                    "from": start.astimezone(UTC).isoformat(),
                    "to": now.isoformat(),
                    "state": "OPERATION_STATE_EXECUTED",
                    "limit": 1000,
                    "cursor": cursor,
                    "withoutCommissions": False,
                    "withoutTrades": False,
                },
            )
            for row in _rows(response, "items"):
                operation_id = str(row["id"])
                if operation_id in seen:
                    continue
                seen.add(operation_id)
                if str(row.get("type", "")) in {
                    "OPERATION_TYPE_BUY",
                    "OPERATION_TYPE_SELL",
                    "OPERATION_TYPE_BUY_CARD",
                    "OPERATION_TYPE_SELL_CARD",
                    "OPERATION_TYPE_BUY_MARGIN",
                    "OPERATION_TYPE_SELL_MARGIN",
                }:
                    result += abs(_money(row.get("payment")))
            if response.get("hasNext") is not True:
                return result
            next_cursor = str(response.get("nextCursor", ""))
            if not next_cursor or next_cursor == cursor:
                raise ValueError("live operations pagination did not advance")
            cursor = next_cursor
        raise ValueError("live operations pagination limit exceeded")

    def _decode(self, intent: OrderIntent, raw: Mapping[str, Any]) -> OrderRecord:
        statuses = {
            "NEW": OrderStatus.ACCEPTED,
            "PARTIALLYFILL": OrderStatus.PARTIALLY_FILLED,
            "FILL": OrderStatus.FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
        }
        status = statuses.get(
            str(raw.get("executionReportStatus", "")).removeprefix("EXECUTION_REPORT_STATUS_"),
            OrderStatus.UNKNOWN,
        )
        filled = int(raw.get("lotsExecuted", 0))
        if not 0 <= filled <= intent.lots or (
            status is OrderStatus.FILLED and filled != intent.lots
        ):
            raise ValueError("invalid production lotsExecuted")
        for key, expected in (
            ("instrumentUid", intent.instrument.uid),
            ("orderRequestId", intent.order_request_id),
        ):
            if key in raw and raw[key] != expected:
                raise ValueError("production order response identity mismatch")
        if "lotsRequested" in raw and int(raw["lotsRequested"]) != intent.lots:
            raise ValueError("production order response quantity mismatch")
        return OrderRecord(
            intent, status, filled, str(raw.get("orderId") or intent.order_request_id)
        )

    def submit(self, intent: OrderIntent) -> OrderRecord:
        if intent.order_type != "limit" or intent.confirm_margin_trade:
            raise ValueError("production transport allows only explicit non-margin limit orders")
        try:
            raw = self._post(
                "OrdersService/PostOrder",
                {
                    "accountId": self.account_id,
                    "instrumentId": intent.instrument.uid,
                    "quantity": str(intent.lots),
                    "price": decimal_to_quotation(intent.limit_price),
                    "direction": "ORDER_DIRECTION_BUY"
                    if intent.side is Side.BUY
                    else "ORDER_DIRECTION_SELL",
                    "orderType": "ORDER_TYPE_LIMIT",
                    "orderId": intent.order_request_id,
                    "timeInForce": "TIME_IN_FORCE_DAY",
                    "priceType": "PRICE_TYPE_CURRENCY",
                    "confirmMarginTrade": False,
                },
            )
        except OSError as exc:
            return OrderRecord(intent, OrderStatus.UNKNOWN, status_detail=type(exc).__name__)
        return self._decode(intent, raw)

    def lookup(self, record: OrderRecord) -> OrderRecord:
        try:
            raw = self._post(
                "OrdersService/GetOrderState",
                {
                    "accountId": self.account_id,
                    "orderId": record.intent.order_request_id,
                    "orderIdType": "ORDER_ID_TYPE_REQUEST",
                    "priceType": "PRICE_TYPE_CURRENCY",
                },
            )
        except OSError as exc:
            return replace(record, status=OrderStatus.UNKNOWN, status_detail=type(exc).__name__)
        return self._decode(record.intent, raw)
