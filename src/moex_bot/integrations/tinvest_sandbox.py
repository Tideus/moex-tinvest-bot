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
SANDBOX_SERVICE_PATH = "/tinkoff.public.invest.api.contract.v1.SandboxService"
GET_SANDBOX_ACCOUNTS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxAccounts"
OPEN_SANDBOX_ACCOUNT_PATH = f"{SANDBOX_SERVICE_PATH}/OpenSandboxAccount"
GET_SANDBOX_WITHDRAW_LIMITS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxWithdrawLimits"
SANDBOX_PAY_IN_PATH = f"{SANDBOX_SERVICE_PATH}/SandboxPayIn"
MAX_SANDBOX_PAY_IN_RUB = Decimal("30000000")


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


def money_value_to_decimal(value: Mapping[str, object]) -> Decimal:
    try:
        units = Decimal(str(value.get("units", "0")))
        nano = Decimal(str(value.get("nano", 0))) / Decimal("1000000000")
    except Exception as exc:
        raise ValueError("invalid T-Invest MoneyValue") from exc
    return units + nano


@dataclass(frozen=True, slots=True)
class SandboxAccount:
    account_id: str
    name: str
    status: str

    @property
    def is_open(self) -> bool:
        return self.status in {"", "ACCOUNT_STATUS_OPEN"}


@dataclass(frozen=True, slots=True)
class SandboxAccountBootstrap:
    account_id: str
    created: bool


@dataclass(slots=True)
class TInvestSandboxAccountService:
    """Account bootstrap and cash operations, fixed to the sandbox host."""

    token: str
    transport: JsonPostTransport
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("sandbox token is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @classmethod
    def from_environment(cls) -> TInvestSandboxAccountService:
        return cls(os.getenv("T_INVEST_SANDBOX_TOKEN", ""), UrlLibJsonTransport())

    def _post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, Any]:
        return self.transport.post(
            SANDBOX_BASE_URL + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )

    def accounts(self) -> tuple[SandboxAccount, ...]:
        response = self._post(GET_SANDBOX_ACCOUNTS_PATH, {})
        raw_accounts = response.get("accounts", [])
        if not isinstance(raw_accounts, list):
            raise ValueError("unexpected GetSandboxAccounts response")
        accounts: list[SandboxAccount] = []
        for raw in raw_accounts:
            if not isinstance(raw, Mapping):
                raise ValueError("unexpected sandbox account record")
            account_id = str(raw.get("id", "")).strip()
            if not account_id:
                raise ValueError("sandbox account response has no id")
            accounts.append(
                SandboxAccount(
                    account_id=account_id,
                    name=str(raw.get("name", "")),
                    status=str(raw.get("status", "")),
                )
            )
        return tuple(accounts)

    def open_account(self, name: str) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("sandbox account name is required")
        response = self._post(OPEN_SANDBOX_ACCOUNT_PATH, {"name": clean_name})
        account_id = str(response.get("accountId") or response.get("account_id") or "").strip()
        if not account_id:
            raise ValueError("OpenSandboxAccount response has no account id")
        return account_id

    def ensure_account(
        self, configured_account_id: str | None, *, account_name: str
    ) -> SandboxAccountBootstrap:
        accounts = self.accounts()
        configured = (configured_account_id or "").strip()
        if configured:
            for account in accounts:
                if account.account_id == configured and account.is_open:
                    return SandboxAccountBootstrap(account.account_id, created=False)

        # Reuse our own still-open named account to remain idempotent if .env was lost.
        matching = sorted(
            (
                account
                for account in accounts
                if account.is_open and account.name == account_name.strip()
            ),
            key=lambda account: account.account_id,
        )
        if matching:
            return SandboxAccountBootstrap(matching[0].account_id, created=False)

        return SandboxAccountBootstrap(self.open_account(account_name), created=True)

    def available_rub_balance(self, account_id: str) -> Decimal:
        response = self._post(
            GET_SANDBOX_WITHDRAW_LIMITS_PATH, {"accountId": account_id.strip()}
        )
        money = response.get("money", [])
        if not isinstance(money, list):
            raise ValueError("unexpected GetSandboxWithdrawLimits response")
        balance = Decimal("0")
        for item in money:
            if not isinstance(item, Mapping):
                raise ValueError("unexpected money record")
            if str(item.get("currency", "")).lower() == "rub":
                balance += money_value_to_decimal(item)
        return balance

    def pay_in(self, account_id: str, amount: Decimal) -> Decimal:
        if not amount.is_finite() or amount <= 0:
            raise ValueError("sandbox top-up must be a positive finite amount")
        if amount > MAX_SANDBOX_PAY_IN_RUB:
            raise ValueError(
                f"sandbox top-up exceeds {MAX_SANDBOX_PAY_IN_RUB} RUB per operation"
            )
        response = self._post(
            SANDBOX_PAY_IN_PATH,
            {
                "accountId": account_id.strip(),
                "amount": {"currency": "rub", **decimal_to_quotation(amount)},
            },
        )
        balance = response.get("balance")
        if not isinstance(balance, Mapping):
            raise ValueError("SandboxPayIn response has no balance")
        if str(balance.get("currency", "")).lower() != "rub":
            raise ValueError("SandboxPayIn returned a non-RUB balance")
        return money_value_to_decimal(balance)


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
