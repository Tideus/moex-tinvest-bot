"""One supervised BUY, bounded polling/cancel, then exact pre-approved native protection.

Not a strategy runner. The caller must validate a fresh root-controlled experiment
review AND the digest of the complete paired preview before invoking this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from .domain import OrderIntent, OrderRecord, OrderStatus, Side
from .execution_journal import ExecutionJournal
from .integrations.tinvest_live import TInvestLiveBroker
from .live_execution import LivePermission, execute_live_intent, registered_live_journal
from .live_protection import ProtectionBroker, ProtectionJournal, ProtectionPlan
from .prod_long import ProdLongPolicy
from .sandbox_execution import decode_validated_intent


class SupervisedBroker(Protocol):
    account_id: str

    @property
    def environment(self) -> str: ...
    def capture(self, intent: OrderIntent) -> Any: ...
    def submit(self, intent: OrderIntent) -> OrderRecord: ...
    def lookup(self, record: OrderRecord) -> OrderRecord: ...
    def cancel_pending(self, record: OrderRecord) -> None: ...


class SupervisedTInvestBroker(TInvestLiveBroker):
    def cancel_pending(self, record: OrderRecord) -> None:
        if (
            not record.broker_order_id
            or record.broker_order_id == record.intent.order_request_id
            or record.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
        ):
            raise ValueError("no verified pending exchange order to cancel; inspect broker state")
        response = self._post(
            "OrdersService/CancelOrder",
            {
                "accountId": self.account_id,
                "orderId": record.broker_order_id,
            },
        )
        moment = datetime.fromisoformat(str(response["time"]).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("cancel timestamp must be timezone-aware; cancellation unverified")


@dataclass
class GuardedProtectionBroker:
    delegate: ProtectionBroker
    before_post: Callable[[], None]

    @property
    def account_id(self) -> str:
        return self.delegate.account_id

    @property
    def environment(self) -> str:
        return self.delegate.environment

    def available_units(self, instrument: Any) -> int:
        return self.delegate.available_units(instrument)

    def list_stops(self) -> tuple[Mapping[str, Any], ...]:
        return self.delegate.list_stops()

    def post_stop(self, plan: ProtectionPlan, request_id: str) -> Mapping[str, Any]:
        self.before_post()  # Revocation/expiry is rechecked at the actual stop POST boundary.
        return self.delegate.post_stop(plan, request_id)


def read_parent(path: Path, plan: ProtectionPlan) -> OrderRecord:
    registered_live_journal(path, plan.account_id)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT record FROM actions WHERE account=? AND request_id=?",
            ("prod:" + plan.account_id, plan.entry_request_id),
        ).fetchone()
    if row is None:
        raise ValueError("durable parent order is missing")
    raw = json.loads(row[0])
    intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
    return OrderRecord(
        intent,
        OrderStatus(raw["status"]),
        int(raw["filled_lots"]),
        raw["broker_order_id"],
        raw.get("status_detail"),
    )


def run_supervised_entry(
    *,
    intent: OrderIntent,
    policy: ProdLongPolicy,
    permission: LivePermission,
    release: str,
    journal_path: Path,
    protection_path: Path,
    protection: ProtectionPlan,
    confirmed_protection_sha256: str,
    broker: SupervisedBroker,
    stop_broker: ProtectionBroker,
    approval_gate: Callable[[], None],
    entry_approval_gate: Callable[[], None] | None = None,
    wait_seconds: int = 30,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, str | int]:
    protection.validate()
    if (
        intent.side is not Side.BUY
        or type(intent.lots) is not int
        or intent.lots != 1
        or protection.account_id != broker.account_id
        or protection.entry_logical_id != intent.order_request_id
        or protection.entry_request_id
        != str(uuid5(NAMESPACE_URL, "prod:" + broker.account_id + ":" + intent.order_request_id))
        or stop_broker.account_id != broker.account_id
        or stop_broker.environment != "prod"
        or protection.instrument != intent.instrument
        or protection.entry_price != intent.limit_price
        or protection.digest != confirmed_protection_sha256
        or type(wait_seconds) is not int
        or not 1 <= wait_seconds <= 30
    ):
        raise ValueError("exact one-lot paired entry/protection confirmation required")
    permission.validate(broker.account_id, intent, policy, release, clock())
    if permission.expires_at - clock() < timedelta(seconds=180):
        raise ValueError("at least 180 seconds of approval must remain before the first BUY")
    approval_gate()
    stops = ProtectionJournal(protection_path)
    stops.prepare(protection, journal_path)  # Durable plan precedes ANY entry submission.

    def gate() -> None:
        permission.validate(broker.account_id, intent, policy, release, clock())
        if permission.expires_at - clock() < timedelta(seconds=180):
            raise ValueError("insufficient time reserved for the child protection")
        if entry_approval_gate is not None:
            entry_approval_gate()
        if stops._row(protection)[0] != "PLANNED":
            raise ValueError("entry protection is not in its original planned state")
        approval_gate()

    execute_live_intent(
        intent=intent,
        policy=policy,
        broker=broker,
        permission=permission,
        release_sha256=release,
        journal_path=journal_path,
        clock=clock,
        before_submit=gate,
    )
    parent = read_parent(journal_path, protection)
    journal = ExecutionJournal(journal_path, clock=clock)
    deadline = monotonic() + wait_seconds
    terminal = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    while parent.status not in terminal and monotonic() < deadline:
        parent = broker.lookup(parent)  # Read-only, never retries PostOrder.
        journal.record("prod:" + broker.account_id, parent)
        if parent.status not in terminal:
            sleeper(1)
    if parent.status not in terminal:
        # The complete preview must explicitly approve this cancellation of the new entry only.
        permission.validate(broker.account_id, intent, policy, release, clock())
        approval_gate()
        broker.cancel_pending(parent)
        parent = broker.lookup(parent)
        journal.record("prod:" + broker.account_id, parent)
    if parent.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED} and parent.filled_lots == 0:
        return {
            "state": "ENTRY_NOT_FILLED",
            "filled_lots": 0,
            "protection": "PLANNED_REVIEW_REQUIRED",
        }
    if parent.status is not OrderStatus.FILLED or parent.filled_lots != 1:
        raise ValueError("CRITICAL: entry ambiguous/partial; operator intervention required")

    def stop_gate() -> None:
        permission.validate(broker.account_id, intent, policy, release, clock())
        approval_gate()

    status = stops.activate(
        protection,
        journal_path,
        GuardedProtectionBroker(stop_broker, stop_gate),
    )
    if status != "ACTIVE":
        raise ValueError("CRITICAL: filled entry lacks verified active protection")
    return {"state": "FILLED_PROTECTED", "filled_lots": 1, "protection": status}
