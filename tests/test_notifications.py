from datetime import UTC, datetime, timedelta
from pathlib import Path

from moex_bot.notifications import SQLiteOutbox, deliver_pending

NOW = datetime(2026, 8, 14, 10, tzinfo=UTC)


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = fail

    def send_text(self, chat_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("offline")
        self.messages.append((chat_id, text))


def test_outbox_deduplicates_and_delivers_once(tmp_path: Path) -> None:
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    assert outbox.enqueue(kind="shadow", dedupe_key="run-1", body="ok", now=NOW)
    assert not outbox.enqueue(kind="shadow", dedupe_key="run-1", body="ok", now=NOW)
    sender = RecordingSender()
    assert deliver_pending(outbox, sender, chat_id="42", now=NOW) == (1, 0)
    assert deliver_pending(outbox, sender, chat_id="42", now=NOW) == (0, 0)
    assert sender.messages == [("42", "ok")]
    assert outbox.counts() == {"sent": 1}
    assert outbox.health(now=NOW) == {"pending_due": 0, "dead": 0}


def test_delivery_failure_is_retried_without_secret_error(tmp_path: Path) -> None:
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue(kind="flow", dedupe_key="flow-1", body="body", now=NOW)
    assert deliver_pending(outbox, RecordingSender(fail=True), chat_id="42", now=NOW) == (0, 1)
    assert outbox.counts() == {"pending": 1}
    sender = RecordingSender()
    assert deliver_pending(outbox, sender, chat_id="42", now=NOW + timedelta(seconds=3)) == (1, 0)


def test_outbox_rejects_oversized_telegram_message(tmp_path: Path) -> None:
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    try:
        outbox.enqueue(kind="x", dedupe_key="x", body="x" * 4097, now=NOW)
    except ValueError as exc:
        assert "4096" in str(exc)
    else:
        raise AssertionError("oversized message should fail")


def test_dead_messages_can_be_inspected_and_requeued(tmp_path: Path) -> None:
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue(kind="shadow", dedupe_key="run-dead", body="body", now=NOW)
    for attempt in range(8):
        retry_at = NOW + timedelta(hours=attempt)
        claimed = outbox.claim(now=retry_at)
        assert len(claimed) == 1
        outbox.mark_failed(claimed[0], now=retry_at, reason="RuntimeError")

    issues = outbox.issues(now=NOW + timedelta(days=1))
    assert len(issues) == 1
    assert issues[0].status == "dead"
    assert issues[0].dedupe_key == "run-dead"
    assert issues[0].attempts == 8
    assert issues[0].last_error == "RuntimeError"

    assert outbox.requeue_dead(now=NOW + timedelta(days=1)) == 1
    assert outbox.health(now=NOW + timedelta(days=1)) == {
        "pending_due": 1,
        "dead": 0,
    }
    assert deliver_pending(
        outbox,
        RecordingSender(),
        chat_id="42",
        now=NOW + timedelta(days=1),
    ) == (1, 0)
