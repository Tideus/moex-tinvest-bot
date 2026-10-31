from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.cli import history_reset_safety
from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.execution_journal import ExecutionJournal


def intent() -> OrderIntent:
    return OrderIntent(
        "logical",
        Instrument("AAA", "uid", "TQBR", 1, Decimal(".01")),
        Side.SELL,
        10,
        Decimal("100"),
        Decimal("1000"),
        "intraday mandatory flat",
    )


def test_reservation_survives_process_restart_and_is_account_scoped(tmp_path: Path) -> None:
    path = tmp_path / "orders.sqlite3"
    journal = ExecutionJournal(path)
    request_id = journal.reserve("a", intent(), "p1", datetime.now(UTC), residual_exit=True)
    restored = ExecutionJournal(path)
    assert restored.unresolved("a")[0]["intent"]["order_request_id"] == request_id
    with pytest.raises(ValueError, match="reconciliation"):
        restored.reserve("a", intent(), "p2", datetime.now(UTC), residual_exit=True)
    assert restored.reserve("b", intent(), "p1", datetime.now(UTC), residual_exit=True)


def test_partial_cancel_requires_fresh_snapshot_and_new_uuid(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "orders.sqlite3")
    snapshot = datetime.now(UTC)
    original = intent()
    request_id = journal.reserve("a", original, "p1", snapshot, residual_exit=True)
    sent = replace(original, order_request_id=str(request_id))
    journal.record("a", OrderRecord(sent, OrderStatus.PARTIALLY_FILLED, 4))
    with pytest.raises(ValueError, match="reconciliation"):
        journal.reserve("a", original, "p2", datetime.now(UTC), residual_exit=True)
    journal.record("a", OrderRecord(sent, OrderStatus.CANCELLED, 4))
    assert journal.reserve("a", original, "p1", snapshot, residual_exit=True) is None
    remaining = replace(original, lots=6, notional=Decimal("600"))
    with pytest.raises(ValueError, match="snapshot"):
        journal.reserve("a", remaining, "p2", snapshot, residual_exit=True)
    new_id = journal.reserve("a", remaining, "p2", datetime.now(UTC), residual_exit=True)
    assert new_id and new_id != request_id


def test_payload_is_immutable_and_new_plan_needs_new_snapshot(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "orders.sqlite3")
    snapshot = datetime.now(UTC)
    original = intent()
    request_id = journal.reserve("a", original, "p1", snapshot, residual_exit=False)
    sent = replace(original, order_request_id=str(request_id))
    with pytest.raises(ValueError, match="payload"):
        journal.record("a", OrderRecord(replace(sent, lots=9), OrderStatus.CANCELLED))
    journal.record("a", OrderRecord(sent, OrderStatus.FILLED, 10))
    with pytest.raises(ValueError, match="payload"):
        journal.reserve("a", replace(original, lots=9), "p1", snapshot, residual_exit=False)
    with pytest.raises(ValueError, match="snapshot"):
        journal.reserve(
            "a",
            replace(original, order_request_id="different"),
            "p2",
            snapshot,
            residual_exit=False,
        )
    with pytest.raises(ValueError, match="terminal"):
        journal.record("a", OrderRecord(sent, OrderStatus.UNKNOWN, 10))


def test_concurrent_workers_cannot_both_reserve_account(tmp_path: Path) -> None:
    path = tmp_path / "orders.sqlite3"
    ExecutionJournal(path)

    def reserve(worker: int) -> bool:
        try:
            return bool(
                ExecutionJournal(path).reserve(
                    "a",
                    replace(intent(), order_request_id=str(worker)),
                    "p1",
                    datetime.now(UTC),
                    residual_exit=False,
                )
            )
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(workers.map(reserve, (1, 2))) == 1


@pytest.mark.parametrize("filled", [-1, 11, 0, 9])
def test_invalid_filled_result_does_not_unlock_account(tmp_path: Path, filled: int) -> None:
    journal = ExecutionJournal(tmp_path / "orders.sqlite3")
    request_id = journal.reserve("a", intent(), "p1", datetime.now(UTC), residual_exit=False)
    with pytest.raises(ValueError, match="quantity"):
        journal.record(
            "a",
            OrderRecord(
                replace(intent(), order_request_id=str(request_id)), OrderStatus.FILLED, filled
            ),
        )
    assert len(journal.unresolved("a")) == 1


def test_naive_snapshot_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        ExecutionJournal(tmp_path / "orders.sqlite3").reserve(
            "a", intent(), "p1", datetime.now(UTC).replace(tzinfo=None), residual_exit=False
        )


@pytest.mark.parametrize("content", [b"", b"not a database"])
def test_existing_empty_or_corrupt_file_is_not_initialized(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "orders.sqlite3"
    path.write_bytes(content)
    assert not ExecutionJournal.is_initialized(path)
    with pytest.raises(ValueError, match="restore/reconcile"):
        ExecutionJournal(path)


def test_history_reset_refuses_unknown_even_when_get_orders_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "execution" / "orders.sqlite3"
    journal = ExecutionJournal(path)
    journal.reserve("a", intent(), "p1", datetime.now(UTC), residual_exit=False)
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"t_invest_environment":"sandbox"}')
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"positions":{},"open_orders":0}')
    assert history_reset_safety(runtime_path=runtime, long_snapshot=snapshot,
                                intraday_snapshot=snapshot, journal_path=path) == 2
    assert journal.unresolved_count() == 1
