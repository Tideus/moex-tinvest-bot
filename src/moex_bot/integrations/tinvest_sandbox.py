from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from ..domain import ExecutionMode, Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from ..performance import TradeOperation
from ..service_config import SANDBOX_REST

SANDBOX_BASE_URL = SANDBOX_REST
POST_ORDER_PATH = "/tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder"
GET_ORDER_STATE_PATH = "/tinkoff.public.invest.api.contract.v1.OrdersService/GetOrderState"
GET_INSTRUMENT_PATH = (
    "/tinkoff.public.invest.api.contract.v1.InstrumentsService/GetInstrumentBy"
)
SANDBOX_SERVICE_PATH = "/tinkoff.public.invest.api.contract.v1.SandboxService"
GET_SANDBOX_ACCOUNTS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxAccounts"
OPEN_SANDBOX_ACCOUNT_PATH = f"{SANDBOX_SERVICE_PATH}/OpenSandboxAccount"
CLOSE_SANDBOX_ACCOUNT_PATH = f"{SANDBOX_SERVICE_PATH}/CloseSandboxAccount"
GET_SANDBOX_STOP_ORDERS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxStopOrders"
GET_SANDBOX_WITHDRAW_LIMITS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxWithdrawLimits"
GET_SANDBOX_PORTFOLIO_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxPortfolio"
GET_SANDBOX_POSITIONS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxPositions"
GET_SANDBOX_ORDERS_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxOrders"
GET_SANDBOX_ORDER_STATE_PATH = f"{SANDBOX_SERVICE_PATH}/GetSandboxOrderState"
CANCEL_SANDBOX_ORDER_PATH = f"{SANDBOX_SERVICE_PATH}/CancelSandboxOrder"
GET_SANDBOX_OPERATIONS_BY_CURSOR_PATH = (
    f"{SANDBOX_SERVICE_PATH}/GetSandboxOperationsByCursor"
)
OPERATIONS_SERVICE_PATH = "/tinkoff.public.invest.api.contract.v1.OperationsService"
ORDERS_SERVICE_PATH = "/tinkoff.public.invest.api.contract.v1.OrdersService"
GET_PORTFOLIO_PATH = f"{OPERATIONS_SERVICE_PATH}/GetPortfolio"
GET_POSITIONS_PATH = f"{OPERATIONS_SERVICE_PATH}/GetPositions"
GET_OPERATIONS_BY_CURSOR_PATH = f"{OPERATIONS_SERVICE_PATH}/GetOperationsByCursor"
GET_ORDERS_PATH = f"{ORDERS_SERVICE_PATH}/GetOrders"
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


def quotation_to_decimal(value: Mapping[str, object]) -> Decimal:
    return money_value_to_decimal(value)


def _rub_total(items: object, *, label: str) -> Decimal:
    if not isinstance(items, list):
        raise ValueError(f"unexpected {label} response")
    result = Decimal("0")
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError(f"unexpected {label} record")
        if str(item.get("currency", "")).lower() == "rub":
            result += money_value_to_decimal(item)
    return result


