"""Read-only IIS buying-power checks. Never a source of execution permission."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import OrderIntent, Side
from .integrations.tinvest_sandbox import (
    JsonPostTransport,
    UrlLibJsonTransport,
    decimal_to_quotation,
)
from .service_config import PROD_REST

PREFIX = "/tinkoff.public.invest.api.contract.v1."
WITHDRAW = PREFIX + "OperationsService/GetWithdrawLimits"
MAX_LOTS = PREFIX + "OrdersService/GetMaxLots"
ORDER_PRICE = PREFIX + "OrdersService/GetOrderPrice"
MAX_AGE_SECONDS = 300


def _now() -> datetime:
    return datetime.now(UTC)


def number(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid broker funding number") from exc
    if not result.is_finite():
        raise ValueError("non-finite broker funding number")
    return result


def _quotation(value: object) -> Decimal:
    if not isinstance(value, Mapping) or not ({"units", "nano"} & value.keys()):
        raise ValueError("missing broker funding quotation")
    units, nano = number(value.get("units", 0)), number(value.get("nano", 0))
    if units != int(units) or nano != int(nano) or abs(nano) >= 1000000000 or units * nano < 0:
        raise ValueError("invalid broker funding quotation")
    return units + nano / Decimal("1000000000")


def _money(value: object) -> Decimal:
    if not isinstance(value, Mapping) or str(value.get("currency", "")).lower() != "rub":
        raise ValueError("broker funding currency must be RUB")
    return _quotation(value)


def _rub(rows: object) -> Decimal:
    if not isinstance(rows, list):
        raise ValueError("missing broker funding money list")
    total = Decimal(0)
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("currency"), str):
            raise ValueError("invalid broker funding money row")
        currency = row["currency"].lower()
        if not currency or currency in seen:
            raise ValueError("duplicate or empty broker funding currency")
        seen.add(currency)
        amount = _quotation(row)
        if currency == "rub":
            total += amount
    return total


def binding(account_id: str) -> str:
    return hashlib.sha256(account_id.encode()).hexdigest()


def verified_funding(raw: object, account_id: str, now: datetime) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("protocol") != "prod-observer-funding-v2":
        raise ValueError("PROD OBSERVER: missing GetWithdrawLimits evidence; refresh snapshot")
    if raw.get("account_binding") != binding(account_id):
        raise ValueError("PROD OBSERVER: funding belongs to a different account")
    started = datetime.fromisoformat(str(raw.get("started_at", "")))
    observed = datetime.fromisoformat(str(raw.get("observed_at", "")))
    if (
        any(t.tzinfo is None for t in (started, observed, now))
        or not started <= observed <= now
        or (now - started).total_seconds() > MAX_AGE_SECONDS
    ):
        raise ValueError("PROD OBSERVER: stale/future GetWithdrawLimits evidence")
    cash, blocked, guarantee = (
        number(raw.get(k)) for k in ("money_rub", "blocked_rub", "guarantee_rub")
    )
    if min(blocked, guarantee) < 0:
        raise ValueError("PROD OBSERVER: negative broker cash restriction")
    # GetWithdrawLimits.blocked can ALREADY contain blockedGuarantee. They are
    # overlapping restrictions, not independent debits. GetMaxLots.buyLimits
    # remains the mandatory per-order authority if limits differ between APIs.
    expected = max(Decimal(0), cash - max(blocked, guarantee))
    if number(raw.get("withdraw_conservative_rub")) != expected:
        raise ValueError("PROD OBSERVER: inconsistent funding calculation")
    return dict(raw)


@dataclass
class ObserverFundsReader:
    token: str = field(repr=False)
    account_id: str = field(repr=False)
    transport: JsonPostTransport = field(default_factory=UrlLibJsonTransport)
    clock: Callable[[], datetime] = _now

    @classmethod
    def from_environment(cls) -> ObserverFundsReader:
        return cls(
            os.environ.get("T_INVEST_PROD_TOKEN", ""),
            os.environ.get("T_INVEST_PROD_ACCOUNT_ID", ""),
        )

    def read(self, path: str, extra: Mapping[str, object] | None = None) -> Mapping[str, Any]:
        if path not in {WITHDRAW, MAX_LOTS, ORDER_PRICE} or not self.token or not self.account_id:
            raise ValueError("PROD OBSERVER funding: only bound read methods are permitted")
        if extra and "accountId" in extra:
            raise ValueError("PROD OBSERVER funding: account override forbidden")
        try:
            result = self.transport.post(
                PROD_REST + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                payload={"accountId": self.account_id, **(extra or {})},
                timeout_seconds=10,
            )
        except (OSError, ValueError) as exc:
            # Do not leak the HTTP body, URL credentials or account ID in diagnostics.
            raise ValueError(
                f"T-Invest/PROD {path.rsplit('/', 1)[-1]} unavailable ({type(exc).__name__})"
            ) from None
        if not isinstance(result, Mapping):
            raise ValueError(f"T-Invest/PROD {path.rsplit('/', 1)[-1]} invalid response")
        return result

    def snapshot(self) -> dict[str, object]:
        start = self.clock()
        raw = self.read(WITHDRAW)
        money = _rub(raw.get("money"))
        # Empty repeated protobuf fields may be omitted. Invalid/non-list is NOT zero.
        blocked, guarantee = _rub(raw.get("blocked", [])), _rub(raw.get("blockedGuarantee", []))
        result: dict[str, object] = {
            "protocol": "prod-observer-funding-v2",
            "account_binding": binding(self.account_id),
            "started_at": start.isoformat(),
            "observed_at": self.clock().isoformat(),
            "money_rub": str(money),
            "blocked_rub": str(blocked),
            "guarantee_rub": str(guarantee),
            "withdraw_conservative_rub": str(max(Decimal(0), money - max(blocked, guarantee))),
        }
        return verified_funding(result, self.account_id, self.clock())


@dataclass
class ObserverFundingGate:
    reader: ObserverFundsReader
    evidence: dict[str, object]
    budget: Decimal
    reserve: Decimal
    spent: Decimal = Decimal(0)
    checks: list[dict[str, object]] = field(default_factory=list)

    def check(self, intent: OrderIntent) -> tuple[str, ...]:
        if intent.side is not Side.BUY:
            return ()  # Existing sell/protected-position policy is unchanged.
        record: dict[str, object] = {
            "secid": intent.instrument.secid,
            "lots": intent.lots,
            "limit_price": str(intent.limit_price),
            "started_at": self.reader.clock().isoformat(),
        }
        try:
            verified_funding(self.evidence, self.reader.account_id, self.reader.clock())
            raw = self.reader.read(
                MAX_LOTS,
                {
                    "instrumentId": intent.instrument.uid,
                    "price": decimal_to_quotation(intent.limit_price),
                },
            )
            if str(raw.get("currency", "")).lower() != "rub":
                raise ValueError("GetMaxLots currency must be RUB")
            own = raw.get("buyLimits")
            if not isinstance(own, Mapping):
                raise ValueError("GetMaxLots buyLimits missing; margin limits are not usable")
            lots = number(own.get("buyMaxLots"))
            cash = _quotation(own.get("buyMoneyAmount"))
            if lots < 0 or lots != int(lots) or cash < 0:
                raise ValueError("GetMaxLots invalid own-money limits")
            record.update({"buy_max_lots": int(lots), "buy_money_rub": str(cash)})
            reasons: list[str] = []
            if intent.lots > lots:
                reasons.append("GetMaxLots: превышен доступный объём на собственные деньги")
            estimate = self.reader.read(
                ORDER_PRICE,
                {
                    "instrumentId": intent.instrument.uid,
                    "quantity": str(intent.lots),
                    "direction": "ORDER_DIRECTION_BUY",
                    "price": decimal_to_quotation(intent.limit_price),
                },
            )
            total = _money(estimate.get("totalOrderAmount"))
            if (
                number(estimate.get("lotsRequested")) != intent.lots
                or _money(estimate.get("initialOrderAmount")) != intent.notional
                or total < intent.notional
            ):
                raise ValueError("GetOrderPrice quantity/amount mismatch")
            verified_funding(self.evidence, self.reader.account_id, self.reader.clock())
            # Earlier shadow BUYs are not present at the broker. Reserve them here;
            # never fund another BUY from proceeds of an unexecuted shadow SELL.
            available = max(Decimal(0), min(self.budget, cash) - self.reserve - self.spent)
            if total > available:
                reasons.append("недостаточно собственных денег с учётом ГО, резерва и комиссии")
            record.update(
                {
                    "estimated_total_rub": str(total),
                    "available_for_order_rub": str(available),
                    "status": "REJECTED" if reasons else "PASSED",
                    "reasons": reasons,
                }
            )
            if not reasons:
                self.spent += total
        except ValueError as exc:
            reasons = [f"PROD OBSERVER: проверка покупательной способности недоступна: {exc}"]
            record.update({"status": "UNAVAILABLE", "reasons": reasons})
        record["observed_at"] = self.reader.clock().isoformat()
        self.checks.append(record)
        return tuple(reasons)

    def report(self) -> dict[str, object]:
        return {
            **self.evidence,
            "budget_rub": str(self.budget),
            "reserve_rub": str(self.reserve),
            "spendable_rub": str(max(Decimal(0), self.budget - self.reserve)),
            "planned_total_with_fees_rub": str(self.spent),
            "checks": self.checks,
            "execution_allowed": False,
        }
