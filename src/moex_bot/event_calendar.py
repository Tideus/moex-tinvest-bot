"""Read-only investor calendar. Deliberately isolated from order execution."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .integrations.tinvest_sandbox import JsonPostTransport, UrlLibJsonTransport
from .service_config import PROD_REST


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value.astimezone(UTC).isoformat()


class EventStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, uid TEXT NOT NULL,
                    received_at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, uid TEXT NOT NULL,
                    secid TEXT NOT NULL, event_at TEXT NOT NULL, body TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event_revisions (
                    event_id TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL,
                    UNIQUE(event_id, received_at, body));
                CREATE TABLE IF NOT EXISTS refreshes (
                    received_at TEXT NOT NULL, failures INTEGER NOT NULL);
            """)

    def connect(self) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def connection() -> Any:
            with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                yield db
        return connection()

    def record(self, kind: str, uid: str, secid: str, response: dict[str, Any],
               now: datetime) -> None:
        received = stamp(now)
        key = {"report": "events", "dividend": "dividends", "session": "exchanges"}[kind]
        if set(response) - {key}:
            raise ValueError("unexpected calendar response fields")
        rows = response.get(key, [])  # {} is a valid protobuf empty repeated field.
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid calendar response")
        with self.connect() as db:
            db.execute("INSERT INTO observations(source,uid,received_at,body) VALUES(?,?,?,?)",
                       (kind, uid, received, json.dumps(response, sort_keys=True)))
            dates_by_id: dict[str, str] = {}
            for row in rows:
                if kind == "session":
                    continue  # Preserve exact sessions, do not flatten into corporate events.
                event_at = row.get("reportDate" if kind == "report" else "recordDate")
                if not event_at:
                    raise ValueError("calendar event has no date")
                at = stamp(datetime.fromisoformat(str(event_at).replace("Z", "+00:00")))
                # Keep ambiguous same-period dates separate; never invent a cancellation.
                identity = ([kind, uid, row.get("periodYear"), row.get("periodNum"),
                             row.get("periodType")] if kind == "report" else [kind, uid, at])
                event_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                if event_id in dates_by_id and dates_by_id[event_id] != at:
                    raise ValueError("ambiguous report dates for one reporting period")
                dates_by_id[event_id] = at
                body = json.dumps(row, sort_keys=True)
                previous = db.execute("SELECT body FROM events WHERE event_id=?",
                                      (event_id,)).fetchone()
                if previous is None or previous[0] != body:
                    db.execute("INSERT INTO event_revisions VALUES(?,?,?)",
                               (event_id, received, body))
                db.execute("""INSERT INTO events VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET body=excluded.body,
                    event_at=excluded.event_at,
                    last_seen=excluded.last_seen""",
                           (event_id, kind, uid, secid, at, body, received, received))

    def status(self, now: datetime, days: int = 7) -> str:
        with self.connect() as db:
            last = db.execute("SELECT received_at, failures FROM refreshes "
                              "ORDER BY rowid DESC LIMIT 1").fetchone()
            rows = db.execute("SELECT secid,kind,event_at,last_seen FROM events "
                              "WHERE event_at>=? AND event_at<=? ORDER BY event_at,secid",
                              (stamp(day_start(now)), stamp(now + timedelta(days=days)))).fetchall()
        fresh = bool(last and now - datetime.fromisoformat(last[0]) < timedelta(hours=26))
        quality = "OK" if fresh and last and last[1] == 0 else "INCOMPLETE/STALE"
        lines = [f"Календарь инвестора · {days} дней · {quality}",
                 f"Последнее обновление: {last[0] if last else 'никогда'}",
                 "Наблюдение: календарь не меняет торговые решения."]
        lines += [f"{at[:10]} · {secid} · {kind}"
                  + (" · UNCONFIRMED" if last and seen < last[0] else "")
                  for secid, kind, at, seen in rows]
        if not rows:
            lines.append("Нет известных событий. Это не гарантирует отсутствие событий.")
        return "\n".join(lines)


def day_start(now: datetime) -> datetime:
    return now.astimezone(ZoneInfo("Europe/Moscow")).replace(
        hour=0, minute=0, second=0, microsecond=0)


def load_calendar_config(config: Path) -> dict[str, Any]:
    settings: dict[str, Any] = json.loads(config.read_text(encoding="utf-8"))
    days = settings["horizon_days"]
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("horizon_days must be 1..365")
    if settings.get("mode") != "observe":
        raise ValueError("only observe mode is supported")
    if not isinstance(settings.get("token_env"), str) or not settings["token_env"]:
        raise ValueError("token_env required")
    if not isinstance(settings.get("exchange"), str) or not settings["exchange"]:
        raise ValueError("exchange required")
    return settings


def refresh(config: Path, universe: Path, database: Path, now: datetime,
            transport: JsonPostTransport | None = None) -> int:
    settings = load_calendar_config(config)
    days = settings["horizon_days"]
    token = os.environ.get(settings["token_env"], "").strip()
    if not token:
        raise ValueError("calendar read-only token is missing")
    client = transport or UrlLibJsonTransport()
    store = EventStore(database)
    instruments = json.loads(universe.read_text(encoding="utf-8"))
    failures = 0
    requests: list[tuple[str, str, str, str, dict[str, object]]] = []
    for instrument in instruments:
        uid, secid = instrument["t_invest_uid"], instrument["secid"]
        for kind, method in (("report", "GetAssetReports"), ("dividend", "GetDividends")):
            requests.append((kind, method, uid, secid, {
                "instrumentId": uid, "from": stamp(day_start(now)),
                "to": stamp(now + timedelta(days=days)),
            }))
    requests.append(("session", "TradingSchedules", "", "", {
        "exchange": settings["exchange"], "from": stamp(now),
        "to": stamp(now + timedelta(days=7)),
    }))
    for kind, method, uid, secid, payload in requests:
        try:
            response = client.post(
                f"{PROD_REST}/tinkoff.public.invest.api.contract.v1.InstrumentsService/{method}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                payload=payload, timeout_seconds=20,
            )
            store.record(kind, uid, secid, dict(response), now)
        except Exception:
            # Exception text may contain credentials/headers. Never print it.
            print(f"FAIL: calendar {method} {secid}: request/response failed")
            failures += 1
    with store.connect() as db:
        db.execute("INSERT INTO refreshes VALUES(?,?)", (stamp(now), failures))
    print(store.status(now))
    return 2 if failures else 0
