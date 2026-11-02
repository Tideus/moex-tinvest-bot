"""Fixed-host Sandbox transport for the single-shot LONG runner (no strategy/state machine)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from .integrations.tinvest_sandbox import (
    JsonPostTransport,
    decimal_to_quotation,
    money_value_to_decimal,
    quotation_to_decimal,
)
from .service_config import SANDBOX_REST


class CandidatePort(Protocol):
    @property
    def instrument(self) -> Instrument: ...
    @property
    def limit_price(self) -> Decimal: ...
    @property
    def stop_trigger(self) -> Decimal: ...
    @property
    def order_request_id(self) -> str: ...
    @property
    def stop_request_id(self) -> str: ...
    @property
    def lots(self) -> int: ...
    @property
    def valid_until(self) -> datetime: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


class SandboxOnlyTransport:
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if not url.startswith(SANDBOX_REST + "/tinkoff.public.invest.api.contract.v1."):
            raise ValueError("only fixed official sandbox endpoint permitted")
        request = Request(
            url, data=json.dumps(payload).encode(), headers=dict(headers), method="POST"
        )
        with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            raw = json.loads(response.read())
        if not isinstance(raw, dict):
            raise ValueError("invalid sandbox response")
        return raw


def _rows(raw: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    known = {
        "securities",
        "futures",
        "options",
        "money",
        "blocked",
        "blockedGuarantee",
        "limitsLoadingInProgress",
        "accountId",
        "orders",
        "stopOrders",
        "accounts",
        "bids",
        "asks",
        "instrumentUid",
        "orderbookTs",
        "priceCurrency",
        "depth",
        "lastPrice",
        "closePrice",
        "limitUp",
        "limitDown",
    }
    if field not in raw and any(k not in known for k in raw):
        raise ValueError("unknown response cannot prove empty sandbox rows")
    values = raw.get(field, [])
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise ValueError("invalid sandbox response rows")
    return values


class SandboxLongBroker:
    environment = "sandbox"

    def __init__(
        self,
        token: str,
        account_id: str,
        *,
        transport: JsonPostTransport | None = None,
        entry_gate: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not token.strip() or not account_id.strip():
            raise ValueError("explicit sandbox token/account required")
        if account_id == os.getenv("T_INVEST_PROD_ACCOUNT_ID", "").strip():
            raise ValueError("production account forbidden")
        self.token, self.account_id = token, account_id
        self.transport = transport or SandboxOnlyTransport()
        self.clock = clock
        self.entry_gate = entry_gate
        self.mutation_guard: Callable[[], None] = lambda: None

    def set_mutation_guard(self, callback: Callable[[], None]) -> None:
        self.mutation_guard = callback

    def _post(self, method: str, payload: Mapping[str, object]) -> Mapping[str, Any]:
        return self.transport.post(
            SANDBOX_REST + "/tinkoff.public.invest.api.contract.v1." + method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            payload=payload,
            timeout_seconds=10,
        )

    def _sandbox(
        self,
        method: str,
        payload: Mapping[str, object] | None = None,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        if method in {"PostSandboxOrder", "PostSandboxStopOrder", "CancelSandboxOrder"}:
            self.mutation_guard()
        if before_send is not None:
            before_send()
        raw = self._post(
            "SandboxService/" + method, {"accountId": self.account_id, **(payload or {})}
        )
        if raw.get("accountId", self.account_id) != self.account_id:
            raise ValueError("sandbox account response binding changed")
        return raw

    def _validate(self, candidate: CandidatePort, *, entry: bool = True) -> None:
        UUID(candidate.order_request_id)
        UUID(candidate.stop_request_id)
        i = candidate.instrument
        if (
            type(candidate.lots) is not int
            or candidate.lots != 1
            or candidate.valid_until.tzinfo is None
            or self.clock().tzinfo is None
            or (entry and not self.clock() < candidate.valid_until)
            or (entry and candidate.valid_until - self.clock() > timedelta(minutes=5))
            or i.currency != "RUB"
            or i.asset_class != "share"
            or i.board != "TQBR"
            or candidate.order_request_id == candidate.stop_request_id
            or any(
                not x.is_finite() or x <= 0 or x % i.tick_size
                for x in (candidate.limit_price, candidate.stop_trigger)
            )
            or candidate.stop_trigger >= candidate.limit_price
        ):
            raise ValueError("invalid/expired sandbox candidate")

    def _book(self, candidate: CandidatePort, *, entry: bool = True) -> datetime:
        raw = self._post(
            "MarketDataService/GetOrderBook", {"instrumentId": candidate.instrument.uid, "depth": 1}
        )
        at = datetime.fromisoformat(str(raw.get("orderbookTs", "")).replace("Z", "+00:00"))
        bids, asks = _rows(raw, "bids"), _rows(raw, "asks")
        if (
            raw.get("instrumentUid") != candidate.instrument.uid
            or at.tzinfo is None
            or (entry and not timedelta(0) <= self.clock() - at <= timedelta(seconds=10))
            or not bids
            or not asks
        ):
            raise ValueError("fresh exact sandbox book required")
        bid, ask = (quotation_to_decimal(x[0].get("price", {})) for x in (bids, asks))
        if (
            not all(x.is_finite() and x > 0 for x in (bid, ask))
            or any(int(x[0].get("quantity", 0)) <= 0 for x in (bids, asks))
            or ask < bid
            or (entry and ask / bid - 1 > Decimal(".005"))
            or bid <= candidate.stop_trigger
            or (entry and abs(candidate.limit_price / ((ask + bid) / 2) - 1) > Decimal(".01"))
        ):
            raise ValueError("sandbox book/stop/limit is unsafe")
        return at

    def preflight(self, candidate: CandidatePort) -> dict[str, Any]:
        self._validate(candidate)
        started = self.clock()
        accounts = _rows(self._post("SandboxService/GetSandboxAccounts", {}), "accounts")
        if (
            len(
                [
                    a
                    for a in accounts
                    if a.get("id") == self.account_id and a.get("status") == "ACCOUNT_STATUS_OPEN"
                ]
            )
            != 1
        ):
            raise ValueError("account is not a verified open sandbox account")
        i = candidate.instrument
        raw = self._post(
            "InstrumentsService/GetInstrumentBy", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": i.uid}
        ).get("instrument", {})
        if (
            raw.get("uid") != i.uid
            or raw.get("ticker") != i.secid
            or raw.get("classCode") != i.board
            or str(raw.get("lot")) != str(i.lot_size)
            or str(raw.get("currency", "")).upper() != "RUB"
            or quotation_to_decimal(raw.get("minPriceIncrement", {})) != i.tick_size
            or raw.get("apiTradeAvailableFlag") is not True
            or raw.get("buyAvailableFlag") is not True
            or raw.get("sellAvailableFlag") is not True
        ):
            raise ValueError("sandbox identity/lot/tick/trading flags changed")
        status = self._post("MarketDataService/GetTradingStatus", {"instrumentId": i.uid})
        if (
            status.get("instrumentUid") != i.uid
            or status.get("limitOrderAvailableFlag") is not True
            or status.get("apiTradeAvailableFlag") is not True
            or status.get("tradingStatus") != "SECURITY_TRADING_STATUS_NORMAL_TRADING"
        ):
            raise ValueError("sandbox limit trading unavailable")
        positions = self._sandbox("GetSandboxPositions")
        if positions.get("limitsLoadingInProgress") is True:
            raise ValueError("sandbox limits loading")
        if positions.get("accountId", self.account_id) != self.account_id:
            raise ValueError("sandbox positions belong to another account")
        if any(
            not p.get("instrumentUid") or p.get("instrumentUid") == i.uid
            for p in _rows(positions, "securities")
        ):
            raise ValueError("existing candidate holding forbidden")
        if _rows(self._sandbox("GetSandboxOrders"), "orders"):
            raise ValueError("active sandbox orders forbidden")
        stops = _rows(
            self._sandbox("GetSandboxStopOrders", {"status": "STOP_ORDER_STATUS_ACTIVE"}),
            "stopOrders",
        )  # Native-stop capability is probed BEFORE BUY.
        if any(not s.get("instrumentUid") or s.get("instrumentUid") == i.uid for s in stops):
            raise ValueError("existing/ambiguous candidate stop forbidden")
        estimate = self._sandbox(
            "GetSandboxOrderPrice",
            {
                "instrumentId": i.uid,
                "quantity": "1",
                "direction": "ORDER_DIRECTION_BUY",
                "price": decimal_to_quotation(candidate.limit_price),
            },
        )
        total = money_value_to_decimal(estimate.get("totalOrderAmount", {}))
        initial = money_value_to_decimal(estimate.get("initialOrderAmount", {}))
        if (
            str(estimate.get("lotsRequested")) != "1"
            or str(estimate.get("initialOrderAmount", {}).get("currency", "")).lower() != "rub"
            or str(estimate.get("totalOrderAmount", {}).get("currency", "")).lower() != "rub"
            or not total.is_finite()
            or not initial.is_finite()
            or initial != candidate.limit_price * i.lot_size
            or not initial <= total <= Decimal("10000")
            or total > initial * Decimal("1.005")
        ):
            raise ValueError("fee-inclusive sandbox notional exceeds cap/estimate invalid")
        funds = self._sandbox("GetSandboxWithdrawLimits")
        rub_rows = [m for m in _rows(funds, "money") if str(m.get("currency", "")).lower() == "rub"]
        if len(rub_rows) != 1:
            raise ValueError("exact unambiguous RUB cash balance required")
        cash = sum(
            (money_value_to_decimal(m) for m in rub_rows),
            Decimal(0),
        )
        for field in ("blocked", "blockedGuarantee"):
            blocked_rows = [
                m for m in _rows(funds, field) if str(m.get("currency", "")).lower() == "rub"
            ]
            if len(blocked_rows) > 1 or any(
                not money_value_to_decimal(m).is_finite() or money_value_to_decimal(m) < 0
                for m in blocked_rows
            ):
                raise ValueError("invalid/ambiguous blocked RUB balance")
            cash -= sum(
                (money_value_to_decimal(m) for m in blocked_rows),
                Decimal(0),
            )
        if not cash.is_finite() or cash < total:
            raise ValueError("sandbox cash including fees insufficient")
        book_at = self._book(candidate)
        self._validate(candidate)
        if self.clock() - started > timedelta(seconds=20):
            raise ValueError("sandbox preflight stale")
        return {"total": str(total), "account_verified": True, "book_at": book_at}

    def submit(self, candidate: CandidatePort) -> OrderRecord:
        started = self.clock()
        book_at = self.preflight(candidate)["book_at"]
        intent = OrderIntent(
            candidate.order_request_id,
            candidate.instrument,
            Side.BUY,
            1,
            candidate.limit_price,
            candidate.limit_price * candidate.instrument.lot_size,
            "sandbox one-shot long genuine signal",
        )

        def final_entry_check() -> None:
            if self.entry_gate is not None:
                self.entry_gate()
            self._validate(candidate)
            now = self.clock()
            if not timedelta(0) <= now - book_at <= timedelta(seconds=10) or not timedelta(
                0
            ) <= now - started <= timedelta(seconds=20):
                raise ValueError("sandbox entry preflight/book expired at dispatch")

        raw = self._sandbox(
            "PostSandboxOrder",
            {
                "instrumentId": intent.instrument.uid,
                "quantity": "1",
                "price": decimal_to_quotation(intent.limit_price),
                "direction": "ORDER_DIRECTION_BUY",
                "orderType": "ORDER_TYPE_LIMIT",
                "orderId": intent.order_request_id,
                "timeInForce": "TIME_IN_FORCE_DAY",
                "priceType": "PRICE_TYPE_CURRENCY",
                "confirmMarginTrade": False,
            },
            before_send=final_entry_check,
        )
        return self._decode(intent, raw)

    def _decode(self, intent: OrderIntent, raw: Mapping[str, Any]) -> OrderRecord:
        names = {
            "NEW": OrderStatus.ACCEPTED,
            "FILL": OrderStatus.FILLED,
            "PARTIALLYFILL": OrderStatus.PARTIALLY_FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
        }
        status = names.get(
            str(raw.get("executionReportStatus", "")).removeprefix("EXECUTION_REPORT_STATUS_")
        )
        if (
            raw.get("instrumentUid") != intent.instrument.uid
            or raw.get("orderRequestId") != intent.order_request_id
            or raw.get("direction") != "ORDER_DIRECTION_BUY"
            or str(raw.get("lotsRequested")) != "1"
            or str(raw.get("lotsExecuted")) not in {"0", "1"}
            or not isinstance(raw.get("orderId"), str)
            or not raw["orderId"]
            or status is None
        ):
            raise ValueError("sandbox order correlation ambiguous")
        filled = int(raw["lotsExecuted"])
        if status is OrderStatus.FILLED and filled != 1:
            raise ValueError("sandbox FILLED quantity mismatch")
        return OrderRecord(intent, status, filled, raw["orderId"])

    def lookup(self, record: OrderRecord) -> OrderRecord:
        result = self._decode(
            record.intent,
            self._sandbox(
                "GetSandboxOrderState",
                {"orderId": record.intent.order_request_id, "orderIdType": "ORDER_ID_TYPE_REQUEST"},
            ),
        )
        if record.broker_order_id is not None and record.broker_order_id != result.broker_order_id:
            raise ValueError("sandbox exchange id changed")
        return result

    def cancel_unfilled_verified(self, record: OrderRecord) -> OrderRecord:
        fresh = self.lookup(record)
        if fresh.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
            return fresh
        self._sandbox("CancelSandboxOrder", {"orderId": fresh.broker_order_id or ""})
        final = self.lookup(fresh)
        if final.status not in {OrderStatus.CANCELLED, OrderStatus.FILLED}:
            raise ValueError("sandbox cancellation unverified")
        return final

    def protect(self, candidate: CandidatePort, filled: OrderRecord) -> dict[str, Any]:
        self._validate(candidate, entry=False)
        actual = self.lookup(filled)
        if (
            actual.status is not OrderStatus.FILLED
            or actual.filled_lots != 1
            or actual.intent.order_request_id != candidate.order_request_id
            or actual.intent.instrument != candidate.instrument
        ):
            raise ValueError("exact correlated sandbox FILLED parent required")
        positions = self._sandbox("GetSandboxPositions")
        holding = [
            p
            for p in _rows(positions, "securities")
            if p.get("instrumentUid") == candidate.instrument.uid
        ]
        if (
            positions.get("accountId", self.account_id) != self.account_id
            or positions.get("limitsLoadingInProgress") is True
            or any(not p.get("instrumentUid") for p in _rows(positions, "securities"))
            or len(holding) != 1
            or str(holding[0].get("balance")) != str(candidate.instrument.lot_size)
            or str(holding[0].get("blocked")) != "0"
            or holding[0].get("exchangeBlocked") is True
            or _rows(self._sandbox("GetSandboxOrders"), "orders")
            or any(
                not s.get("instrumentUid") or s.get("instrumentUid") == candidate.instrument.uid
                for s in _rows(
                    self._sandbox("GetSandboxStopOrders", {"status": "STOP_ORDER_STATUS_ACTIVE"}),
                    "stopOrders",
                )
            )
        ):
            raise ValueError("sandbox holding/ordinary orders/stop conflict")
        self._book(candidate, entry=False)
        self._validate(candidate, entry=False)
        raw = dict(
            self._sandbox(
                "PostSandboxStopOrder",
                {
                    "instrumentId": candidate.instrument.uid,
                    "quantity": "1",
                    "direction": "STOP_ORDER_DIRECTION_SELL",
                    "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
                    "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
                    "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET",
                    "orderId": candidate.stop_request_id,
                    "stopPrice": decimal_to_quotation(candidate.stop_trigger),
                    "priceType": "PRICE_TYPE_CURRENCY",
                    "confirmMarginTrade": False,
                },
            )
        )
        return self.verify_protection(candidate, raw)

    def verify_protection(
        self, candidate: CandidatePort, stop_response: Mapping[str, Any]
    ) -> dict[str, Any]:
        if (
            stop_response.get("orderRequestId") != candidate.stop_request_id
            or not isinstance(stop_response.get("stopOrderId"), str)
            or not stop_response["stopOrderId"]
        ):
            raise ValueError("sandbox stop POST correlation ambiguous; never retry")
        stops = _rows(
            self._sandbox("GetSandboxStopOrders", {"status": "STOP_ORDER_STATUS_ACTIVE"}),
            "stopOrders",
        )
        matches = [s for s in stops if s.get("instrumentUid") == candidate.instrument.uid]
        if len(matches) != 1 or any(not s.get("instrumentUid") for s in stops):
            raise ValueError("sandbox stop readback ambiguous")
        row = matches[0]
        expected = {
            "stopOrderId": stop_response["stopOrderId"],
            "lotsRequested": "1",
            "direction": "STOP_ORDER_DIRECTION_SELL",
            "orderType": "STOP_ORDER_TYPE_STOP_LOSS",
            "status": "STOP_ORDER_STATUS_ACTIVE",
        }
        if (
            any(str(row.get(k)) != str(v) for k, v in expected.items())
            or str(row.get("currency", "")).lower() != "rub"
            or str(row.get("stopPrice", {}).get("currency", "")).lower() != "rub"
            or row.get("expirationType")
            not in (None, "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL")
            or row.get("exchangeOrderType")
            not in (None, "EXCHANGE_ORDER_TYPE_UNSPECIFIED", "EXCHANGE_ORDER_TYPE_MARKET", 0, "0")
            or money_value_to_decimal(row.get("stopPrice", {})) != candidate.stop_trigger
            or any(row.get(k) for k in ("expirationTime", "exchangeOrderId", "activationDateTime"))
        ):
            raise ValueError("sandbox stop fields mismatched")
        return dict(stop_response, verified=True)
