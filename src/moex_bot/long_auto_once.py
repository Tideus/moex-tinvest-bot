"""Durable single-use sandbox entry/protection orchestration. No automatic retries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from .sandbox_execution import decode_validated_intent

if TYPE_CHECKING:
    from .long_auto_signal import Candidate


class SandboxPort(Protocol):
    @property
    def account_id(self) -> str: ...
    @property
    def environment(self) -> str: ...
    def set_mutation_guard(self, callback: Callable[[], None]) -> None: ...

    def preflight(self, candidate: Candidate) -> dict[str, Any]: ...
    def submit(self, candidate: Candidate) -> OrderRecord: ...
    def lookup(self, record: OrderRecord) -> OrderRecord: ...
    def cancel_unfilled_verified(self, record: OrderRecord) -> OrderRecord: ...
    def protect(self, candidate: Candidate, filled: OrderRecord) -> dict[str, Any]: ...
    def verify_protection(
        self, candidate: Candidate, response: dict[str, Any]
    ) -> dict[str, Any]: ...


def _anchor_path(path: Path, anchor: Path | None) -> Path:
    target = anchor or path.parent / "claim-anchors" / (path.name + ".once.json")
    if (
        not target.is_absolute()
        or target.parent == path.parent
        or target == path
        or target.is_symlink()
    ):
        raise ValueError("independent absolute non-symlink claim anchor required")
    return target


def init(path: Path, account_id: str, *, anchor: Path | None = None) -> None:
    """Explicit initialization; never used by execution, update or recovery."""
    if not account_id.strip() or not path.is_absolute():
        raise ValueError("explicit absolute one-shot ledger path and account required")
    target = _anchor_path(path, anchor)
    if target.exists():
        raise ValueError("claim anchor already exists; never reset or replace")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Set permissions at creation, including when invoked outside the unit's UMask.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            PRAGMA synchronous=FULL;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE attempt(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                candidate TEXT NOT NULL,state TEXT NOT NULL,updated_at TEXT NOT NULL,
                record TEXT,detail TEXT);
        """)
        db.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            [
                ("format", "sandbox-long-once-v1"),
                ("account_id", account_id),
                ("anchor_path", str(target)),
            ],
        )


