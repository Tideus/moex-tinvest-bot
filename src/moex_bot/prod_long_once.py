"""Single-use PROD LONG orchestration; never arms itself or invents review evidence.

The operator boundary owns production_session(), fresh strategy provenance and a
root-controlled grant. This module is also testable with fake brokers, without IO
to a trading endpoint. A spent claim is never reset, even on a pre-submit failure.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from .live_execution import LivePermission, LiveState, _authorize, intent_digest, policy_digest
from .live_execution import registered_live_journal as journal_identity
from .live_protection import ProtectionBroker, ProtectionJournal, ProtectionPlan
from .long_auto_signal import Candidate
from .prod_long import ProdLongPolicy
from .supervised_execution import (
    SupervisedBroker,
    SupervisedTInvestBroker,
    read_parent,
    run_supervised_entry,
)

CAP = Decimal("10000")


def canonical(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def paired(candidate: Candidate) -> tuple[OrderIntent, ProtectionPlan]:
    i, price = candidate.instrument, candidate.limit_price
    UUID(candidate.order_request_id)
    if (
        type(candidate.lots) is not int
        or candidate.lots != 1
        or i.board != "TQBR"
        or i.currency != "RUB"
        or i.asset_class != "share"
        or type(i.lot_size) is not int
        or i.lot_size <= 0
        or not price.is_finite()
        or price <= 0
        or not i.tick_size.is_finite()
        or i.tick_size <= 0
        or price % i.tick_size
        or price * i.lot_size > CAP
        or len(candidate.decision_sha256) != 64
        or any(c not in "0123456789abcdef" for c in candidate.decision_sha256)
    ):
        raise ValueError("invalid single-use LONG candidate")
    stop = (price * Decimal(".98") / i.tick_size).to_integral_value(
        rounding=ROUND_FLOOR
    ) * i.tick_size
    if candidate.stop_trigger != stop:
        raise ValueError("LONG stop must equal the tick-aligned two-percent strategy stop")
    intent = OrderIntent(
        candidate.order_request_id,
        i,
        Side.BUY,
        1,
        price,
        price * i.lot_size,
        "one-shot LONG decision " + candidate.decision_sha256,
    )
    protection = ProtectionPlan(
        candidate.account_id,
        intent.order_request_id,
        str(uuid5(NAMESPACE_URL, "prod:" + candidate.account_id + ":" + intent.order_request_id)),
        i,
        1,
        price,
        stop,
    )
    protection.validate()
    return intent, protection


@dataclass(frozen=True)
class Claim:
    """Independent append-only attempt and dispatch fences; caller excludes concurrency."""

    directory: Path
    binding: dict[str, Any]

    def _path(self, name: str) -> Path:
        if name not in {"claim", "buy-dispatch", "stop-dispatch", "cancel-dispatch", "result"}:
            raise ValueError("unknown claim record")
        if not self.directory.is_absolute():
            raise ValueError("absolute independent claim directory required")
        for p in (self.directory, *self.directory.parents):
            if not stat.S_ISDIR(p.lstat().st_mode):
                raise ValueError("claim ancestors must be existing real directories")
        return self.directory / (name + ".json")

    def exists(self, name: str = "claim") -> bool:
        p = self._path(name)
        return p.exists() or p.is_symlink()

    def check(self) -> None:
        p = self._path("claim")
        meta = p.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise ValueError("claim is not a single regular file")
        if p.read_text(encoding="utf-8") != canonical(self.binding):
            raise ValueError("independent claim no longer matches account/release/order/stop")

    def create(self, name: str, value: object) -> None:
        p = self._path(name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        if os.name != "nt":
            fd = os.open(p.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def dispatch(self, name: str) -> None:
        self.check()
        # Exclusive creation BEFORE transport. A lost response never authorizes a retry.
        self.create(name, self.binding)


def check_entry_state(intent: OrderIntent, state: LiveState, protection: ProtectionPlan) -> None:
    if (
        state.account_id != protection.account_id
        or state.account_type != "ACCOUNT_TYPE_TINKOFF_IIS"
        or state.instrument != intent.instrument
        or not state.estimated_total.is_finite()
        or not intent.notional <= state.estimated_total <= CAP
        or not state.cash_available.is_finite()
        or state.cash_available < state.estimated_total
        or any(
            h.instrument.uid == intent.instrument.uid and h.units != 0
            for h in state.positions.values()
        )
        or not state.bid.is_finite()
        or state.bid <= protection.stop_trigger
    ):
        raise ValueError("one-shot IIS cash/fee/manual-position/stop gate failed")


@dataclass
class OnceBroker:
    delegate: SupervisedBroker
    intent: OrderIntent
    protection: ProtectionPlan
    claim: Claim
    entry_gate: Callable[[], None]
    protection_gate: Callable[[], None]
    account_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.account_id = self.delegate.account_id

    @property
    def environment(self) -> str:
        return self.delegate.environment

    def capture(self, intent: OrderIntent) -> LiveState:
        if intent != self.intent:
            raise ValueError("another intent cannot use this single-use capability")
        self.entry_gate()
        state: LiveState = self.delegate.capture(intent)
        check_entry_state(intent, state, self.protection)
        return state

    def submit(self, intent: OrderIntent) -> OrderRecord:
        expected = asdict(self.intent) | {"order_request_id": self.protection.entry_request_id}
        if canonical(asdict(intent)) != canonical(expected):
            raise ValueError("changed one-shot BUY")
        self.entry_gate()
        self.claim.dispatch("buy-dispatch")
        result = self.delegate.submit(intent)
        # Independent correlated readback, even if POST says FILLED.
        return self.delegate.lookup(result)

    def lookup(self, record: OrderRecord) -> OrderRecord:
        if record.intent.order_request_id != self.protection.entry_request_id:
            raise ValueError("foreign parent lookup")
        return self.delegate.lookup(record)

    def cancel_pending(self, record: OrderRecord) -> None:
        if record.intent.order_request_id != self.protection.entry_request_id:
            raise ValueError("foreign cancellation forbidden")
        self.protection_gate()
        self.claim.dispatch("cancel-dispatch")
        if isinstance(self.delegate, SupervisedTInvestBroker):
            self.delegate.cancel_pending(record, before_cancel=self.protection_gate)
        else:
            self.delegate.cancel_pending(record)


@dataclass
class OnceProtection:
    delegate: ProtectionBroker
    plan: ProtectionPlan
    claim: Claim
    gate: Callable[[], None]

    @property
    def account_id(self) -> str:
        return self.delegate.account_id

    @property
    def environment(self) -> str:
        return self.delegate.environment

    def available_units(self, instrument: Instrument) -> int:
        if instrument != self.plan.instrument:
            raise ValueError("foreign protection instrument")
        return self.delegate.available_units(instrument)

    def list_stops(self) -> tuple[Mapping[str, Any], ...]:
        return self.delegate.list_stops()

    def post_stop(self, plan: ProtectionPlan, request_id: str) -> Mapping[str, Any]:
        if plan != self.plan or request_id != plan.request_id:
            raise ValueError("changed child stop")
        self.gate()
        self.claim.dispatch("stop-dispatch")
        return self.delegate.post_stop(plan, request_id)


def claim_binding(
    candidate: Candidate,
    plan: ProtectionPlan,
    journal_uuid: str,
    release: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "prod-long-once",
        "candidate": asdict(candidate),
        "protection_sha256": plan.digest,
        "journal_uuid": journal_uuid,
        "release_sha256": release,
    }


def run_once(
    *,
    candidate: Candidate,
    policy: ProdLongPolicy,
    release: str,
    journal: Path,
    protection_journal: Path,
    claim_directory: Path,
    broker: SupervisedBroker,
    stops: ProtectionBroker,
    authority: Callable[[], None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, str | int]:
    """Caller must hold the production session lock throughout this entire call."""
    authority()  # No entry, state creation, or broker traffic when not armed by operator.
    if (
        broker.environment != "prod"
        or stops.environment != "prod"
        or broker.account_id != candidate.account_id
        or stops.account_id != candidate.account_id
        or policy.execution_stage != "canary"
        or claim_directory.is_relative_to(journal.parent)
        or journal == protection_journal
        or not journal.is_absolute()
        or journal.resolve() != journal
        or protection_journal.resolve() != protection_journal
    ):
        raise ValueError("separate production account and independent single-use state required")
    intent, plan = paired(candidate)
    risk = (plan.entry_price - plan.stop_trigger) * plan.instrument.lot_size
    risk += intent.notional * Decimal(".005")
    if risk > policy.canary.capital_limit_rub * policy.canary.max_account_daily_loss_weight:
        raise ValueError("planned stop distance and transaction costs exceed risk budget")
    identity = journal_identity(journal, candidate.account_id)
    claim = Claim(claim_directory, claim_binding(candidate, plan, identity, release))
    # Do not read the broker or adopt a new signal after ANY previous attempt.
    if claim.exists():
        return {"state": "ALREADY_CLAIMED", "protection": "NOT_RECHECKED"}
    for name in ("buy-dispatch", "stop-dispatch", "cancel-dispatch", "result"):
        if claim.exists(name):
            raise ValueError("missing claim with surviving fence; restore evidence, never re-arm")
    with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] != 0:
            raise ValueError("first-entry journal already contains attempts; never re-arm")
    now = clock()
    if (
        now.tzinfo is None
        or candidate.valid_until.tzinfo is None
        or not timedelta(seconds=180) <= candidate.valid_until - now <= timedelta(minutes=5)
    ):
        raise ValueError("fresh decision with time reserved for protection required")
    # A definite read-only preflight refusal is not a dispatched attempt. The
    # journal reservation repeats all checks against a NEW snapshot after claim.
    initial = broker.capture(intent)
    check_entry_state(intent, initial, plan)
    _authorize(intent, policy, initial, {}, 0, Decimal(0), clock())
    authority()
    if candidate.valid_until - clock() < timedelta(seconds=180):
        raise ValueError("signal too old after read-only preflight")
    permission = LivePermission(
        candidate.account_id,
        intent_digest(intent),
        policy_digest(policy),
        release,
        now,
        now + timedelta(minutes=5),
        True,
        identity,
    )

    def protect_gate() -> None:
        authority()
        claim.check()
        if journal_identity(journal, candidate.account_id) != identity:
            raise ValueError("production journal binding changed")

    def entry_gate() -> None:
        protect_gate()
        if clock() >= candidate.valid_until:
            raise ValueError("strategy signal expired before dispatch")

    claim.create("claim", claim.binding)
    wrapped = OnceBroker(broker, intent, plan, claim, entry_gate, protect_gate)
    protected = OnceProtection(stops, plan, claim, protect_gate)
    try:
        result = run_supervised_entry(
            intent=intent,
            policy=policy,
            permission=permission,
            release=release,
            journal_path=journal,
            protection_path=protection_journal,
            protection=plan,
            confirmed_protection_sha256=plan.digest,
            broker=wrapped,
            stop_broker=protected,
            approval_gate=protect_gate,
            entry_approval_gate=entry_gate,
            clock=clock,
        )
        claim.create("result", result)
        return result
    except Exception:
        # No automatic purchase, cancellation or protection retry after ambiguity.
        if not claim.exists("result"):
            claim.create("result", {"state": "QUARANTINED", "protection": "REQUIRES_REVIEW"})
        raise


def recover_stop_only(
    *,
    candidate: Candidate,
    release: str,
    journal: Path,
    protection_journal: Path,
    claim_directory: Path,
    broker: SupervisedBroker,
    stops: ProtectionBroker,
    authority: Callable[[], None],
) -> str:
    """Explicit operator recovery; NEVER invoked automatically by run_once or a timer.

    Only PLANNED child with no external dispatch fence and a freshly correlated
    FILLED parent may proceed. Restoring a DB before an already-sent stop cannot
    authorize another stop. A missing response requires manual broker investigation.
    """
    authority()
    if (
        broker.environment != "prod"
        or stops.environment != "prod"
        or broker.account_id != candidate.account_id
        or stops.account_id != candidate.account_id
    ):
        raise ValueError("exact production recovery account required")
    _, plan = paired(candidate)
    identity = journal_identity(journal, candidate.account_id)
    claim = Claim(claim_directory, claim_binding(candidate, plan, identity, release))
    claim.check()
    if not claim.exists("buy-dispatch") or claim.exists("stop-dispatch"):
        raise ValueError("stop dispatch absent proof missing; never repeat entry or stop")
    # Avoid ProtectionJournal's constructor creating a missing journal on recovery.
    if not protection_journal.is_file() or protection_journal.is_symlink():
        raise ValueError("existing protection journal required")
    protection = ProtectionJournal(protection_journal)
    if protection._row(plan)[0] != "PLANNED":
        raise ValueError("only unequivocal pre-dispatch protection recovery allowed")
    parent = read_parent(journal, plan)
    actual = broker.lookup(parent)
    if parent != actual or actual.status is not OrderStatus.FILLED or actual.filled_lots != 1:
        raise ValueError("exact durable and current FILLED parent required")

    def gate() -> None:
        authority()
        claim.check()
        if journal_identity(journal, candidate.account_id) != identity:
            raise ValueError("recovery journal binding changed")

    return protection.activate(plan, journal, OnceProtection(stops, plan, claim, gate))
