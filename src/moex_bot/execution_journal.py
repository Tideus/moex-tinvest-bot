"""Durable reservation before network IO. Unknown delivery never permits another submit."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .domain import OrderIntent, OrderRecord, OrderStatus

TERMINAL = {"filled", "cancelled", "rejected"}


class ExecutionJournal:
    @staticmethod
    def is_initialized(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                valid = db.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
                db.execute("SELECT account,logical_id,request_id,state FROM actions LIMIT 0")
                return bool(valid == ("1",))
        except sqlite3.Error:
            return False

    def __init__(
        self,
        path: Path,
        *,
        migration_required: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if path.exists() and not self.is_initialized(path):
            raise ValueError("execution journal is uninitialized or corrupt; restore/reconcile it")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS actions (
                account TEXT NOT NULL, logical_id TEXT NOT NULL, generation INTEGER NOT NULL,
                request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, plan_id TEXT NOT NULL,
                state TEXT NOT NULL, snapshot_at TEXT NOT NULL, completed_at TEXT,
                record TEXT, UNIQUE(account, logical_id, generation))""")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('format','1')")
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES('migration_state',?)",
                ("review_required" if migration_required else "ready",),
            )
            if migration_required and db.execute(
                "SELECT value FROM metadata WHERE key='migration_state'"
            ).fetchone() != ("review_required",):
                raise ValueError("cannot import historical records into a trading-ready journal")

    def unresolved_count(self) -> int:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM actions WHERE "
                    "state NOT IN ('filled','cancelled','rejected')"
                ).fetchone()[0]
            )

    def reserve(
        self,
        account: str,
        intent: OrderIntent,
        plan_id: str,
        snapshot_at: datetime,
        *,
        residual_exit: bool,
        authorize: Callable[[sqlite3.Connection], datetime] | None = None,
        broker_request_id: str | None = None,
    ) -> str | None:
        if snapshot_at.tzinfo is None:
            raise ValueError("broker snapshot timezone required")
        payload = json.dumps(asdict(intent), default=str, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM metadata WHERE key='migration_state'").fetchone() != (
                "ready",
            ):
                raise ValueError("journal migration requires independent review before trading")
            last = db.execute(
                "SELECT generation,payload_hash,plan_id,state,completed_at "
                "FROM actions WHERE account=? AND logical_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (account, intent.order_request_id),
            ).fetchone()
            if last and last[1] != digest and (last[2] == plan_id or not residual_exit):
                raise ValueError("same logical order changed payload")
            pending = db.execute(
                "SELECT 1 FROM actions WHERE account=? AND "
                "state NOT IN ('filled','cancelled','rejected') LIMIT 1",
                (account,),
            ).fetchone()
            if pending:
                raise ValueError("durable pending/UNKNOWN order requires broker reconciliation")
            if last and (last[2] == plan_id or not residual_exit):
                return None
            if authorize is not None:
                snapshot_at = authorize(db)
                if snapshot_at.tzinfo is None:
                    raise ValueError("authorization snapshot must be timezone-aware")
            latest = db.execute(
                "SELECT plan_id,completed_at FROM actions WHERE account=? "
                "AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1",
                (account,),
            ).fetchone()
            if latest and latest[0] != plan_id and snapshot_at <= datetime.fromisoformat(latest[1]):
                raise ValueError("new plan requires broker snapshot after latest terminal order")
            if last and (not last[4] or snapshot_at <= datetime.fromisoformat(last[4])):
                raise ValueError("residual exit requires snapshot after terminal reconciliation")
            request_id = broker_request_id or str(uuid4())
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    intent.order_request_id,
                    1 if last is None else last[0] + 1,
                    request_id,
                    digest,
                    plan_id,
                    "request_pending",
                    snapshot_at.astimezone(UTC).isoformat(),
                    None,
                    json.dumps(
                        asdict(
                            OrderRecord(
                                replace(intent, order_request_id=request_id), OrderStatus.UNKNOWN
                            )
                        ),
                        default=str,
                    ),
                ),
            )
            return request_id

    def record(self, account: str, result: OrderRecord) -> None:
        state = result.status.value
        if not 0 <= result.filled_lots <= result.intent.lots:
            raise ValueError("invalid executed quantity")
        if state == "filled" and result.filled_lots != result.intent.lots:
            raise ValueError("filled order must have full executed quantity")
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("journal clock must be timezone-aware")
        completed = now.astimezone(UTC).isoformat() if state in TERMINAL else None
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT logical_id,payload_hash,state,record,snapshot_at FROM actions "
                "WHERE account=? AND request_id=?",
                (account, result.intent.order_request_id),
            ).fetchone()
            if previous is None:
                raise ValueError("order journal reservation missing")
            if now < datetime.fromisoformat(previous[4]):
                raise ValueError("journal clock moved behind the reserved snapshot")
            original = replace(result.intent, order_request_id=previous[0])
            digest = hashlib.sha256(
                json.dumps(asdict(original), default=str, sort_keys=True).encode()
            ).hexdigest()
            if digest != previous[1]:
                raise ValueError("broker result changed reserved payload")
            old = json.loads(previous[3])
            if result.filled_lots < old["filled_lots"]:
                raise ValueError("executed quantity cannot decrease")
            if previous[2] in TERMINAL:
                if state != previous[2] or result.filled_lots != old["filled_lots"]:
                    raise ValueError("terminal order cannot change state")
                return
            cursor = db.execute(
                "UPDATE actions SET state=?,completed_at=?,record=? "
                "WHERE account=? AND request_id=?",
                (
                    state,
                    completed,
                    json.dumps(asdict(result), default=str),
                    account,
                    result.intent.order_request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("order journal reservation missing")

    def unresolved(self, account: str) -> tuple[dict[str, object], ...]:
        """Persisted intents survive a crash even before submit returns. Never clear on 404."""
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute(
                "SELECT record FROM actions WHERE account=? AND "
                "state NOT IN ('filled','cancelled','rejected')",
                (account,),
            )
            return tuple(json.loads(row[0]) for row in rows)

    def stage_legacy(
        self,
        entries: tuple[tuple[str, str, datetime, OrderRecord], ...],
        manifest: str,
    ) -> None:
        """Atomic candidate import, preserving original request IDs. Never grants trading access."""
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT value FROM metadata WHERE key='migration_state'").fetchone()
            if state != ("review_required",):
                raise ValueError("legacy import requires a quarantined candidate journal")
            previous = db.execute(
                "SELECT value FROM metadata WHERE key='legacy_manifest'"
            ).fetchone()
            if previous is not None:
                if previous != (manifest,):
                    raise ValueError("legacy manifest changed; create a new candidate")
                return
            if db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]:
                raise ValueError("candidate contains unexpected orders")
            for account, plan_id, snapshot_at, record in entries:
                if snapshot_at.tzinfo is None:
                    raise ValueError("legacy timestamp must be timezone-aware")
                intent = record.intent
                if not 0 <= record.filled_lots <= intent.lots:
                    raise ValueError("legacy executed quantity is inconsistent")
                digest = hashlib.sha256(
                    json.dumps(asdict(intent), default=str, sort_keys=True).encode()
                ).hexdigest()
                pending = replace(
                    record,
                    status=OrderStatus.UNKNOWN,
                    status_detail="legacy_requires_broker_confirmation",
                )
                db.execute(
                    "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        intent.order_request_id,
                        1,
                        intent.order_request_id,
                        digest,
                        plan_id,
                        "unknown",
                        snapshot_at.astimezone(UTC).isoformat(),
                        None,
                        json.dumps(asdict(pending), default=str),
                    ),
                )
            db.execute("INSERT INTO metadata VALUES('legacy_manifest',?)", (manifest,))
