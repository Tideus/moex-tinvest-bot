"""Explicit second sandbox admission. Original ledger/claim are never reset or replaced."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .domain import OrderRecord, OrderStatus
from .long_auto_once import OnceLedger, init
from .long_auto_sandbox import SandboxLongBroker, _rows
from .long_auto_signal import Candidate
from .sandbox_execution import decode_validated_intent


class OriginalLedger(OnceLedger):
    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)


@dataclass(frozen=True)
class Binding:
    state: Path
    anchor: Path
    admitted_at: datetime | None = None
    previous_decision: str = ""

    def check_candidate(self, candidate: Candidate, now: datetime) -> None:
        if self.admitted_at is None:
            return
        if (now.tzinfo is None or not self.admitted_at <= now < candidate.valid_until
                or candidate.valid_until - timedelta(minutes=5) <= self.admitted_at
                or candidate.decision_sha256 == self.previous_decision
                or candidate.lots != 1
                or not candidate.limit_price.is_finite()
                or not 0 < candidate.limit_price * candidate.instrument.lot_size <= 10_000):
            raise ValueError("second attempt requires a NEW post-admission signal, 1 lot <=10000")


def paths(state: Path, anchor: Path) -> tuple[Path, Path, Path]:
    return (state.with_name("sandbox-retry-2.sqlite3"),
            anchor.with_name("entry-retry-2.claim"),
            anchor.parent.parent / "sandbox-long-retry-2.json")


def digest(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise ValueError("regular single-link evidence required")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted_receipt(path: Path) -> None:
    info = path.lstat()
    parent = path.parent.lstat()
    if (os.name != "posix" or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
            or parent.st_mode & 0o022 or path.resolve() != path):
        raise ValueError("second admission requires trusted root-owned receipt")


def original_record(state: Path, anchor: Path, account: str) -> tuple[dict[str, Any], OrderRecord]:
    ledger = OriginalLedger(state, account, anchor=anchor)
    if ledger.status() != "ENTRY_NOT_FILLED":
        raise ValueError("only a terminal unfilled original attempt can be admitted again")
    with closing(ledger.connect()) as db:
        saved, record = db.execute(
            "SELECT candidate,record FROM attempt WHERE singleton=1").fetchone()
    candidate, raw = json.loads(saved), json.loads(record)
    intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
    parent = OrderRecord(
        intent, OrderStatus(raw["status"]), raw["filled_lots"], raw["broker_order_id"])
    if (parent.status is not OrderStatus.CANCELLED or parent.filled_lots != 0
            or not parent.broker_order_id
            or intent.order_request_id != candidate["order_request_id"]
            or intent.instrument.uid != candidate["instrument"]["uid"]
            or candidate["account_id"] != account):
        raise ValueError("original cancelled zero-fill order identity required")
    return candidate, parent


def resolve(state: Path, anchor: Path, account: str) -> Binding:
    second, claim, receipt = paths(state, anchor)
    if not receipt.exists() and not receipt.is_symlink():
        if second.exists() or second.is_symlink() or claim.exists() or claim.is_symlink():
            raise ValueError("orphan second-attempt evidence; never fall back or recreate")
        return Binding(state, anchor)
    trusted_receipt(receipt)
    raw = json.loads(receipt.read_bytes())
    candidate, _ = original_record(state, anchor, account)
    at = datetime.fromisoformat(raw["admitted_at"])
    expected = {
        "version": 1, "attempt": 2, "environment": "sandbox",
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "original_ledger_sha256": digest(state), "original_anchor_sha256": digest(anchor),
        "previous_decision": candidate["decision_sha256"],
        "admitted_at": raw["admitted_at"], "lots": 1, "max_notional_rub": "10000",
    }
    if raw != expected or at.tzinfo is None:
        raise ValueError("second admission/old evidence mismatch; possible rollback")
    # Missing DB/claim, WAITING restoration and dispatched-stop rollback fail closed.
    OriginalLedger(second, account, anchor=claim).status()
    return Binding(second, claim, at, candidate["decision_sha256"])


def require_empty(broker: SandboxLongBroker, account: str) -> None:
    if broker.environment != "sandbox" or broker.account_id != account:
        raise ValueError("exact sandbox account required")
    accounts = _rows(broker._post("SandboxService/GetSandboxAccounts", {}), "accounts")
    if len([a for a in accounts if a.get("id") == account
            and a.get("status") == "ACCOUNT_STATUS_OPEN"]) != 1:
        raise ValueError("working LONG sandbox is not open")
    positions = broker._sandbox("GetSandboxPositions")
    if (positions.get("accountId", account) != account
            or positions.get("limitsLoadingInProgress") is True
            or any(_rows(positions, k) for k in ("securities", "futures", "options"))
            or _rows(broker._sandbox("GetSandboxOrders"), "orders")
            or _rows(broker._sandbox("GetSandboxStopOrders", {
                "status": "STOP_ORDER_STATUS_ACTIVE"}), "stopOrders")):
        raise ValueError("second admission requires flat LONG sandbox with no orders/stops")


def authorize(state: Path, anchor: Path, account: str, broker: SandboxLongBroker,
              now: datetime) -> Binding:
    """Caller MUST hold writer_lock and enforce canonical sandbox enrollment/runtime.

    Creates only local evidence, never a broker mutation. A crash leaves evidence
    for operator review, not an automatically re-created/re-armed empty journal.
    """
    require_root()
    if now.tzinfo is None:
        raise ValueError("aware timestamp required for explicit second admission")
    current = resolve(state, anchor, account)
    if current.admitted_at is not None:
        return current  # Idempotent repeated command, never a third admission.
    before = digest(state), digest(anchor)
    candidate, parent = original_record(state, anchor, account)
    if broker.environment != "sandbox" or broker.account_id != account:
        raise ValueError("exact sandbox broker required")
    actual = broker.lookup(parent)
    if (actual.intent != parent.intent or actual.broker_order_id != parent.broker_order_id
            or actual.status is not OrderStatus.CANCELLED or actual.filled_lots != 0):
        raise ValueError("broker has not confirmed original cancellation with zero fills")
    require_empty(broker, account)
    if (digest(state), digest(anchor)) != before:
        raise ValueError("original evidence changed during admission")
    second, claim, receipt = paths(state, anchor)
    if receipt.exists() or second.exists() or claim.exists():
        raise ValueError("second admission already exists; never replace")
    init(second, account, anchor=claim)
    owner = state.stat()
    inherit_owner(second, owner)
    raw = {
        "version": 1, "attempt": 2, "environment": "sandbox",
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "original_ledger_sha256": before[0], "original_anchor_sha256": before[1],
        "previous_decision": candidate["decision_sha256"], "admitted_at": now.isoformat(),
        "lots": 1, "max_notional_rub": "10000",
    }
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        receipt_permissions(stream.fileno(), owner.st_gid)
        json.dump(raw, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    for folder in (second.parent, receipt.parent):
        sync_directory(folder)
    return resolve(state, anchor, account)


def require_root() -> None:
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        raise ValueError("root required for explicit second admission")


def inherit_owner(path: Path, owner: os.stat_result) -> None:
    method = "chown"
    getattr(os, method)(path, owner.st_uid, owner.st_gid)


def receipt_permissions(fd: int, group: int) -> None:
    ownership, permissions = "fchown", "fchmod"
    getattr(os, ownership)(fd, 0, group)
    getattr(os, permissions)(fd, 0o640)


def sync_directory(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
