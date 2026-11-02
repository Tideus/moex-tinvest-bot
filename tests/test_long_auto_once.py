import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_long_auto_sandbox import case as case

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.long_auto_once import (
    OnceLedger,
    init,
    recover_sandbox_protection,
    run_sandbox_candidate,
)


@dataclass(frozen=True)
class ExampleCandidate:
    account_id: str
    decision_sha256: str
    instrument: Instrument
    limit_price: Decimal
    stop_trigger: Decimal
    order_request_id: str
    stop_request_id: str
    lots: int
    valid_until: datetime


@pytest.fixture
def setup(tmp_path):
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    candidate = ExampleCandidate(
        "sandbox-only",
        "a" * 64,
        Instrument("SBER", "uid", "TQBR", 10, Decimal(".01")),
        Decimal("100"),
        Decimal("95"),
        str(uuid4()),
        str(uuid4()),
        1,
        now + timedelta(minutes=3),
    )
    intent = OrderIntent(
        candidate.order_request_id,
        candidate.instrument,
        Side.BUY,
        1,
        candidate.limit_price,
        Decimal("1000"),
        "test",
    )
    broker = Mock(account_id="sandbox-only", environment="sandbox")
    record = OrderRecord(intent, OrderStatus.FILLED, 1, "exchange")
    broker.submit.return_value = record
    broker.lookup.side_effect = lambda record: record
    broker.protect.return_value = {"stopOrderId": "stop"}
    broker.verify_protection.return_value = {"stopOrderId": "stop", "verified": True}
    path = tmp_path / "once.sqlite3"
    init(path, candidate.account_id)
    return candidate, path, broker, now


def test_once_across_dates_signals_and_exact_ledger_before_post(setup):
    c, path, broker, now = setup
    record = broker.submit.return_value

    def submit(candidate):
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT state FROM attempt").fetchone() == ("CLAIMED",)
        return record

    broker.submit.side_effect = submit
    assert run_sandbox_candidate(c, path, broker, clock=lambda: now) == "FILLED_PROTECTED"
    for another in [c, replace(c, order_request_id=str(uuid4()), decision_sha256="b" * 64)]:
        assert (
            run_sandbox_candidate(another, path, broker, clock=lambda: now + timedelta(days=10))
            == "ALREADY_CLAIMED"
        )
    assert broker.submit.call_count == broker.protect.call_count == 1


