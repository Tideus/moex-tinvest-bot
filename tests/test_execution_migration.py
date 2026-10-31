import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.execution_migration import (
    inventory_report,
    scan_legacy,
    stage_inventory,
    verify_inventory,
)
from moex_bot.sandbox_execution import reconcile_execution_journal

ACCOUNTS = {"long": "long-account", "intraday": "intraday-account"}


def _record() -> OrderRecord:
    return OrderRecord(
        OrderIntent(
            str(uuid4()),
            Instrument("AAA", "uid", "TQBR", 1, Decimal(".01")),
            Side.BUY,
            10,
            Decimal("100"),
            Decimal("1000"),
            "test",
        ),
        OrderStatus.FILLED,
        10,
        "exchange-id",
    )


def _pair(
    artifacts: Path,
    record: OrderRecord,
    *,
    stamp: str = "20261030T100000Z",
    profile: str = "long",
    account: str | None = None,
) -> Path:
    artifacts.mkdir(parents=True, exist_ok=True)
    prefix, portfolio, run = (
        ("sandbox-execution-", "portfolio-", "shadow-")
        if profile == "long"
        else ("intraday-execution-", "intraday-portfolio-", "intraday-")
    )
    path = artifacts / f"{prefix}{stamp}.json"
    path.write_text(
        json.dumps(
            {"run_id": f"{run}2026-10-30T10:00:00+00:00", "submitted": [asdict(record)]},
            default=str,
        )
    )
    (artifacts / f"{portfolio}{stamp}.json").write_text(
        json.dumps({"source": "t_invest_sandbox", "account_id": account or ACCOUNTS[profile]})
    )
    return path


def test_candidate_keeps_original_uuid_and_cannot_submit_even_after_broker_confirmation(
    tmp_path: Path,
) -> None:
    record = _record()
    artifacts = tmp_path / "state" / "artifacts"
    _pair(artifacts, record)
    inventory = scan_legacy(artifacts, ACCOUNTS)
    assert not inventory.issues
    assert inventory_report(inventory)["trading_permission"] is False
    path = tmp_path / "candidate" / "orders.sqlite3"
    journal = stage_inventory(inventory, path)
    assert journal.unresolved_count() == 1  # Local FILLED is not broker confirmation.
    assert journal.unresolved(ACCOUNTS["long"])[0]["intent"]["order_request_id"] == (
        record.intent.order_request_id
    )
    stage_inventory(inventory, path)  # Restart is idempotent.
    assert journal.unresolved_count() == 1

    class ReadOnlyBroker:
        account_id = ACCOUNTS["long"]

        def lookup(self, item: OrderRecord) -> OrderRecord:
            return replace(item, status=OrderStatus.FILLED, filled_lots=10)

        def submit(self, intent: OrderIntent) -> OrderRecord:
            raise AssertionError("migration must never send orders")

    assert reconcile_execution_journal(path, ReadOnlyBroker()) == 0
    with pytest.raises(ValueError, match="independent review"):
        journal.reserve(
            ACCOUNTS["long"], record.intent, "new-plan", datetime.now(UTC), residual_exit=False
        )


