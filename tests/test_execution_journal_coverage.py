"""Journal failure paths, transactional safety and immutable terminal results."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_execution_journal import intent

from moex_bot.domain import OrderRecord, OrderStatus
from moex_bot.execution_journal import ExecutionJournal


def test_journal_clock_and_reservation_guards(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    assert not ExecutionJournal.is_initialized(tmp_path / "missing")
    journal = ExecutionJournal(tmp_path / "orders", clock=lambda: now)
    with pytest.raises(ValueError, match="trading-ready"):
        ExecutionJournal(journal.path, migration_required=True)
    with pytest.raises(ValueError, match="timezone-aware"):
        journal.reserve(
            "a",
            intent(),
            "p",
            now,
            residual_exit=False,
            authorize=lambda db: now.replace(tzinfo=None),
        )
    key = journal.reserve("a", intent(), "p", now, residual_exit=False, authorize=lambda db: now)
    sent = replace(intent(), order_request_id=key)
    journal.clock = lambda: now.replace(tzinfo=None)
    with pytest.raises(ValueError, match="clock must"):
        journal.record("a", OrderRecord(sent, OrderStatus.ACCEPTED))
    journal.clock = lambda: now - timedelta(seconds=1)
    with pytest.raises(ValueError, match="moved behind"):
        journal.record("a", OrderRecord(sent, OrderStatus.ACCEPTED))
    journal.clock = lambda: now
    with pytest.raises(ValueError, match="reservation missing"):
        journal.record("another", OrderRecord(sent, OrderStatus.ACCEPTED))
    journal.record("a", OrderRecord(sent, OrderStatus.PARTIALLY_FILLED, 2))
    with pytest.raises(ValueError, match="cannot decrease"):
        journal.record("a", OrderRecord(sent, OrderStatus.ACCEPTED))
    journal.record("a", OrderRecord(sent, OrderStatus.CANCELLED, 2))
    journal.record("a", OrderRecord(sent, OrderStatus.CANCELLED, 2))
    # Missing completion metadata must never authorize a residual retry.
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE actions SET completed_at=NULL")
    with pytest.raises(ValueError, match="residual exit"):
        journal.reserve("a", intent(), "next", now + timedelta(seconds=1), residual_exit=True)


def test_update_failure_rolls_back_and_keeps_pending(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "orders")
    key = journal.reserve("a", intent(), "p", datetime.now(UTC), residual_exit=False)
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE ON actions BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(ValueError, match="reservation missing"):
        journal.record(
            "a", OrderRecord(replace(intent(), order_request_id=key), OrderStatus.REJECTED)
        )
    assert journal.unresolved_count() == 1


def test_legacy_import_quarantine_manifest_and_invalid_entries(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    record = OrderRecord(intent(), OrderStatus.FILLED, 10)
    ready = ExecutionJournal(tmp_path / "ready")
    with pytest.raises(ValueError, match="quarantined"):
        ready.stage_legacy((), "manifest")
    journal = ExecutionJournal(tmp_path / "candidate", migration_required=True)
    with pytest.raises(ValueError, match="timezone"):
        journal.stage_legacy((("a", "p", now.replace(tzinfo=None), record),), "manifest")
    with pytest.raises(ValueError, match="quantity"):
        journal.stage_legacy((("a", "p", now, replace(record, filled_lots=11)),), "manifest")
    journal.stage_legacy((("a", "p", now, record),), "manifest")
    journal.stage_legacy((("a", "p", now, record),), "manifest")
    with pytest.raises(ValueError, match="manifest changed"):
        journal.stage_legacy((), "different")
    with sqlite3.connect(journal.path) as db:
        db.execute("DELETE FROM metadata WHERE key='legacy_manifest'")
    with pytest.raises(ValueError, match="unexpected orders"):
        journal.stage_legacy((), "manifest")
