from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .notifications import SQLiteOutbox


def notify(checks: dict[str, str], database: Path, outbox: SQLiteOutbox,
           now: datetime, settings: dict[str, Any]) -> int:
    with closing(sqlite3.connect(database, timeout=10)) as db, db:
        snapshot_id = hashlib.sha256(json.dumps(
            [now.isoformat(), checks], sort_keys=True).encode()).hexdigest()
        db.execute("CREATE TABLE IF NOT EXISTS monitoring_runs (snapshot_id TEXT PRIMARY KEY)")
        previous = db.execute("SELECT 1 FROM monitoring_runs WHERE snapshot_id=?",
                              (snapshot_id,)).fetchone()
        if previous:
            return sum(value != "OK" for value in checks.values())
        db.execute("CREATE TABLE IF NOT EXISTS incidents "
                   "(key TEXT PRIMARY KEY, failures INTEGER, alerted INTEGER, last_sent REAL)")
        for key, value in checks.items():
            row = db.execute("SELECT failures,alerted,last_sent FROM incidents WHERE key=?",
                             (key,)).fetchone() or (0, 0, 0)
            failures, alerted, sent = row
            failed = value != "OK"
            failures = failures + 1 if failed else 0
            if value.startswith("CRITICAL"):
                failures = max(failures, settings["failures_to_alert"])
            if ((failed and failures >= settings["failures_to_alert"] and
                 (not alerted or now.timestamp() - sent >= settings["repeat_hours"] * 3600))
                    or (not failed and alerted)):
                body = f"{'🚨 СБОЙ' if failed else '✅ ВОССТАНОВЛЕНО'} · MOEX BOT\n{key}\n{value}"
                # Last committed transition identifies the next event across a retry/crash.
                event_key = f"monitor:{key}:{'down' if failed else 'up'}:{sent}"
                outbox.enqueue(kind="monitoring", dedupe_key=event_key,
                               body=body, now=now)
                sent, alerted = now.timestamp(), int(failed)
            db.execute("INSERT OR REPLACE INTO incidents VALUES(?,?,?,?)",
                       (key, failures, alerted, sent))
        db.execute("INSERT INTO monitoring_runs VALUES(?)", (snapshot_id,))
    return sum(value != "OK" for value in checks.values())
