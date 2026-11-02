"""Fresh, separately confirmed protection-only recovery; never creates an entry."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .domain import Instrument, OrderRecord, OrderStatus, Side
from .execution_journal import ExecutionJournal
from .integrations.tinvest_live import TInvestLiveBroker
from .integrations.tinvest_sandbox import quotation_to_decimal
from .live_execution import registered_live_journal
from .live_protection import ProtectionBroker, ProtectionJournal, ProtectionPlan
from .supervised_execution import GuardedProtectionBroker, read_parent


class RecoveryParentBroker(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def environment(self) -> str: ...
    def read_filled_parent(self, record: OrderRecord) -> OrderRecord: ...
    def validate_market(self, plan: ProtectionPlan, now: datetime) -> None: ...


@dataclass
class NativeRecoveryParentBroker:
    delegate: TInvestLiveBroker

    @property
    def account_id(self) -> str:
        return self.delegate.account_id

    @property
    def environment(self) -> str:
        return self.delegate.environment

    def validate_market(self, plan: ProtectionPlan, now: datetime) -> None:
        raw = self.delegate._post("MarketDataService/GetOrderBook", {
            "instrumentId": plan.instrument.uid, "depth": 1})
        at = datetime.fromisoformat(str(raw.get("orderbookTs", "")).replace("Z", "+00:00"))
        now = self.delegate.clock()  # Evaluate freshness after the read, not before network IO.
        bids = raw.get("bids")
        if (raw.get("instrumentUid") != plan.instrument.uid or at.tzinfo is None
                or not timedelta(0) <= now - at <= timedelta(seconds=10)
                or not isinstance(bids, list) or not bids or not isinstance(bids[0], dict)):
            raise ValueError("fresh exact recovery orderbook required")
        bid = quotation_to_decimal(bids[0].get("price", {}))
        if not bid.is_finite() or bid <= plan.stop_trigger or int(bids[0].get("quantity", 0)) <= 0:
            raise ValueError("stop already crossed or market unavailable; operator exit required")

    def read_filled_parent(self, record: OrderRecord) -> OrderRecord:
        positions = self.delegate._post("OperationsService/GetPositions", {
            "accountId": self.account_id})
        orders = self.delegate._post("OrdersService/GetOrders", {"accountId": self.account_id})
        securities = positions.get("securities", [])
        order_rows = orders.get("orders", [])
        if (not isinstance(securities, list) or any(not isinstance(s, dict) for s in securities)
                or not isinstance(order_rows, list)
                or any(not isinstance(o, dict) for o in order_rows)):
            raise ValueError("invalid holdings")
        holdings = [s for s in securities if s.get("instrumentUid") == record.intent.instrument.uid]
        if (positions.get("limitsLoadingInProgress") is True or order_rows
                or len(holdings) != 1
                or holdings[0].get("exchangeBlocked") is True
                or str(holdings[0].get("balance")) != str(record.intent.instrument.lot_size)
                or str(holdings[0].get("blocked")) != "0"):
            raise ValueError("exact free holding and no active child orders required")
        history = self.delegate._post("StopOrdersService/GetStopOrders", {
            "accountId": self.account_id, "status": "STOP_ORDER_STATUS_ALL",
        }).get("stopOrders", [])
        if not isinstance(history, list) or any(
            not isinstance(s, dict) or not s.get("instrumentUid")
            or s["instrumentUid"] == record.intent.instrument.uid for s in history
        ):
            raise ValueError("stop history/triggered child requires manual reconciliation")
        raw = self.delegate._post("OrdersService/GetOrderState", {
            "accountId": self.account_id, "orderId": record.intent.order_request_id,
            "orderIdType": "ORDER_ID_TYPE_REQUEST", "priceType": "PRICE_TYPE_CURRENCY",
        })
        expected = {
            "instrumentUid": record.intent.instrument.uid,
            "orderRequestId": record.intent.order_request_id,
            "direction": "ORDER_DIRECTION_BUY",
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
        }
        if (any(raw.get(k) != v for k, v in expected.items())
                or str(raw.get("lotsRequested")) != "1"
                or str(raw.get("lotsExecuted")) != "1"
                or not isinstance(raw.get("orderId"), str) or not raw["orderId"]
                or raw["orderId"] == record.intent.order_request_id):
            raise ValueError("exact broker FILLED BUY parent identity required")
        if (record.broker_order_id and record.broker_order_id != record.intent.order_request_id
                and record.broker_order_id != raw["orderId"]):
            raise ValueError("exchange parent id changed")
        return replace(record, status=OrderStatus.FILLED, filled_lots=1,
                       broker_order_id=raw["orderId"])


def recovery_digest(preview: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(preview, sort_keys=True, default=str).encode()).hexdigest()


def load_recovery_plan(path: Path, account: str, entry_path: Path) -> ProtectionPlan:
    identity = registered_live_journal(entry_path, account)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT plan,plan_hash,live_uuid,state FROM protection "
                          "WHERE account=?", (account,)).fetchall()
    if len(rows) != 1 or rows[0][2] != identity or rows[0][3] != "PLANNED":
        raise ValueError("one original PLANNED enrollment-bound pair required")
    raw = json.loads(rows[0][0])
    raw["instrument"]["tick_size"] = Decimal(raw["instrument"]["tick_size"])
    raw["instrument"] = Instrument(**raw["instrument"])
    for key in ("entry_price", "stop_trigger", "stop_limit", "take_profit"):
        if raw.get(key) is not None:
            raw[key] = Decimal(raw[key])
    plan = ProtectionPlan(**raw)
    plan.validate()
    if plan.account_id != account or plan.digest != rows[0][1]:
        raise ValueError("immutable recovery pair changed")
    return plan


def _check(plan: ProtectionPlan, entry_path: Path, protection_path: Path,
           parent_broker: RecoveryParentBroker, stop_broker: ProtectionBroker,
           now: datetime) -> tuple[str, OrderRecord]:
    plan.validate()
    if now.tzinfo is None:
        raise ValueError("aware recovery time required")
    for broker in (parent_broker, stop_broker):
        if broker.account_id != plan.account_id or broker.environment != "prod":
            raise ValueError("exact production recovery account required")
    identity = registered_live_journal(entry_path, plan.account_id)
    stops = ProtectionJournal(protection_path)
    state, stop_id, saved_identity = stops._row(plan)
    if saved_identity != identity or state != "PLANNED" or stop_id is not None:
        raise ValueError("only original PLANNED protection may recover; UNKNOWN never resubmits")
    parent = read_parent(entry_path, plan)
    intent = parent.intent
    if (intent.order_request_id != plan.entry_request_id or intent.side is not Side.BUY
            or intent.lots != 1 or intent.instrument != plan.instrument
            or intent.limit_price != plan.entry_price):
        raise ValueError("durable parent differs from exact protection pair")
    actual = parent_broker.read_filled_parent(parent)
    if (actual.intent != intent or actual.status is not OrderStatus.FILLED
            or actual.filled_lots != 1 or not actual.broker_order_id):
        raise ValueError("parent is not a verified exact full fill")
    if parent.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
        raise ValueError(
            "terminal local parent contradicts broker fill; manual investigation required")
    parent_broker.validate_market(plan, now)
    if stop_broker.available_units(plan.instrument) != plan.instrument.lot_size:
        raise ValueError("exact one-lot free holding required for recovery")
    if any(s.get("instrumentUid") == plan.instrument.uid for s in stop_broker.list_stops()):
        raise ValueError("foreign or ambiguous stop blocks recovery")
    return identity, actual


def preview_recovery(*, plan: ProtectionPlan, entry_path: Path, protection_path: Path,
                     parent_broker: RecoveryParentBroker, stop_broker: ProtectionBroker,
                     release: str, now: datetime) -> dict[str, Any]:
    identity, actual = _check(plan, entry_path, protection_path, parent_broker, stop_broker, now)
    return {
        "format": "protection-recovery-v1", "action": "PROTECTION_ONLY",
        "account_id": plan.account_id, "plan_sha256": plan.digest,
        "entry_request_id": plan.entry_request_id, "journal_uuid": identity,
        "release_sha256": release, "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=2)).isoformat(),
        "protection": json.loads(json.dumps(asdict(plan), default=str)),
        "parent_exchange_id": actual.broker_order_id, "parent_filled_lots": 1,
        "direction": "SELL", "expiration": "GOOD_TILL_CANCEL",
    }


def apply_recovery(*, preview: dict[str, Any], confirmed_sha256: str,
                   plan: ProtectionPlan, entry_path: Path, protection_path: Path,
                   parent_broker: RecoveryParentBroker, stop_broker: ProtectionBroker,
                   release: str, approval_gate: Callable[[], None],
                   clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> str:
    def gate() -> None:
        now = clock()
        created = datetime.fromisoformat(str(preview["created_at"]))
        expires = datetime.fromisoformat(str(preview["expires_at"]))
        expected = {
            "format": "protection-recovery-v1", "action": "PROTECTION_ONLY",
            "account_id": plan.account_id, "plan_sha256": plan.digest,
            "entry_request_id": plan.entry_request_id,
            "journal_uuid": registered_live_journal(entry_path, plan.account_id),
            "release_sha256": release, "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
            "protection": json.loads(json.dumps(asdict(plan), default=str)),
            "parent_exchange_id": preview.get("parent_exchange_id"), "parent_filled_lots": 1,
            "direction": "SELL", "expiration": "GOOD_TILL_CANCEL",
        }
        if (preview != expected or recovery_digest(preview) != confirmed_sha256
                or any(x.tzinfo is None for x in (now, created, expires))
                or not created <= now < expires or expires - created > timedelta(minutes=2)):
            raise ValueError("exact fresh protection-only confirmation required")
        approval_gate()

    gate()
    _, actual = _check(plan, entry_path, protection_path, parent_broker, stop_broker, clock())
    if actual.broker_order_id != preview.get("parent_exchange_id"):
        raise ValueError("preview parent exchange id changed")
    gate()
    ExecutionJournal(entry_path, clock=clock).record("prod:" + plan.account_id, actual)
    def final_guard() -> None:
        parent_broker.validate_market(plan, clock())
        gate()  # Market read must not consume/revoke the fresh approval silently.
    return ProtectionJournal(protection_path).activate(
        plan, entry_path, GuardedProtectionBroker(stop_broker, final_guard))