@dataclass(frozen=True, slots=True)
class SandboxBrokerSnapshot:
    account_id: str
    cash_available: Decimal
    cash_blocked: Decimal
    reported_equity: Decimal
    positions_lots: Mapping[str, int]
    blocked_lots: Mapping[str, int]
    position_values: Mapping[str, Decimal]
    open_orders: int
    source: str = "t_invest_sandbox"

    def as_portfolio_payload(self, *, daily_turnover: Decimal = Decimal("0")) -> dict[str, object]:
        return {
            "source": self.source,
            "account_id": self.account_id,
            "cash": str(self.cash_available),
            "blocked_cash": str(self.cash_blocked),
            "reported_equity": str(self.reported_equity),
            "positions": {
                secid: {
                    "lots": lots,
                    "blocked_lots": self.blocked_lots.get(secid, 0),
                    **(
                        {"current_value": str(self.position_values[secid])}
                        if secid in self.position_values
                        else {}
                    ),
                }
                for secid, lots in sorted(self.positions_lots.items())
            },
            "daily_turnover": str(daily_turnover),
            "open_orders": self.open_orders,
        }


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

    def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        base_url: str = SANDBOX_BASE_URL,
    ) -> Mapping[str, Any]:
        return self.transport.post(
            base_url + path,
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

    def account_state(self, account_id: str) -> dict[str, object]:
        """Read-only raw state for an explicit sandbox account lifecycle preview."""
        if not account_id.strip():
            raise ValueError("sandbox account id required")
        return {
            "portfolio": dict(self._post(GET_SANDBOX_PORTFOLIO_PATH,
                                         {"accountId": account_id, "currency": "RUB"})),
            "positions": dict(self._post(GET_SANDBOX_POSITIONS_PATH, {"accountId": account_id})),
            "orders": dict(self._post(GET_SANDBOX_ORDERS_PATH, {"accountId": account_id})),
            "stop_orders": dict(self._post(GET_SANDBOX_STOP_ORDERS_PATH,
                                           {"accountId": account_id})),
        }

    def close_account(self, account_id: str) -> None:
        """Destructive sandbox-only primitive; callers must preview, confirm and verify."""
        if not account_id.strip():
            raise ValueError("sandbox account id required")
        self._post(CLOSE_SANDBOX_ACCOUNT_PATH, {"accountId": account_id})

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

    def operations(
        self,
        account_id: str,
        instruments_by_uid: Mapping[str, Instrument],
        *,
        from_time: datetime,
        to_time: datetime,
        base_url: str = SANDBOX_BASE_URL,
        operations_path: str = GET_SANDBOX_OPERATIONS_BY_CURSOR_PATH,
    ) -> tuple[TradeOperation, ...]:
        """Read executed broker operations, following the official cursor contract."""
        if from_time.tzinfo is None or to_time.tzinfo is None or from_time >= to_time:
            raise ValueError("operation range must be ordered and timezone-aware")
        cursor = ""
        result: list[TradeOperation] = []
        seen: set[str] = set()
        for _page in range(100):
            payload: dict[str, object] = {
                "accountId": account_id.strip(),
                "from": from_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "to": to_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "state": "OPERATION_STATE_EXECUTED",
                "limit": 1000,
                "withoutCommissions": False,
                "withoutTrades": False,
                "withoutOvernights": False,
            }
            if cursor:
                payload["cursor"] = cursor
            response = self._post(operations_path, payload, base_url=base_url)
            raw_items = response.get("items", [])
            if not isinstance(raw_items, list):
                raise ValueError("unexpected operations response")
            for raw in raw_items:
                operation = _trade_operation(raw, instruments_by_uid)
                if operation is not None and operation.operation_id not in seen:
                    seen.add(operation.operation_id)
                    result.append(operation)
            if response.get("hasNext") is not True:
                break
            next_cursor = str(response.get("nextCursor", "")).strip()
            if not next_cursor or next_cursor == cursor:
                raise ValueError("operations cursor did not advance")
            cursor = next_cursor
        else:
            raise ValueError("operations pagination exceeded 100 pages")
        return tuple(sorted(result, key=lambda item: (item.occurred_at, item.operation_id)))

    def broker_snapshot(
        self,
        account_id: str,
        instruments_by_uid: Mapping[str, Instrument],
        *,
        base_url: str = SANDBOX_BASE_URL,
        portfolio_path: str = GET_SANDBOX_PORTFOLIO_PATH,
        positions_path: str = GET_SANDBOX_POSITIONS_PATH,
        orders_path: str = GET_SANDBOX_ORDERS_PATH,
        source: str = "t_invest_sandbox",
    ) -> SandboxBrokerSnapshot:
        clean_account_id = account_id.strip()
        if not clean_account_id:
            raise ValueError("sandbox account id is required")
        payload = {"accountId": clean_account_id}
        portfolio = self._post(
            portfolio_path, {**payload, "currency": "RUB"}, base_url=base_url
        )
        positions = self._post(positions_path, payload, base_url=base_url)
        orders = self._post(orders_path, payload, base_url=base_url)

        if positions.get("limitsLoadingInProgress") is True:
            raise ValueError("sandbox position limits are still loading")
        cash_available = _rub_total(positions.get("money", []), label="money")
        cash_blocked = _rub_total(positions.get("blocked", []), label="blocked money")
        equity_raw = portfolio.get("totalAmountPortfolio")
        if not isinstance(equity_raw, Mapping):
            raise ValueError("GetSandboxPortfolio response has no totalAmountPortfolio")
        reported_equity = money_value_to_decimal(equity_raw)
        raw_positions = portfolio.get("positions", [])
        if not isinstance(raw_positions, list):
            raise ValueError("unexpected GetSandboxPortfolio positions response")

        positions_lots: dict[str, int] = {}
        blocked_lots: dict[str, int] = {}
        position_values: dict[str, Decimal] = {}
        for raw in raw_positions:
            if not isinstance(raw, Mapping):
                raise ValueError("unexpected sandbox portfolio position")
            quantity_raw = raw.get("quantity")
            if not isinstance(quantity_raw, Mapping):
                raise ValueError("sandbox portfolio position has no quantity")
            quantity = quotation_to_decimal(quantity_raw)
            if quantity == 0:
                continue
            instrument_type = str(raw.get("instrumentType", "")).lower()
            if instrument_type == "currency":
                continue
            uid = str(raw.get("instrumentUid", "")).strip()
            instrument = instruments_by_uid.get(uid)
            if instrument is None:
                raise ValueError(f"non-zero sandbox position outside verified universe: {uid}")
            lots = quantity / Decimal(instrument.lot_size)
            if lots != lots.to_integral_value():
                raise ValueError(
                    f"position quantity is not divisible by lot size: {instrument.secid}"
                )
            blocked_raw = raw.get("blockedLots", {"units": "0", "nano": 0})
            if not isinstance(blocked_raw, Mapping):
                raise ValueError("invalid blockedLots in sandbox portfolio")
            blocked = quotation_to_decimal(blocked_raw)
            if blocked != blocked.to_integral_value() or blocked < 0:
                raise ValueError(f"invalid blocked lots for {instrument.secid}")
            positions_lots[instrument.secid] = int(lots)
            blocked_lots[instrument.secid] = int(blocked)
            current_price_raw = raw.get("currentPrice")
            if isinstance(current_price_raw, Mapping):
                position_values[instrument.secid] = (
                    quotation_to_decimal(current_price_raw) * quantity
                )

        raw_orders = orders.get("orders", [])
        if not isinstance(raw_orders, list):
            raise ValueError("unexpected GetSandboxOrders response")
        return SandboxBrokerSnapshot(
            account_id=clean_account_id,
            cash_available=cash_available,
            cash_blocked=cash_blocked,
            reported_equity=reported_equity,
            positions_lots=positions_lots,
            blocked_lots=blocked_lots,
            position_values=position_values,
            open_orders=len(raw_orders),
            source=source,
        )

    def reconcile_cancel_active_orders(self, account_id: str) -> tuple[str, ...]:
        """Cancel every still-active order on a dedicated sandbox account, then verify empty."""
        clean = account_id.strip()
        if not clean:
            raise ValueError("sandbox account id is required")
        response = self._post(GET_SANDBOX_ORDERS_PATH, {"accountId": clean})
        raw_orders = response.get("orders", [])
        if not isinstance(raw_orders, list):
            raise ValueError("unexpected GetSandboxOrders response")
        cancelled: list[str] = []
        for raw in raw_orders:
            if not isinstance(raw, Mapping):
                raise ValueError("unexpected active sandbox order")
            order_id = str(raw.get("orderId", "")).strip()
            if not order_id:
                raise ValueError("active sandbox order has no orderId")
            self._post(
                CANCEL_SANDBOX_ORDER_PATH,
                {"accountId": clean, "orderId": order_id},
            )
            cancelled.append(order_id)
        verified = self._post(GET_SANDBOX_ORDERS_PATH, {"accountId": clean})
        remaining = verified.get("orders", [])
        if not isinstance(remaining, list) or remaining:
            raise ValueError("sandbox active-order reconciliation is incomplete")
        return tuple(cancelled)


def _trade_operation(
    raw: object, instruments_by_uid: Mapping[str, Instrument]
) -> TradeOperation | None:
    if not isinstance(raw, Mapping):
        raise ValueError("unexpected operation record")
    operation_id = str(raw.get("id", "")).strip()
    date_raw = str(raw.get("date", "")).strip()
    if not operation_id or not date_raw:
        raise ValueError("operation has no id or date")
    occurred_at = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
    operation_type = str(raw.get("type", ""))
    payment_raw = raw.get("payment", {})
    payment = (
        money_value_to_decimal(payment_raw) if isinstance(payment_raw, Mapping) else Decimal("0")
    )
    external_cashflow = Decimal("0")
    if operation_type.startswith("OPERATION_TYPE_INPUT"):
        external_cashflow = abs(payment)
    elif operation_type.startswith("OPERATION_TYPE_OUTPUT"):
        external_cashflow = -abs(payment)

    side = None
    if operation_type in {"OPERATION_TYPE_BUY", "OPERATION_TYPE_BUY_CARD"}:
        side = "BUY"
    elif operation_type == "OPERATION_TYPE_SELL":
        side = "SELL"

    uid = str(raw.get("instrumentUid", "")).strip()
    instrument = instruments_by_uid.get(uid)
    if side is not None and instrument is None:
        raise ValueError(f"executed operation outside verified universe: {uid}")
    trades_info = raw.get("tradesInfo", {})
    trades = trades_info.get("trades", []) if isinstance(trades_info, Mapping) else []
    if not isinstance(trades, list):
        raise ValueError("invalid operation trades")
    quantity = 0
    gross = Decimal("0")
    for trade in trades:
        if not isinstance(trade, Mapping):
            raise ValueError("invalid operation trade")
        trade_quantity = int(trade.get("quantity", 0))
        price_raw = trade.get("price", {})
        if not isinstance(price_raw, Mapping) or trade_quantity < 0:
            raise ValueError("invalid operation trade price or quantity")
        quantity += trade_quantity
        gross += money_value_to_decimal(price_raw) * trade_quantity
    commission_raw = raw.get("commission", {})
    commission = (
        abs(money_value_to_decimal(commission_raw))
        if isinstance(commission_raw, Mapping)
        else Decimal("0")
    )
    return TradeOperation(
        operation_id=operation_id,
        occurred_at=occurred_at,
        secid=None if instrument is None else instrument.secid,
        side=side,
        quantity=quantity,
        gross=abs(gross),
        commission=commission,
        external_cashflow=external_cashflow,
    )


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
    poll_interval_seconds: float = 2.0
    sleeper: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if not self.token.strip() or not self.account_id.strip():
            raise ValueError("sandbox token and account_id are required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

    @classmethod
    def from_environment(
        cls, *, account_id_env: str = "T_INVEST_SANDBOX_ACCOUNT_ID"
    ) -> TInvestSandboxExecutionAdapter:
        token = os.getenv("T_INVEST_SANDBOX_TOKEN", "")
        account_id = os.getenv(account_id_env, "")
        return cls(token, account_id, UrlLibJsonTransport())

    @property
    def mode(self) -> ExecutionMode:
        return ExecutionMode.SANDBOX

    def submit(self, intent: OrderIntent) -> OrderRecord:
        try:
            UUID(intent.order_request_id)
        except ValueError as exc:
            raise ValueError("T-Invest order_request_id must be a UUID") from exc
        if intent.confirm_margin_trade:
            instrument_response = self.transport.post(
                SANDBOX_BASE_URL + GET_INSTRUMENT_PATH,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                payload={
                    "idType": "INSTRUMENT_ID_TYPE_UID",
                    "id": intent.instrument.uid,
                },
                timeout_seconds=self.timeout_seconds,
            )
            instrument = instrument_response.get("instrument")
            if not isinstance(instrument, Mapping):
                raise ValueError("T-Invest instrument availability response is incomplete")
            if instrument.get("uid") != intent.instrument.uid:
                raise ValueError("T-Invest instrument UID changed during short verification")
            if instrument.get("apiTradeAvailableFlag") is not True:
                raise ValueError("T-Invest API trading is unavailable for short instrument")
            if instrument.get("shortEnabledFlag") is not True:
                raise ValueError("T-Invest short is unavailable for instrument")
        payload: dict[str, object] = {
            "quantity": str(intent.lots),
            "direction": (
                "ORDER_DIRECTION_BUY" if intent.side is Side.BUY else "ORDER_DIRECTION_SELL"
            ),
            "accountId": self.account_id,
            "orderType": (
                "ORDER_TYPE_MARKET" if intent.order_type == "market" else "ORDER_TYPE_LIMIT"
            ),
            "orderId": intent.order_request_id,
            "instrumentId": intent.instrument.uid,
            "timeInForce": "TIME_IN_FORCE_DAY",
            "priceType": "PRICE_TYPE_CURRENCY",
            "confirmMarginTrade": intent.confirm_margin_trade,
        }
        if intent.order_type == "limit":
            payload["price"] = decimal_to_quotation(intent.limit_price)
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
        except (TimeoutError, HTTPError, URLError) as exc:
            # Unknown delivery state: reconciliation must decide; never retry blindly.
            detail = type(exc).__name__
            if isinstance(exc, HTTPError):
                detail += f":HTTP {exc.code}"
            return OrderRecord(
                intent=intent,
                status=OrderStatus.UNKNOWN,
                broker_order_id=intent.order_request_id,
                status_detail=detail,
            )
        status = _status(response.get("executionReportStatus"))
        filled = int(response.get("lotsExecuted", 0))
        if status is OrderStatus.FILLED and filled != intent.lots:
            raise ValueError("filled order state has inconsistent lotsExecuted")
        if not 0 <= filled <= intent.lots:
            raise ValueError("sandbox response has invalid lotsExecuted")
        return OrderRecord(
            intent=intent,
            status=status,
            filled_lots=filled,
            broker_order_id=(
                str(response["orderId"])
                if response.get("orderId")
                else intent.order_request_id
            ),
        )

    def lookup(self, record: OrderRecord) -> OrderRecord:
        """Read-only crash recovery by request UUID. Missing state remains unresolved."""
        try:
            state = self.transport.post(
                SANDBOX_BASE_URL + GET_SANDBOX_ORDER_STATE_PATH,
                headers=self._headers(),
                payload={"accountId": self.account_id,
                         "orderId": record.intent.order_request_id,
                         "orderIdType": "ORDER_ID_TYPE_REQUEST",
                         "priceType": "PRICE_TYPE_CURRENCY"},
                timeout_seconds=self.timeout_seconds,
            )
        except (TimeoutError, HTTPError, URLError) as exc:
            return OrderRecord(record.intent, OrderStatus.UNKNOWN, record.filled_lots,
                               record.broker_order_id, self._error_detail(exc))
        filled = int(state.get("lotsExecuted", 0))
        status = _status(state.get("executionReportStatus"))
        if not 0 <= filled <= record.intent.lots:
            raise ValueError("sandbox order state has invalid lotsExecuted")
        if status is OrderStatus.FILLED and filled != record.intent.lots:
            raise ValueError("filled order state has inconsistent lotsExecuted")
        for field, expected in (
            ("instrumentUid", record.intent.instrument.uid),
            ("orderRequestId", record.intent.order_request_id),
        ):
            if field in state and state[field] != expected:
                raise ValueError(f"sandbox order state has mismatched {field}")
        if "lotsRequested" in state and int(state["lotsRequested"]) != record.intent.lots:
            raise ValueError("sandbox order state has mismatched lotsRequested")
        return OrderRecord(record.intent, status, filled,
                           str(state.get("orderId") or record.broker_order_id or
                               record.intent.order_request_id))

    def settle(
        self, records: tuple[OrderRecord, ...], *, ttl_seconds: int
    ) -> tuple[OrderRecord, ...]:
        """Resolve accepted/unknown orders, then cancel non-filled orders at their TTL."""
        if ttl_seconds < 0:
            raise ValueError("order ttl must not be negative")
        settled = list(records)
        pending = {
            index
            for index, record in enumerate(settled)
            if record.status
            in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
            and record.broker_order_id
        }
        deadline = self.monotonic() + ttl_seconds
        while pending:
            for index in tuple(pending):
                record = settled[index]
                try:
                    state = self.transport.post(
                        SANDBOX_BASE_URL + GET_ORDER_STATE_PATH,
                        headers=self._headers(),
                        payload={
                            "accountId": self.account_id,
                            "orderId": record.intent.order_request_id,
                            "orderIdType": "ORDER_ID_TYPE_REQUEST",
                            "priceType": "PRICE_TYPE_CURRENCY",
                        },
                        timeout_seconds=self.timeout_seconds,
                    )
                except (TimeoutError, HTTPError, URLError) as exc:
                    settled[index] = OrderRecord(
                        intent=record.intent,
                        status=OrderStatus.UNKNOWN,
                        filled_lots=record.filled_lots,
                        broker_order_id=record.broker_order_id,
                        status_detail=self._error_detail(exc),
                    )
                    continue
                status = _status(state.get("executionReportStatus"))
                filled = int(state.get("lotsExecuted", record.filled_lots))
                if status is OrderStatus.FILLED and filled != record.intent.lots:
                    raise ValueError("filled order state has inconsistent lotsExecuted")
                if not 0 <= filled <= record.intent.lots:
                    raise ValueError("sandbox order state has invalid lotsExecuted")
                settled[index] = OrderRecord(
                    intent=record.intent,
                    status=status,
                    filled_lots=filled,
                    broker_order_id=record.broker_order_id,
                )
                if status in {
                    OrderStatus.FILLED,
                    OrderStatus.REJECTED,
                    OrderStatus.CANCELLED,
                }:
                    pending.remove(index)
            if not pending:
                break
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleeper(min(self.poll_interval_seconds, remaining))
        for index in tuple(pending):
            record = settled[index]
            try:
                self.transport.post(
                    SANDBOX_BASE_URL + CANCEL_SANDBOX_ORDER_PATH,
                    headers=self._headers(),
                    payload={
                        "accountId": self.account_id,
                        "orderId": record.intent.order_request_id,
                        "orderIdType": "ORDER_ID_TYPE_REQUEST",
                    },
                    timeout_seconds=self.timeout_seconds,
                )
            except (TimeoutError, HTTPError, URLError) as exc:
                settled[index] = OrderRecord(
                    intent=record.intent,
                    status=OrderStatus.UNKNOWN,
                    filled_lots=record.filled_lots,
                    broker_order_id=record.broker_order_id,
                    status_detail=f"cancel:{self._error_detail(exc)}",
                )
            else:
                try:
                    final_state = self.transport.post(
                        SANDBOX_BASE_URL + GET_ORDER_STATE_PATH,
                        headers=self._headers(),
                        payload={
                            "accountId": self.account_id,
                            "orderId": record.intent.order_request_id,
                            "orderIdType": "ORDER_ID_TYPE_REQUEST",
                            "priceType": "PRICE_TYPE_CURRENCY",
                        },
                        timeout_seconds=self.timeout_seconds,
                    )
                except (TimeoutError, HTTPError, URLError) as exc:
                    settled[index] = OrderRecord(
                        intent=record.intent,
                        status=OrderStatus.UNKNOWN,
                        filled_lots=record.filled_lots,
                        broker_order_id=record.broker_order_id,
                        status_detail=f"cancel_verify:{self._error_detail(exc)}",
                    )
                else:
                    final_status = _status(final_state.get("executionReportStatus"))
                    final_filled = int(final_state.get("lotsExecuted", record.filled_lots))
                    if not 0 <= final_filled <= record.intent.lots:
                        raise ValueError("sandbox order state has invalid lotsExecuted")
                    if final_status is OrderStatus.FILLED and final_filled != record.intent.lots:
                        raise ValueError("filled order state has inconsistent lotsExecuted")
                    terminal = final_status in {
                        OrderStatus.FILLED,
                        OrderStatus.REJECTED,
                        OrderStatus.CANCELLED,
                    }
                    settled[index] = OrderRecord(
                        intent=record.intent,
                        status=final_status if terminal else OrderStatus.UNKNOWN,
                        filled_lots=final_filled,
                        broker_order_id=record.broker_order_id,
                        status_detail=(
                            "ttl_expired"
                            if final_status is OrderStatus.CANCELLED
                            else None if terminal else "cancel_not_final"
                        ),
                    )
        return tuple(settled)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _error_detail(exc: BaseException) -> str:
        detail = type(exc).__name__
        if isinstance(exc, HTTPError):
            detail += f":HTTP {exc.code}"
        return detail
