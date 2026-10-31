"""Quiescent coordinated SQLite-journal restore drill, NOT a full-VM restore."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_live_protection import _case, _stop

from moex_bot.live_execution import (
    LivePermission,
    execute_live_intent,
    intent_digest,
    policy_digest,
    registered_live_journal,
)
from moex_bot.live_protection import ProtectionJournal
from moex_bot.prod_long import load_prod_long_policy
from moex_bot.sandbox_execution import decode_validated_intent


def _restore_pair(entry: Path, protection: Path, destination: Path) -> tuple[Path, Path]:
    """Both writers are quiescent in this test; no claim of cross-DB online atomicity."""
    destination.mkdir()
    outputs = (destination / "orders.sqlite3", destination / "protection.sqlite3")
    for original, restored in zip((entry, protection), outputs, strict=True):
        with (
            closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(restored)) as target,
        ):
            source.backup(target)
            assert target.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    return outputs


def test_restore_preserves_enrollment_active_stop_and_entry_idempotency(tmp_path):
    plan, journal, entry, broker = _case(tmp_path)
    assert journal.activate(plan, entry, broker) == "ACTIVE"
    enrollment = registered_live_journal(entry, plan.account_id)
    restored_entry, restored_protection = _restore_pair(entry, journal.path, tmp_path / "restored")
    assert registered_live_journal(restored_entry, plan.account_id) == enrollment
    restored = ProtectionJournal(restored_protection)
    assert restored.prepare(plan, restored_entry) == plan.request_id
    assert restored.reconcile(plan, broker) == "ACTIVE"
    assert restored.activate(plan, restored_entry, broker) == "ACTIVE"
    assert broker.posts == 1
    with closing(sqlite3.connect(restored_entry)) as db:
        logical, request, raw = db.execute(
            "SELECT logical_id,request_id,record FROM actions"
        ).fetchone()
    assert (logical, request) == (plan.entry_logical_id, plan.entry_request_id)
    original = json.loads(raw)["intent"]
    original["order_request_id"] = logical
    intent = decode_validated_intent({"status": "validated", "intent": original})
    root = Path(__file__).resolve().parents[1]
    policy = replace(
        load_prod_long_policy(root / "config/prod_long.json"), execution_stage="canary"
    )
    now = datetime.now(UTC)
    release = "a" * 64
    permission = LivePermission(
        plan.account_id,
        intent_digest(intent),
        policy_digest(policy),
        release,
        now - timedelta(seconds=1),
        now + timedelta(minutes=1),
        True,
        enrollment,
    )

    class NoEntryIO:
        account_id = plan.account_id
        environment = "prod"

        def capture(self, intent):
            raise AssertionError("replay must not even capture for new entry")

        def submit(self, intent):
            raise AssertionError("restored terminal entry must never submit again")

    assert (
        execute_live_intent(
            intent=intent,
            policy=policy,
            broker=NoEntryIO(),
            permission=permission,
            release_sha256=release,
            journal_path=restored_entry,
            clock=lambda: now,
        )
        is None
    )


def test_restore_unknown_lost_stop_post_never_retries_or_adopts_manual_stop(tmp_path):
    plan, journal, entry, broker = _case(tmp_path)
    broker.lost = True
    with pytest.raises(TimeoutError):
        journal.activate(plan, entry, broker)
    restored_entry, restored_protection = _restore_pair(entry, journal.path, tmp_path / "restored")
    restored = ProtectionJournal(restored_protection)
    broker.lost = False
    broker.stops = [_stop(plan)]  # Similar manual stop is deliberately not adopted.
    assert restored._row(plan)[0:2] == ("UNKNOWN", None)
    assert restored.activate(plan, restored_entry, broker) == "UNKNOWN"
    assert restored.reconcile(plan, broker) == "UNKNOWN"
    assert broker.posts == 1