def test_windows_lock_byte_is_exclusive_and_released_even_on_exception(setup, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    from moex_bot import long_auto_once as module

    c, path, _, now = setup
    ledger = OnceLedger(path, c.account_id)
    lock = Mock(LK_NBLCK=1, LK_UNLCK=2)
    monkeypatch.setitem(sys.modules, "msvcrt", lock)
    sync = Mock(wraps=os.fsync)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt", fsync=sync))
    with pytest.raises(RuntimeError), ledger.workflow_lock():
        assert ledger.claim(c, now)
        ledger.record("STOP_PENDING", now)
        ledger.mutation_guard()
        assert sync.call_count == 2  # Both files synced; no POSIX directory fsync on Windows.
        raise RuntimeError("process failed")
    assert [call.args[1:] for call in lock.locking.call_args_list] == [(1, 1), (2, 1)]
    lock.locking.side_effect = OSError("occupied")
    with pytest.raises(ValueError, match="already running"), ledger.workflow_lock():
        pytest.fail("overlapping process obtained lock")


@pytest.mark.parametrize("stage", ["submit", "lookup", "protect", "verify_protection"])
def test_any_mutation_ambiguity_quarantines_without_retry(setup, stage):
    c, path, broker, now = setup
    getattr(broker, stage).side_effect = TimeoutError("do not print credential-like exception")
    assert run_sandbox_candidate(c, path, broker, clock=lambda: now) == "QUARANTINED"
    assert run_sandbox_candidate(c, path, broker, clock=lambda: now) == "ALREADY_CLAIMED"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM attempt").fetchone() == ("QUARANTINED",)


def test_only_correlated_pending_remainder_cancelled_on_timeout(setup):
    c, path, broker, now = setup
    pending = replace(broker.submit.return_value, status=OrderStatus.ACCEPTED, filled_lots=0)
    broker.submit.return_value = pending
    broker.cancel_unfilled_verified.return_value = replace(pending, status=OrderStatus.CANCELLED)
    assert (
        run_sandbox_candidate(c, path, broker, clock=lambda: now, sleep=lambda x: None, polls=1)
        == "ENTRY_NOT_FILLED"
    )
    broker.cancel_unfilled_verified.assert_called_once_with(pending)
    broker.protect.assert_not_called()


def test_no_missing_recreation_or_cross_account_execution(setup, tmp_path):
    c, path, broker, now = setup
    with pytest.raises(ValueError):
        run_sandbox_candidate(c, tmp_path / "missing.sqlite3", broker, clock=lambda: now)
    assert not (tmp_path / "missing.sqlite3").exists()
    with pytest.raises(FileExistsError):
        init(path, c.account_id)
    with pytest.raises(ValueError):
        OnceLedger(path, "foreign")
    broker.environment = "prod"
    with pytest.raises(ValueError):
        run_sandbox_candidate(c, path, broker, clock=lambda: now)
    broker.submit.assert_not_called()


def test_atomic_claim_one_winner(setup):
    c, path, _, now = setup
    first, second = OnceLedger(path, c.account_id), OnceLedger(path, c.account_id)
    assert first.status() == "WAITING"
    assert first.claim(c, now)
    assert first.status() == "CLAIMED"
    assert not second.claim(replace(c, order_request_id=str(uuid4())), now)


def test_real_sandbox_transport_same_runner_offline_e2e(case, tmp_path):
    original, broker, transport = case
    candidate = ExampleCandidate("sandbox-only", "a" * 64, **vars(original))
    path = tmp_path / "real-adapter-once.sqlite3"
    init(path, "sandbox-only")
    assert run_sandbox_candidate(candidate, path, broker, clock=broker.clock) == "FILLED_PROTECTED"
    assert run_sandbox_candidate(candidate, path, broker, clock=broker.clock) == "ALREADY_CLAIMED"
    assert [m for m, _ in transport.posts].count("PostSandboxOrder") == 1
    assert [m for m, _ in transport.posts].count("PostSandboxStopOrder") == 1


@pytest.mark.parametrize("fault", ["foreign_intent", "bad_fill", "bad_stop"])
def test_mismatched_execution_quarantines(setup, fault):
    c, path, broker, now = setup
    record = broker.submit.return_value
    if fault == "foreign_intent":
        broker.submit.return_value = replace(record, intent=replace(record.intent, side=Side.SELL))
    elif fault == "bad_fill":
        broker.submit.return_value = replace(record, filled_lots=0)
    else:
        broker.verify_protection.return_value = {"verified": False}
    assert run_sandbox_candidate(c, path, broker, clock=lambda: now) == "QUARANTINED"


def test_another_process_claims_during_preflight(setup):
    c, path, broker, now = setup
    broker.preflight.side_effect = lambda candidate: OnceLedger(path, c.account_id).claim(c, now)
    assert run_sandbox_candidate(c, path, broker, clock=lambda: now) == "ALREADY_CLAIMED"
    broker.submit.assert_not_called()


def test_explicit_init_time_and_unclaimed_update_guards(setup, tmp_path):
    c, path, _, now = setup
    with pytest.raises(ValueError):
        init(tmp_path / "bad.sqlite3", "")
    ledger = OnceLedger(path, c.account_id)
    with pytest.raises(ValueError):
        ledger.claim(c, now.replace(tzinfo=None))
    with pytest.raises(ValueError):
        ledger.record("FILLED_PROTECTED", now)


def test_independent_anchor_blocks_restored_waiting_database(setup):
    c, path, broker, now = setup
    old_database = path.read_bytes()
    assert run_sandbox_candidate(c, path, broker, clock=lambda: now) == "FILLED_PROTECTED"
    path.write_bytes(
        old_database
    )  # Restore pre-trade backup; independent anchor is not rolled back.
    with pytest.raises(ValueError, match="restored WAITING"):
        run_sandbox_candidate(c, path, broker, clock=lambda: now)
    assert broker.submit.call_count == 1


def test_anchor_requires_exact_candidate_and_cannot_be_removed_or_reinitialized(setup):
    c, path, _, now = setup
    ledger = OnceLedger(path, c.account_id)
    assert ledger.claim(c, now)
    original = ledger.anchor.read_text()
    ledger.anchor.write_text("{}")
    with pytest.raises(ValueError, match="mismatch"):
        ledger.status()
    ledger.anchor.write_text(original)
    with pytest.raises(ValueError, match="anchor already exists"):
        init(path.parent / "other.sqlite3", c.account_id, anchor=ledger.anchor)
    ledger.anchor.unlink()
    with pytest.raises(ValueError, match="lacks independent anchor"):
        ledger.status()


def test_explicit_anchor_independent_directory_binding(tmp_path):
    db = tmp_path / "normal-backup" / "once.sqlite3"
    anchor = tmp_path / "outside-backup" / "once.json"
    init(db, "sandbox-only", anchor=anchor)
    assert OnceLedger(db, "sandbox-only", anchor=anchor).status() == "WAITING"
    with pytest.raises(ValueError):
        OnceLedger(db, "sandbox-only")


def test_safe_stop_only_recovery_never_enters_or_reposts(setup):
    c, path, broker, now = setup
    ledger = OnceLedger(path, c.account_id)
    assert ledger.claim(c, now)
    ledger.record("ENTRY_OBSERVED", now, broker.submit.return_value)
    assert recover_sandbox_protection(c, path, broker, clock=lambda: now) == "FILLED_PROTECTED"
    broker.submit.assert_not_called()
    broker.cancel_unfilled_verified.assert_not_called()
    assert broker.protect.call_count == 1
    with pytest.raises(ValueError):
        recover_sandbox_protection(c, path, broker, clock=lambda: now)
    assert broker.protect.call_count == 1


@pytest.mark.parametrize("state", ["CLAIMED", "STOP_PENDING", "QUARANTINED"])
def test_ambiguous_states_never_protection_recover(setup, state):
    c, path, broker, now = setup
    ledger = OnceLedger(path, c.account_id)
    ledger.claim(c, now)
    ledger.record(state, now, broker.submit.return_value)
    with pytest.raises(ValueError):
        recover_sandbox_protection(c, path, broker, clock=lambda: now)
    broker.submit.assert_not_called()
    broker.protect.assert_not_called()


def test_anchor_loss_during_repeated_preflight_blocks_actual_post(case, tmp_path):
    original, broker, transport = case
    candidate = ExampleCandidate("sandbox-only", "a" * 64, **vars(original))
    path = tmp_path / "guarded-once.sqlite3"
    init(path, candidate.account_id)
    ledger = OnceLedger(path, candidate.account_id)
    original_preflight = broker.preflight
    calls = 0

    def preflight(c):
        nonlocal calls
        result = original_preflight(c)
        calls += 1
        if calls == 2:
            ledger.anchor.unlink()
        return result

    broker.preflight = preflight
    assert run_sandbox_candidate(candidate, path, broker, clock=broker.clock) == "QUARANTINED"
    assert "PostSandboxOrder" not in [method for method, _ in transport.posts]


def test_process_lock_prevents_runner_and_recovery_overlap(setup):
    c, path, broker, now = setup
    ledger = OnceLedger(path, c.account_id)
    with ledger.workflow_lock(), pytest.raises(ValueError, match="already running"):
        run_sandbox_candidate(c, path, broker, clock=lambda: now)
    broker.submit.assert_not_called()


def test_stop_dispatch_fence_blocks_restored_pre_child_snapshot(case, tmp_path):
    original, broker, _ = case
    candidate = ExampleCandidate("sandbox-only", "a" * 64, **vars(original))
    path = tmp_path / "stop-restore.sqlite3"
    init(path, candidate.account_id)
    ledger = OnceLedger(path, candidate.account_id)
    ledger.claim(candidate, broker.clock())
    filled = broker.submit(candidate)
    ledger.record("ENTRY_OBSERVED", broker.clock(), filled)
    before_child = path.read_bytes()
    assert (
        recover_sandbox_protection(candidate, path, broker, clock=broker.clock)
        == "FILLED_PROTECTED"
    )
    path.write_bytes(before_child)
    with pytest.raises(ValueError, match="predates independent native stop"):
        recover_sandbox_protection(candidate, path, broker, clock=broker.clock)


def test_readonly_lost_ack_reconcile_and_saved_candidate(case, tmp_path):
    from moex_bot.long_auto_once import load_saved_candidate, reconcile_sandbox_once

    original, broker, transport = case
    candidate = ExampleCandidate("sandbox-only", "a" * 64, **vars(original))
    path = tmp_path / "read-only-reconcile.sqlite3"
    init(path, candidate.account_id)
    ledger = OnceLedger(path, candidate.account_id)
    ledger.claim(candidate, broker.clock())  # Simulate crash after POST before response saved.
    assert (
        load_saved_candidate(path, candidate.account_id).order_request_id
        == candidate.order_request_id
    )
    result = reconcile_sandbox_once(path, candidate.account_id, broker)
    assert result["broker_status"] == "filled" and result["read_only"] is True
    assert ledger.status() == "CLAIMED"
    assert "PostSandboxOrder" not in [method for method, _ in transport.posts]
    assert "PostSandboxStopOrder" not in [method for method, _ in transport.posts]


@pytest.mark.parametrize(
    "fault", ["foreign_account", "wrong_parent", "changed_fill", "bad_stop", "transport"]
)
def test_recovery_negative_branches(setup, fault):
    c, path, broker, now = setup
    ledger = OnceLedger(path, c.account_id)
    ledger.claim(c, now)
    record = broker.submit.return_value
    if fault == "foreign_account":
        broker.account_id = "foreign"
    elif fault == "wrong_parent":
        record = replace(record, intent=replace(record.intent, side=Side.SELL))
    elif fault == "changed_fill":
        broker.lookup.side_effect = lambda rec: replace(rec, filled_lots=0)
    elif fault == "bad_stop":
        broker.verify_protection.return_value = {"verified": False}
    else:
        broker.protect.side_effect = TimeoutError("ambiguous child")
    ledger.record("ENTRY_OBSERVED", now, record)
    if fault in {"foreign_account", "wrong_parent"}:
        with pytest.raises(ValueError):
            recover_sandbox_protection(c, path, broker, clock=lambda: now)
    else:
        assert recover_sandbox_protection(c, path, broker, clock=lambda: now) == "QUARANTINED"
    broker.submit.assert_not_called()


def test_invalid_anchor_fence_symlink_lock_and_empty_guard(setup, tmp_path, monkeypatch):
    c, path, _, now = setup
    with pytest.raises(ValueError):
        init(tmp_path / "new.sqlite3", c.account_id, anchor=tmp_path / "same-dir.json")
    ledger = OnceLedger(path, c.account_id)
    with pytest.raises(ValueError, match="no durable claim"):
        ledger.mutation_guard()
    target = ledger.anchor.with_suffix(ledger.anchor.suffix + ".lock")
    original = type(target).is_symlink
    monkeypatch.setattr(type(target), "is_symlink", lambda self: self == target or original(self))
    with pytest.raises(ValueError, match="symlink"), ledger.workflow_lock():
        pytest.fail("symlink lock entered")
    monkeypatch.undo()
    ledger.claim(c, now)
    fence = ledger.anchor.with_suffix(ledger.anchor.suffix + ".stop-dispatch")
    fence.write_text("{}")
    with pytest.raises(ValueError, match="fence mismatch"):
        ledger.status()


def test_posix_fsync_and_lock_paths_are_exercised_offline(setup, monkeypatch, tmp_path):
    import os
    from types import SimpleNamespace

    from moex_bot import long_auto_once as module

    c, path, _, now = setup
    sync_target = tmp_path / "directory-fsync-surrogate"
    sync_target.write_text("offline directory fsync mock")
    flock = Mock()
    monkeypatch.setattr(
        module,
        "import_module",
        lambda name: SimpleNamespace(flock=flock, LOCK_EX=1, LOCK_NB=2, LOCK_UN=8),
    )
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            name="posix",
            O_RDONLY=os.O_RDONLY,
            open=lambda path, flags: os.open(sync_target, os.O_RDWR),
            fsync=Mock(wraps=os.fsync),
            close=os.close,
        ),
    )
    ledger = OnceLedger(path, c.account_id)
    with ledger.workflow_lock():
        ledger.claim(c, now)
        ledger.record("STOP_PENDING", now)
        ledger.mutation_guard()
    assert module.os.fsync.call_count == 4  # File + parent sync for each independent anchor.
    flock.side_effect = OSError("held by independent process")
    with pytest.raises(ValueError, match="already running"), ledger.workflow_lock():
        pytest.fail("contended POSIX lock entered")


def test_saved_candidate_empty_foreign_and_readonly_with_record(setup):
    import hashlib
    import json

    from moex_bot.long_auto_once import load_saved_candidate, reconcile_sandbox_once

    c, path, broker, now = setup
    with pytest.raises(ValueError, match="no saved candidate"):
        load_saved_candidate(path, c.account_id)
    ledger = OnceLedger(path, c.account_id)
    ledger.claim(c, now)
    ledger.record("ENTRY_OBSERVED", now, broker.submit.return_value)
    assert reconcile_sandbox_once(path, c.account_id, broker)["read_only"]
    broker.environment = "prod"
    with pytest.raises(ValueError):
        reconcile_sandbox_once(path, c.account_id, broker)
    payload = json.dumps(
        __import__("dataclasses").asdict(replace(c, account_id="foreign")),
        sort_keys=True,
        default=str,
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE attempt SET candidate=?", (payload,))
    ledger.anchor.write_text(
        json.dumps(
            {
                "version": 1,
                "account_id": c.account_id,
                "candidate_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            }
        )
    )
    with pytest.raises(ValueError, match="another sandbox account"):
        load_saved_candidate(path, c.account_id)