class OnceLedger:
    def __init__(self, path: Path, account_id: str, *, anchor: Path | None = None) -> None:
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError("existing explicit one-shot ledger required; never recreate")
        self.path, self.account_id = path, account_id
        self.anchor = _anchor_path(path, anchor)
        with closing(self.connect()) as db:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",) or dict(
                db.execute("SELECT key,value FROM metadata")
            ) != {
                "format": "sandbox-long-once-v1",
                "account_id": account_id,
                "anchor_path": str(self.anchor),
            }:
                raise ValueError("one-shot ledger corrupt or bound to another account")
        self._check_anchor()

    def _check_anchor(self) -> None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT candidate FROM attempt WHERE singleton=1").fetchone()
        if row is None:
            if self.anchor.exists():
                raise ValueError("CRITICAL: restored WAITING ledger has independent claim anchor")
            return
        if not self.anchor.is_file() or self.anchor.is_symlink():
            raise ValueError("CRITICAL: claimed ledger lacks independent anchor; never retry")
        expected = {
            "version": 1,
            "account_id": self.account_id,
            "candidate_sha256": hashlib.sha256(row[0].encode()).hexdigest(),
        }
        if json.loads(self.anchor.read_text(encoding="utf-8")) != expected:
            raise ValueError("CRITICAL: claim anchor/account/candidate mismatch")
        fence = self.anchor.with_suffix(self.anchor.suffix + ".stop-dispatch")
        if fence.exists():
            if fence.is_symlink() or json.loads(fence.read_text()) != expected:
                raise ValueError("CRITICAL: native stop dispatch fence mismatch")
            with closing(self.connect()) as db:
                state = db.execute("SELECT state FROM attempt WHERE singleton=1").fetchone()[0]
            if state not in {"STOP_PENDING", "FILLED_PROTECTED", "QUARANTINED"}:
                raise ValueError(
                    "CRITICAL: restored ledger predates independent native stop dispatch"
                )

    @contextmanager
    def workflow_lock(self) -> Iterator[None]:
        target = self.anchor.with_suffix(self.anchor.suffix + ".lock")
        if target.is_symlink():
            raise ValueError("one-shot process lock cannot be a symlink")
        with target.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt

                if target.stat().st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ValueError("one-shot workflow already running") from exc
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = import_module("fcntl")

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ValueError("one-shot workflow already running") from exc
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def mutation_guard(self) -> None:
        self._check_anchor()
        state = self.status()
        if state == "WAITING":
            raise ValueError("one-shot mutation has no durable claim")
        if state == "STOP_PENDING":
            fence = self.anchor.with_suffix(self.anchor.suffix + ".stop-dispatch")
            # Exclusive external fence precedes the child transport even across DB rollback.
            with fence.open("x", encoding="utf-8") as stream:
                stream.write(self.anchor.read_text(encoding="utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                fd = os.open(fence.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def connect(self) -> sqlite3.Connection:
        # mode=rw prevents implicit DB creation after deletion/race.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def claim(self, candidate: Candidate, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("aware one-shot claim time required")
        payload = json.dumps(asdict(candidate), sort_keys=True, default=str)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM attempt").fetchone():
                return False
            db.execute(
                "INSERT INTO attempt VALUES(1,?,'CLAIMED',?,NULL,NULL)", (payload, now.isoformat())
            )
        anchor_body = {
            "version": 1,
            "account_id": self.account_id,
            "candidate_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
        with self.anchor.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                self.anchor.chmod(0o600)
            json.dump(anchor_body, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            fd = os.open(self.anchor.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._check_anchor()
        return True

    def status(self) -> str:
        self._check_anchor()
        with closing(self.connect()) as db:
            row = db.execute("SELECT state FROM attempt WHERE singleton=1").fetchone()
        return str(row[0]) if row else "WAITING"

    def record(
        self, state: str, now: datetime, record: OrderRecord | None = None, detail: str = ""
    ) -> None:
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                "UPDATE attempt SET state=?,updated_at=?,record=COALESCE(?,record),"
                "detail=? WHERE singleton=1",
                (
                    state,
                    now.isoformat(),
                    json.dumps(asdict(record), default=str) if record else None,
                    detail,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("durable one-shot claim missing")


def _run_sandbox_candidate(
    candidate: Candidate,
    path: Path,
    broker: SandboxPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    polls: int = 30,
    anchor: Path | None = None,
) -> str:
    if (
        broker.environment != "sandbox"
        or broker.account_id != candidate.account_id
        or type(polls) is not int
        or not 1 <= polls <= 30
    ):
        raise ValueError("exact sandbox one-shot account and bounded polling required")
    ledger = OnceLedger(path, candidate.account_id, anchor=anchor)
    broker.set_mutation_guard(ledger.mutation_guard)
    with closing(ledger.connect()) as db:
        if db.execute("SELECT 1 FROM attempt").fetchone():
            state = ledger.status()
            severity = (
                "BLOCKED" if state in {"FILLED_PROTECTED", "ENTRY_NOT_FILLED"} else "CRITICAL"
            )
            print(f"{severity}: sandbox LONG one-shot {state}; no broker mutation/retry")
            return "ALREADY_CLAIMED"
    broker.preflight(candidate)
    if not ledger.claim(candidate, clock()):
        print("BLOCKED: sandbox LONG one-shot already consumed; inspect ledger, never retry")
        return "ALREADY_CLAIMED"
    try:
        entry = broker.submit(candidate)
        if (
            entry.intent.order_request_id != candidate.order_request_id
            or entry.intent.instrument != candidate.instrument
            or entry.intent.side is not Side.BUY
            or entry.intent.lots != 1
            or entry.intent.limit_price != candidate.limit_price
        ):
            raise ValueError("one-shot broker entry differs from immutable candidate")
        ledger.record("ENTRY_OBSERVED", clock(), entry)
        entry = broker.lookup(entry)  # Independently verify even a terminal POST acknowledgement.
        for _ in range(polls):
            ledger.record("ENTRY_OBSERVED", clock(), entry)
            if entry.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                break
            sleep(1)
            entry = broker.lookup(entry)
        if entry.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
            ledger.record("CANCEL_PENDING", clock(), entry)
            entry = broker.cancel_unfilled_verified(entry)
        ledger.record("ENTRY_OBSERVED", clock(), entry)
        if entry.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED} and entry.filled_lots == 0:
            ledger.record("ENTRY_NOT_FILLED", clock(), entry)
            return "ENTRY_NOT_FILLED"
        if entry.status is not OrderStatus.FILLED or entry.filled_lots != 1:
            raise ValueError("one-shot entry fill is ambiguous")
        ledger.record("STOP_PENDING", clock(), entry)  # Before ANY child mutation.
        response = broker.protect(candidate, entry)
        verified = broker.verify_protection(candidate, response)
        if verified.get("verified") is not True:
            raise ValueError("native sandbox protection not verified")
        ledger.record(
            "FILLED_PROTECTED", clock(), entry, json.dumps(verified, sort_keys=True, default=str)
        )
        print("PASS: sandbox LONG one-shot FILLED_PROTECTED; no further entries permitted")
        return "FILLED_PROTECTED"
    except Exception as exc:
        ledger.record("QUARANTINED", clock(), detail=type(exc).__name__)
        print("CRITICAL: sandbox LONG one-shot QUARANTINED; inspect broker parent/stop; no retries")
        return "QUARANTINED"


def _recover_sandbox_protection(
    candidate: Candidate,
    path: Path,
    broker: SandboxPort,
    *,
    anchor: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Explicit stop-only recovery after crash BEFORE the durable child reservation.

    STOP_PENDING/QUARANTINED/unknown delivery never resume or repost a child.
    No entry preflight, BUY, cancel, SELL, funding or account creation can occur here.
    """
    if broker.environment != "sandbox" or broker.account_id != candidate.account_id:
        raise ValueError("exact sandbox recovery account required")
    ledger = OnceLedger(path, candidate.account_id, anchor=anchor)
    broker.set_mutation_guard(ledger.mutation_guard)
    expected = json.dumps(asdict(candidate), sort_keys=True, default=str)
    with closing(ledger.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT candidate,state,record FROM attempt WHERE singleton=1").fetchone()
        if row is None or row[0] != expected or row[1] != "ENTRY_OBSERVED" or not row[2]:
            raise ValueError("no unequivocal pre-child recovery state; never repost")
        raw = json.loads(row[2])
        intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
        if (
            raw["status"] != "filled"
            or raw["filled_lots"] != 1
            or intent.instrument != candidate.instrument
            or intent.side is not Side.BUY
            or intent.order_request_id != candidate.order_request_id
            or intent.lots != 1
            or intent.limit_price != candidate.limit_price
        ):
            raise ValueError("exact durable FILLED parent required for stop-only recovery")
        entry = OrderRecord(intent, OrderStatus.FILLED, 1, raw["broker_order_id"])
        db.execute(
            "UPDATE attempt SET state='STOP_PENDING',updated_at=? WHERE singleton=1",
            (clock().isoformat(),),
        )
    try:
        actual = broker.lookup(entry)
        if actual != entry:
            raise ValueError("recovery parent changed; manual reconciliation required")
        response = broker.protect(candidate, actual)
        verified = broker.verify_protection(candidate, response)
        if verified.get("verified") is not True:
            raise ValueError("recovered stop not verified")
        ledger.record(
            "FILLED_PROTECTED", clock(), entry, json.dumps(verified, sort_keys=True, default=str)
        )
        return "FILLED_PROTECTED"
    except Exception as exc:
        ledger.record("QUARANTINED", clock(), detail=type(exc).__name__)
        print("CRITICAL: sandbox stop-only recovery QUARANTINED; no retries")
        return "QUARANTINED"


def run_sandbox_candidate(
    candidate: Candidate,
    path: Path,
    broker: SandboxPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    polls: int = 30,
    anchor: Path | None = None,
) -> str:
    ledger = OnceLedger(path, candidate.account_id, anchor=anchor)
    with ledger.workflow_lock():
        return _run_sandbox_candidate(
            candidate, path, broker, clock=clock, sleep=sleep, polls=polls, anchor=anchor
        )


def recover_sandbox_protection(
    candidate: Candidate,
    path: Path,
    broker: SandboxPort,
    *,
    anchor: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    ledger = OnceLedger(path, candidate.account_id, anchor=anchor)
    with ledger.workflow_lock():
        return _recover_sandbox_protection(candidate, path, broker, anchor=anchor, clock=clock)


def load_saved_candidate(path: Path, account_id: str, *, anchor: Path | None = None) -> Candidate:
    from .long_auto_signal import Candidate

    ledger = OnceLedger(path, account_id, anchor=anchor)
    with closing(ledger.connect()) as db:
        row = db.execute("SELECT candidate FROM attempt WHERE singleton=1").fetchone()
    if not row:
        raise ValueError("one-shot has no saved candidate")
    raw = json.loads(row[0])
    raw["instrument"]["tick_size"] = Decimal(raw["instrument"]["tick_size"])
    raw["instrument"] = Instrument(**raw["instrument"])
    raw["limit_price"], raw["stop_trigger"] = (
        Decimal(raw["limit_price"]),
        Decimal(raw["stop_trigger"]),
    )
    raw["valid_until"] = datetime.fromisoformat(raw["valid_until"])
    candidate = Candidate(**raw)
    if candidate.account_id != account_id:
        raise ValueError("saved candidate belongs to another sandbox account")
    return candidate


def reconcile_sandbox_once(
    path: Path, account_id: str, broker: SandboxPort, *, anchor: Path | None = None
) -> dict[str, Any]:
    """Read-only broker reconciliation by anchored entry UUID, including lost POST acknowledgement.

    Does not change ledger states or authorize recovery/re-entry/cancellation.
    """
    if broker.environment != "sandbox" or broker.account_id != account_id:
        raise ValueError("exact sandbox account required for read-only reconciliation")
    ledger = OnceLedger(path, account_id, anchor=anchor)
    with ledger.workflow_lock():
        candidate = load_saved_candidate(path, account_id, anchor=anchor)
        with closing(ledger.connect()) as db:
            row = db.execute("SELECT record,state FROM attempt WHERE singleton=1").fetchone()
        if row[0]:
            raw = json.loads(row[0])
            intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
            parent = OrderRecord(
                intent, OrderStatus(raw["status"]), raw["filled_lots"], raw["broker_order_id"]
            )
        else:
            intent = OrderIntent(
                candidate.order_request_id,
                candidate.instrument,
                Side.BUY,
                1,
                candidate.limit_price,
                candidate.limit_price * candidate.instrument.lot_size,
                "sandbox one-shot long genuine signal",
            )
            parent = OrderRecord(intent, OrderStatus.UNKNOWN)
        actual = broker.lookup(parent)
        return {
            "state": row[1],
            "broker_status": actual.status.value,
            "filled_lots": actual.filled_lots,
            "exchange_order_id": actual.broker_order_id,
            "read_only": True,
            "operator_review_required": row[1] not in {"FILLED_PROTECTED", "ENTRY_NOT_FILLED"}
            or parent.status != actual.status
            or parent.filled_lots != actual.filled_lots,
        }