def test_missing_broker_state_keeps_candidate_unresolved(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _pair(artifacts, _record())
    path = tmp_path / "candidate.sqlite3"
    stage_inventory(scan_legacy(artifacts, ACCOUNTS), path)

    class MissingBroker:
        account_id = ACCOUNTS["long"]

        def lookup(self, item: OrderRecord) -> OrderRecord:
            return replace(item, status=OrderStatus.UNKNOWN, status_detail="HTTPError:HTTP 404")

        def submit(self, intent: OrderIntent) -> OrderRecord:
            raise AssertionError("must never submit")

    assert reconcile_execution_journal(path, MissingBroker()) == 1


@pytest.mark.parametrize("failure", ["account", "missing_portfolio", "payload_reuse", "malformed"])
def test_invalid_inventory_cannot_create_candidate(tmp_path: Path, failure: str) -> None:
    artifacts = tmp_path / "state" / "artifacts"
    record = _record()
    path = _pair(artifacts, record, account="wrong" if failure == "account" else None)
    if failure == "missing_portfolio":
        (artifacts / "portfolio-20261030T100000Z.json").unlink()
    if failure == "payload_reuse":
        _pair(
            artifacts,
            replace(record, intent=replace(record.intent, lots=11)),
            stamp="20261030T110000Z",
        )
    if failure == "malformed":
        path.write_text("{")
    inventory = scan_legacy(artifacts, ACCOUNTS)
    assert inventory.issues
    candidate = tmp_path / "candidate.sqlite3"
    with pytest.raises(ValueError, match="blocking issues"):
        stage_inventory(inventory, candidate)
    assert not candidate.exists()


def test_duplicates_preserve_strongest_fill_floor_and_manifest_changes_are_rejected(
    tmp_path: Path,
) -> None:
    record = _record()
    artifacts = tmp_path / "artifacts"
    _pair(artifacts, record)
    _pair(
        artifacts,
        replace(record, status=OrderStatus.UNKNOWN, filled_lots=0),
        stamp="20261030T110000Z",
    )
    inventory = scan_legacy(artifacts, ACCOUNTS)
    assert inventory.duplicates == 1 and len(inventory.entries) == 1
    assert inventory.entries[0].record.filled_lots == 10
    path = tmp_path / "candidate.sqlite3"
    stage_inventory(inventory, path)
    _pair(artifacts, _record(), stamp="20261030T120000Z")
    with pytest.raises(ValueError, match="manifest changed"):
        stage_inventory(scan_legacy(artifacts, ACCOUNTS), path)


def test_profiles_cannot_be_mixed_on_same_uuid(tmp_path: Path) -> None:
    record = _record()
    artifacts = tmp_path / "artifacts"
    _pair(artifacts, record)
    _pair(artifacts, record, profile="intraday")
    assert scan_legacy(artifacts, ACCOUNTS).issues


def test_old_sandbox_requests_are_not_assumed_terminal(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _pair(artifacts, _record())
    inventory = scan_legacy(artifacts, ACCOUNTS)

    def forbidden(account: str) -> object:
        raise AssertionError("old requests must be explicitly unverified, not queried or cleared")

    result = verify_inventory(inventory, forbidden,  # type: ignore[arg-type]
                              as_of=datetime(2026, 11, 10, tzinfo=UTC))
    assert result[0]["verification"] == "not_queried_outside_retention"


@pytest.mark.parametrize("failure", ["wrong_account", "changed_payload", "fill_conflict"])
def test_broker_verification_refuses_conflicts(tmp_path: Path, failure: str) -> None:
    artifacts = tmp_path / "artifacts"
    _pair(artifacts, _record())
    inventory = scan_legacy(artifacts, ACCOUNTS)

    class WrongBroker:
        account_id = "wrong" if failure == "wrong_account" else ACCOUNTS["long"]

        def lookup(self, item: OrderRecord) -> OrderRecord:
            if failure == "changed_payload":
                return replace(item, intent=replace(item.intent, limit_price=Decimal("101")))
            return replace(item, filled_lots=5)

    result = verify_inventory(inventory, lambda account: WrongBroker(),
                              as_of=datetime(2026, 10, 30, 11, tzinfo=UTC))
    assert result[0]["verification"] == "error"


def test_cli_dry_run_reports_errors_without_creating_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moex_bot.cli import sandbox_journal_prepare

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", ACCOUNTS["long"])
    monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", ACCOUNTS["intraday"])
    artifacts = tmp_path / "state" / "artifacts"
    _pair(artifacts, _record(), account="different-account")
    candidate = tmp_path / "review" / "orders.sqlite3"
    report = tmp_path / "review" / "inventory.json"
    assert sandbox_journal_prepare(
        accounts_path=root / "config" / "accounts.json", artifacts_path=artifacts,
        report_path=report, candidate_path=candidate, verify_broker=False) == 2
    assert not candidate.exists()
    assert json.loads(report.read_text())["issues"]


def test_cli_refuses_to_overwrite_source_report_or_candidate_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moex_bot.cli import sandbox_journal_prepare

    root = Path(__file__).resolve().parents[1]
    artifacts = tmp_path / "state" / "artifacts"
    source = _pair(artifacts, _record())
    before = source.read_bytes()
    assert sandbox_journal_prepare(
        accounts_path=root / "config" / "accounts.json", artifacts_path=artifacts,
        report_path=source, candidate_path=None, verify_broker=False) == 2
    assert source.read_bytes() == before
