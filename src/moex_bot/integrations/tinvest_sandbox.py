from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from ..domain import ExecutionMode, OrderIntent, OrderRecord, OrderStatus, Side
from ..service_config import SANDBOX_REST

SANDBOX_BASE_URL = SANDBOX_REST
POST_ORDER_PATH = "/tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder"


class JsonPostTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class UrlLibJsonTransport:
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("unexpected T-Invest response")
        return decoded


def decimal_to_quotation(value: Decimal) -> dict[str, object]:
    units = int(value.to_integral_value(rounding=ROUND_DOWN))
    nano = int((value - Decimal(units)) * Decimal("1000000000"))
    return {"units": str(units), "nano": nano}


def _status(value: object) -> OrderStatus:
    statuses = {
        "EXECUTION_REPORT_STATUS_NEW": OrderStatus.ACCEPTED,
        "EXECUTION_REPORT_STATUS_PARTIALLYFILL": OrderStatus.PARTIALLY_FILLED,
        "EXECUTION_REPORT_STATUS_FILL": OrderStatus.FILLED,
        "EXECUTION_REPORT_STATUS_REJECTED": OrderStatus.REJECTED,
        "EXECUTION_REPORT_STATUS_CANCELLED": OrderStatus.CANCELLED,
    }
    return statuses.get(str(value), OrderStatus.UNKNOWN)


@dataclass(slots=True)
class TInvestSandboxExecutionAdapter:
    """Mutation-capable only against the fixed T-Invest sandbox endpoint."""

    token: str
    account_id: str
    transport: JsonPostTransport
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.token.strip() or not self.account_id.strip():
            raise ValueError("sandbox token and account_id are required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @classmethod
    def from_environment(cls) -> TInvestSandboxExecutionAdapter:
        token = os.getenv("T_INVEST_SANDBOX_TOKEN", "")
        account_id = os.getenv("T_INVEST_SANDBOX_ACCOUNT_ID", "")
        return cls(token, account_id, UrlLibJsonTransport())

    @property
    def mode(self) -> ExecutionMode:
        return ExecutionMode.SANDBOX

    def submit(self, intent: OrderIntent) -> OrderRecord:
        try:
            UUID(intent.order_request_id)
        except ValueError as exc:
            raise ValueError("T-Invest order_request_id must be a UUID") from exc
        payload: dict[str, object] = {
            "quantity": str(intent.lots),
            "price": decimal_to_quotation(intent.limit_price),
            "direction": (
                "ORDER_DIRECTION_BUY" if intent.side is Side.BUY else "ORDER_DIRECTION_SELL"
            ),
            "accountId": self.account_id,
            "orderType": "ORDER_TYPE_LIMIT",
            "orderId": intent.order_request_id,
            "instrumentId": intent.instrument.uid,
            "timeInForce": "TIME_IN_FORCE_DAY",
            "priceType": "PRICE_TYPE_CURRENCY",
            "confirmMarginTrade": False,
        }
        try:
            response = self.transport.post(
                SANDBOX_BASE_URL + POST_ORDER_PATH,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except (TimeoutError, HTTPError, URLError):
            # Unknown delivery state: reconciliation must decide; never retry blindly.
            return OrderRecord(intent=intent, status=OrderStatus.UNKNOWN)
        status = _status(response.get("executionReportStatus"))
        filled = intent.lots if status is OrderStatus.FILLED else 0
        return OrderRecord(
            intent=intent,
            status=status,
            filled_lots=filled,
            broker_order_id=(str(response["orderId"]) if response.get("orderId") else None),
        )
