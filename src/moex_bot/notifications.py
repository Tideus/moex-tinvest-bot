from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib import error, parse, request
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True, slots=True)
class Notification:
    notification_id: str
    kind: str
    dedupe_key: str
    body: str
    attempts: int


class MessageSender(Protocol):
    def send_text(self, chat_id: str, text: str) -> None: ...


class SQLiteOutbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    notification_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    lease_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def enqueue(self, *, kind: str, dedupe_key: str, body: str, now: datetime) -> bool:
        _aware(now)
        if not kind or not dedupe_key or not body or len(body) > 4096:
            raise ValueError("notification kind/key/body must be present and body <= 4096 chars")
        notification_id = str(uuid5(NAMESPACE_URL, f"moex-bot:{dedupe_key}"))
        stamp = _stamp(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notification_outbox
                (notification_id, kind, dedupe_key, body, status, available_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (notification_id, kind, dedupe_key, body, stamp, stamp),
            )
        return cursor.rowcount == 1

    def claim(self, *, now: datetime, limit: int = 20) -> tuple[Notification, ...]:
        _aware(now)
        if limit <= 0:
            raise ValueError("limit must be positive")
        stamp = _stamp(now)
        lease = _stamp(now + timedelta(minutes=5))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE notification_outbox SET status='pending', lease_until=NULL "
                "WHERE status='sending' AND lease_until < ?",
                (stamp,),
            )
            rows = conn.execute(
                """
                SELECT notification_id, kind, dedupe_key, body, attempts
                FROM notification_outbox
                WHERE status='pending' AND available_at <= ?
                ORDER BY created_at, notification_id LIMIT ?
                """,
                (stamp, limit),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            conn.executemany(
                "UPDATE notification_outbox SET status='sending', lease_until=? "
                "WHERE notification_id=? AND status='pending'",
                ((lease, item) for item in ids),
            )
        return tuple(Notification(*row) for row in rows)

    def mark_sent(self, notification_id: str, *, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_outbox SET status='sent', sent_at=?, lease_until=NULL "
                "WHERE notification_id=? AND status='sending'",
                (_stamp(now), notification_id),
            )

    def mark_failed(self, notification: Notification, *, now: datetime, reason: str) -> None:
        attempts = notification.attempts + 1
        dead = attempts >= 8
        delay = min(3600, 2 ** min(attempts, 10))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status=?, attempts=?, available_at=?, lease_until=NULL, last_error=?
                WHERE notification_id=? AND status='sending'
                """,
                (
                    "dead" if dead else "pending",
                    attempts,
                    _stamp(now + timedelta(seconds=delay)),
                    reason[:500],
                    notification.notification_id,
                ),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM notification_outbox GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def health(self, *, now: datetime) -> dict[str, int]:
        stamp = _stamp(now)
        with self._connect() as conn:
            pending_due = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE status='pending' AND available_at <= ?",
                (stamp,),
            ).fetchone()
            dead = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE status='dead'"
            ).fetchone()
        return {
            "pending_due": 0 if pending_due is None else int(pending_due[0]),
            "dead": 0 if dead is None else int(dead[0]),
        }


class TelegramBotApiSender:
    def __init__(self, token: str, *, timeout_seconds: float = 10.0) -> None:
        if not token.strip():
            raise ValueError("Telegram bot token is empty")
        self._token = token.strip()
        self._timeout = timeout_seconds

    def send_text(self, chat_id: str, text: str) -> None:
        if not chat_id.strip() or not text or len(text) > 4096:
            raise ValueError("Telegram chat/text is invalid")
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = parse.urlencode(
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        req = request.Request(url, data=payload, method="POST")
        try:
            with request.urlopen(req, timeout=self._timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            raise RuntimeError(f"Telegram HTTP error {exc.code}") from None
        except (error.URLError, TimeoutError):
            raise RuntimeError("Telegram network error") from None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("Telegram returned invalid JSON") from None
        if not parsed.get("ok"):
            raise RuntimeError("Telegram rejected the message")


def deliver_pending(
    outbox: SQLiteOutbox,
    sender: MessageSender,
    *,
    chat_id: str,
    now: datetime,
    limit: int = 20,
) -> tuple[int, int]:
    sent = failed = 0
    for notification in outbox.claim(now=now, limit=limit):
        try:
            sender.send_text(chat_id, notification.body)
        except Exception as exc:  # delivery is isolated from the trading loop
            outbox.mark_failed(notification, now=now, reason=type(exc).__name__)
            failed += 1
        else:
            outbox.mark_sent(notification.notification_id, now=now)
            sent += 1
    return sent, failed


def _aware(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")


def _stamp(value: datetime) -> str:
    _aware(value)
    return value.astimezone(UTC).isoformat()
