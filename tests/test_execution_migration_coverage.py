"""Legacy inventory corruption and read-only verification paths."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_execution_migration import ACCOUNTS, _pair, _record

from moex_bot.domain import OrderStatus
from moex_bot.execution_migration import scan_legacy, stage_inventory, verify_inventory


@pytest.mark.parametrize("fault", ["directory", "accounts", "distinct"])
def test_inventory_requires_two_distinct_accounts(tmp_path: Path, fault: str) -> None:
    with pytest.raises(ValueError):
        scan_legacy(
            tmp_path / "missing" if fault == "directory" else tmp_path,
            {} if fault == "accounts" else {"long": "a", "intraday": "a"},
        )


@pytest.mark.parametrize("fault", ["shape", "run", "naive", "empty", "item", "quantity", "filled"])
def test_inventory_validates_every_legacy_artifact(tmp_path: Path, fault: str) -> None:
    path = _pair(tmp_path, _record())
    raw = json.loads(path.read_text())
    if fault == "shape":
        raw = []
    elif fault == "run":
        raw["run_id"] = "intraday-2026-10-30T10:00:00+00:00"
    elif fault == "naive":
        raw["run_id"] = "shadow-2026-10-30T10:00:00"
    elif fault == "empty":
        raw["submitted"] = []
    elif fault == "item":
        raw["submitted"] = [None]
    else:
        raw["submitted"][0]["filled_lots"] = 11 if fault == "quantity" else 5
    path.write_text(json.dumps(raw))
    inventory = scan_legacy(tmp_path, ACCOUNTS)
    assert bool(inventory.issues) == (fault != "empty")


@pytest.mark.parametrize("fault", ["naive", "filled", "confirmed", "unknown"])
def test_verification_records_confirmed_results_without_permission(
    tmp_path: Path, fault: str
) -> None:
    record = replace(_record(), status=OrderStatus.UNKNOWN, filled_lots=0)
    _pair(tmp_path, record)
    inventory = scan_legacy(tmp_path, ACCOUNTS)
    candidate = stage_inventory(inventory, tmp_path / "candidate")

    class Broker:
        account_id = ACCOUNTS["long"]

        def lookup(self, item):
            if fault == "unknown":
                return item
            return replace(
                item, status=OrderStatus.FILLED, filled_lots=5 if fault == "filled" else 10
            )

    now = datetime(2026, 10, 30, 11, tzinfo=UTC)
    if fault == "naive":
        with pytest.raises(ValueError):
            verify_inventory(inventory, lambda a: Broker(), as_of=now.replace(tzinfo=None))
    else:
        result = verify_inventory(inventory, lambda a: Broker(), as_of=now, candidate=candidate)
        assert (
            result[0]["verification"]
            == {"filled": "error", "confirmed": "broker_response", "unknown": "not_confirmed"}[
                fault
            ]
        )


def test_newer_stronger_fill_and_verification_without_candidate(tmp_path: Path) -> None:
    record = _record()
    _pair(tmp_path, replace(record, status=OrderStatus.UNKNOWN, filled_lots=0))
    _pair(tmp_path, record, stamp="20261030T110000Z")
    inventory = scan_legacy(tmp_path, ACCOUNTS)
    assert inventory.duplicates == 1 and inventory.entries[0].record.filled_lots == 10

    class Broker:
        account_id = ACCOUNTS["long"]

        def lookup(self, item):
            return item

    result = verify_inventory(
        inventory, lambda a: Broker(), as_of=datetime(2026, 10, 30, 12, tzinfo=UTC)
    )
    assert result[0]["verification"] == "broker_response"
