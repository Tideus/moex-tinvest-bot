"""Supervised synthetic take-profit: never keeps two live sell orders on one lot.

Cancellation and sale are NOT atomic. Unknown/partial results require operator
review, not automatic retry or re-arming. No scheduler is installed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .domain import Instrument, OrderIntent, OrderStatus, Side
from .live_execution import (
    LivePermission,
    _authorize,
    _ledger_summary,
    execute_live_intent,
    intent_digest,
    policy_digest,
    registered_live_journal,
)
from .live_protection import (
    NativeProtectionBroker,
    ProtectionJournal,
    ProtectionPlan,
    _verified_stop,
)
from .prod_long import ProdLongPolicy
from .supervised_execution import SupervisedTInvestBroker, read_parent


class NativeExitBroker(NativeProtectionBroker):
    def history(self) -> tuple[Mapping[str, Any], ...]:
        result = self._post(
            "StopOrdersService/GetStopOrders",
            {"accountId": self.account_id, "status": "STOP_ORDER_STATUS_ALL"},
        )
        rows = result.get("stopOrders", [])
        if not isinstance(rows, list) or any(not isinstance(s, Mapping) for s in rows):
            raise ValueError("invalid stop history")
        return tuple(rows)

    def cancel(self, stop_id: str) -> None:
        response = self._post(
            "StopOrdersService/CancelStopOrder",
            {"accountId": self.account_id, "stopOrderId": stop_id},
        )
        moment = datetime.fromisoformat(str(response["time"]).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("stop cancellation timestamp is unverified")


def load_plan(path: Path, account: str, entry_path: Path) -> ProtectionPlan:
    """Only load an existing immutable, enrollment-bound protection plan."""
    identity = registered_live_journal(entry_path, account)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT plan,plan_hash,live_uuid FROM protection WHERE account=?", (account,)
        ).fetchall()
    if len(rows) != 1 or rows[0][2] != identity:
        raise ValueError("one enrollment-bound protection plan required")
    raw = json.loads(rows[0][0])
    raw["instrument"]["tick_size"] = Decimal(raw["instrument"]["tick_size"])
    raw["instrument"] = Instrument(**raw["instrument"])
    for key in ("entry_price", "stop_trigger", "stop_limit", "take_profit"):
        if raw.get(key) is not None:
            raw[key] = Decimal(raw[key])
    plan = ProtectionPlan(**raw)
    plan.validate()
    if plan.account_id != account or plan.digest != rows[0][1] or plan.take_profit is None:
        raise ValueError("immutable take-profit plan missing or changed")
    parent = read_parent(entry_path, plan)
    if (parent.status is not OrderStatus.FILLED or parent.filled_lots != 1
            or parent.intent.side is not Side.BUY or parent.intent.lots != 1
            or parent.intent.instrument != plan.instrument
            or parent.intent.limit_price != plan.entry_price):
        raise ValueError("confirmed FILLED parent required")
    return plan


def active_stop(plan: ProtectionPlan, journal: ProtectionJournal, broker: NativeExitBroker) -> str:
    status, stop_id, _ = journal._row(plan)
    if (status != "ACTIVE" or not stop_id or broker.account_id != plan.account_id
            or broker.environment != "prod"):
        raise ValueError("known ACTIVE protection required; inspect stop/child at broker")
    matches = [s for s in broker.history() if s.get("instrumentUid") == plan.instrument.uid]
    if len(matches) != 1 or matches[0].get("stopOrderId") != stop_id:
        raise ValueError("conflicting or missing broker protection")
    if not _verified_stop(plan, matches[0]):
        raise ValueError("stop triggered/expired/mismatched; never submit another sell")
    return stop_id


def exit_intent(plan: ProtectionPlan) -> OrderIntent:
    if plan.take_profit is None:
        raise ValueError("take-profit target required")
    return OrderIntent(
        str(uuid5(NAMESPACE_URL, "take-profit:" + plan.request_id)),
        plan.instrument, Side.SELL, 1, plan.take_profit,
        plan.take_profit * plan.instrument.lot_size, "supervised synthetic take-profit",
    )


def precheck(
    plan: ProtectionPlan, intent: OrderIntent, policy: ProdLongPolicy,
    broker: SupervisedTInvestBroker, entry_path: Path, clock: Callable[[], datetime],
) -> None:
    if (intent != exit_intent(plan) or broker.environment != "prod"
            or broker.account_id != plan.account_id):
        raise ValueError("exact immutable one-lot take-profit exit required")
    state = broker.capture(intent)
    now = clock()
    if state.account_id != plan.account_id or state.bid < intent.limit_price:
        raise ValueError("take-profit not reached on current executable bid")
    with closing(sqlite3.connect(entry_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        owned, count, turnover = _ledger_summary(db, "prod:" + plan.account_id, now)
    # The exact sole native stop was checked separately; no other restriction is relaxed.
    _authorize(intent, policy, replace(state, stop_uids=state.stop_uids - {plan.instrument.uid}),
               owned, count, turnover, now)


def run_exit(
    *, plan: ProtectionPlan, journal: ProtectionJournal, entry_path: Path,
    policy: ProdLongPolicy, broker: SupervisedTInvestBroker, stops: NativeExitBroker,
    permission: LivePermission, release: str, gate: Callable[[], None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, str]:
    intent = exit_intent(plan)
    permission.validate(plan.account_id, intent, policy, release, clock())
    if permission.journal_uuid != registered_live_journal(entry_path, plan.account_id):
        raise ValueError("exit approval belongs to another ledger")
    if permission.expires_at - clock() < timedelta(seconds=120):
        raise ValueError("120 seconds of approval must remain before cancelling protection")
    gate()
    stop_id = active_stop(plan, journal, stops)
    precheck(plan, intent, policy, broker, entry_path, clock)
    with closing(sqlite3.connect(journal.path)) as db, db:
        changed = db.execute(
            "UPDATE protection SET state='EXIT_CANCEL_PENDING' "
            "WHERE request_id=? AND state='ACTIVE' AND stop_id=?",
            (plan.request_id, stop_id),
        ).rowcount
        if changed != 1:
            raise ValueError("exit already reserved; no mutation retried")
    # Durable reservation precedes cancellation. Every error after here requires review.
    gate()
    stops.cancel(stop_id)
    matches = [s for s in stops.history() if s.get("stopOrderId") == stop_id]
    if len(matches) != 1:
        raise ValueError("stop cancellation history unavailable")
    row = matches[0]
    if (row.get("status") != "STOP_ORDER_STATUS_CANCELED"
            or row.get("exchangeOrderId") or row.get("activationDateTime")
            or not _verified_stop(plan, dict(row, status="STOP_ORDER_STATUS_ACTIVE"))):
        raise ValueError("cancel raced with activation; no extra sell permitted")
    journal._state(plan, "EXIT_STOP_CANCELLED", stop_id)
    result = execute_live_intent(
        intent=intent, policy=policy, broker=broker, permission=permission,
        release_sha256=release, journal_path=entry_path, clock=clock, before_submit=gate,
    )
    if result is None or result.status is not OrderStatus.FILLED or result.filled_lots != 1:
        raise ValueError("exit unfilled/ambiguous; protection is cancelled; operator required")
    journal._state(plan, "CLOSED_TAKE_PROFIT", stop_id)
    return {"state": "CLOSED_TAKE_PROFIT", "protection": "CANCELLED_VERIFIED"}


def preview_report(
    plan: ProtectionPlan, stop_id: str, release: str, policy: ProdLongPolicy,
    identity: str, now: datetime,
) -> dict[str, Any]:
    return {
        "format": "supervised-exit-v1", "account_id": plan.account_id,
        "plan_sha256": plan.digest, "stop_id": stop_id, "intent": asdict(exit_intent(plan)),
        "release_sha256": release, "policy_sha256": policy_digest(policy),
        "journal_uuid": identity, "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "protection_gap_acknowledged": True, "order_submitted": False,
    }


def write_preview(path: Path, report: dict[str, Any]) -> str:
    if path.suffix != ".json":
        raise ValueError("new private JSON exit preview required")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        if os.name != "nt":
            path.chmod(0o600)
        json.dump(report, stream, default=str, indent=2)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exit_permission(
    raw: dict[str, Any], plan: ProtectionPlan, stop_id: str, release: str,
    policy: ProdLongPolicy, identity: str, now: datetime,
) -> LivePermission:
    created = datetime.fromisoformat(str(raw["created_at"]))
    expected = preview_report(plan, stop_id, release, policy, identity, created)
    if json.loads(json.dumps(expected, default=str)) != raw:
        raise ValueError("exact exit/cancel preview identity changed")
    result = LivePermission(
        plan.account_id, intent_digest(exit_intent(plan)), policy_digest(policy), release,
        created, datetime.fromisoformat(raw["expires_at"]), True, identity,
    )
    result.validate(plan.account_id, exit_intent(plan), policy, release, now)
    return result
