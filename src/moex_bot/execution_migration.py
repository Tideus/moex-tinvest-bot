"""Offline legacy inventory and quarantined imports. No order submission or cancellation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .domain import OrderRecord, OrderStatus
from .execution_journal import ExecutionJournal
from .sandbox_execution import decode_validated_intent


class OrderLookup(Protocol):
    account_id: str

    def lookup(self, record: OrderRecord) -> OrderRecord: ...


@dataclass(frozen=True)
class LegacyEntry:
    profile: str
    account: str
    plan_id: str
    snapshot_at: datetime
    record: OrderRecord
    artifact: str


@dataclass(frozen=True)
class LegacyInventory:
    entries: tuple[LegacyEntry, ...]
    files: int
    duplicates: int
    issues: tuple[str, ...]
    manifest: str


def scan_legacy(artifacts: Path, accounts: dict[str, str]) -> LegacyInventory:
    if not artifacts.is_dir():
        raise ValueError("legacy artifacts directory does not exist")
    if set(accounts) != {"long", "intraday"} or any(not value for value in accounts.values()):
        raise ValueError("both account identities are required for migration")
    if len(set(accounts.values())) != 2:
        raise ValueError("sandbox account profiles must be distinct")
    entries: dict[str, LegacyEntry] = {}
    issues: list[str] = []
    manifest_rows: list[tuple[str, str]] = []
    files = duplicates = 0
    for profile, prefix, portfolio_prefix, run_prefix in (
        ("long", "sandbox-execution-", "portfolio-", "shadow-"),
        ("intraday", "intraday-execution-", "intraday-portfolio-", "intraday-"),
    ):
        for path in sorted(artifacts.glob(f"{prefix}*.json")):
            files += 1
            try:
                raw = path.read_bytes()
                manifest_rows.append((path.name, hashlib.sha256(raw).hexdigest()))
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(payload.get("submitted"), list):
                    raise ValueError("execution artifact must contain a submitted list")
                plan_id = payload.get("run_id")
                if not isinstance(plan_id, str) or not plan_id.startswith(run_prefix):
                    raise ValueError("execution profile/run_id mismatch")
                run_at = datetime.fromisoformat(plan_id.removeprefix(run_prefix))
                if run_at.tzinfo is None:
                    raise ValueError("execution run_id has no timezone")
                if not payload["submitted"]:
                    continue
                portfolio_path = path.with_name(portfolio_prefix + path.name.removeprefix(prefix))
                portfolio_raw = portfolio_path.read_bytes()
                manifest_rows.append(
                    (portfolio_path.name, hashlib.sha256(portfolio_raw).hexdigest())
                )
                portfolio = json.loads(portfolio_raw)
                if (
                    not isinstance(portfolio, dict)
                    or portfolio.get("source") != "t_invest_sandbox"
                    or portfolio.get("account_id") != accounts[profile]
                ):
                    raise ValueError("paired portfolio account/source mismatch")
                for item in payload["submitted"]:
                    if not isinstance(item, dict):
                        raise ValueError("execution record must be an object")
                    intent = decode_validated_intent(
                        {"status": "validated", "intent": item["intent"]}
                    )
                    UUID(intent.order_request_id)
                    record = OrderRecord(
                        intent,
                        OrderStatus(item["status"]),
                        int(item["filled_lots"]),
                        str(item["broker_order_id"]) if item.get("broker_order_id") else None,
                        str(item["status_detail"]) if item.get("status_detail") else None,
                    )
                    if not 0 <= record.filled_lots <= intent.lots:
                        raise ValueError("invalid legacy filled quantity")
                    if record.status is OrderStatus.FILLED and record.filled_lots != intent.lots:
                        raise ValueError("legacy FILLED has inconsistent quantity")
                    entry = LegacyEntry(
                        profile, accounts[profile], plan_id, run_at, record, path.name
                    )
                    previous = entries.get(intent.order_request_id)
                    if previous is not None:
                        if previous.account != entry.account or previous.record.intent != intent:
                            raise ValueError("request UUID reused with a different account/payload")
                        duplicates += 1
                        # Retain strongest observed fill floor; broker must confirm it.
                        if previous.record.filled_lots > record.filled_lots:
                            entry = replace(entry, record=previous.record)
                    entries[intent.order_request_id] = entry
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append(f"{path.name}: {exc}")
    manifest = hashlib.sha256(
        json.dumps({"files": sorted(manifest_rows), "accounts": accounts}, sort_keys=True).encode()
    ).hexdigest()
    return LegacyInventory(tuple(entries.values()), files, duplicates, tuple(issues), manifest)


def stage_inventory(inventory: LegacyInventory, journal_path: Path) -> ExecutionJournal:
    if inventory.issues:
        raise ValueError("legacy inventory has blocking issues; candidate not created")
    journal = ExecutionJournal(journal_path, migration_required=True)
    journal.stage_legacy(
        tuple(
            (entry.account, entry.plan_id, entry.snapshot_at, entry.record)
            for entry in inventory.entries
        ),
        inventory.manifest,
    )
    return journal


def inventory_report(inventory: LegacyInventory) -> dict[str, object]:
    return {
        "status": "BLOCKED" if inventory.issues else "REVIEW_REQUIRED",
        "manifest": inventory.manifest,
        "execution_files": inventory.files,
        "unique_requests": len(inventory.entries),
        "duplicates": inventory.duplicates,
        "issues": list(inventory.issues),
        "trading_permission": False,
        "requests": [
            {
                "profile": e.profile,
                "request_id": e.record.intent.order_request_id,
                "secid": e.record.intent.instrument.secid,
                "lots": e.record.intent.lots,
                "local_status": e.record.status.value,
                "local_filled_lots": e.record.filled_lots,
                "artifact": e.artifact,
            }
            for e in inventory.entries
        ],
    }


def verify_inventory(
    inventory: LegacyInventory,
    factory: Callable[[str], OrderLookup],
    *,
    as_of: datetime,
    candidate: ExecutionJournal | None = None,
) -> list[dict[str, object]]:
    """Check recent requests only. Old sandbox history is not guaranteed by the broker."""
    if as_of.tzinfo is None:
        raise ValueError("verification requires timezone-aware as_of")
    results: list[dict[str, object]] = []
    for entry in inventory.entries:
        row: dict[str, object] = {
            "request_id": entry.record.intent.order_request_id,
            "profile": entry.profile,
            "secid": entry.record.intent.instrument.secid,
        }
        if not timedelta(0) <= as_of - entry.snapshot_at <= timedelta(days=7):
            row["verification"] = "not_queried_outside_retention"
            results.append(row)
            continue
        try:
            broker = factory(entry.account)
            if broker.account_id != entry.account:
                raise ValueError("broker verification account mismatch")
            result = broker.lookup(entry.record)
            if result.intent != entry.record.intent:
                raise ValueError("broker verification changed request payload")
            if not entry.record.filled_lots <= result.filled_lots <= result.intent.lots:
                raise ValueError("broker fill count conflicts with legacy evidence")
            if result.status is OrderStatus.FILLED and result.filled_lots != result.intent.lots:
                raise ValueError("broker FILLED quantity is incomplete")
            terminal = result.status in {OrderStatus.FILLED, OrderStatus.CANCELLED,
                                         OrderStatus.REJECTED}
            row.update(
                verification="broker_response" if terminal else "not_confirmed",
                broker_status=result.status.value,
                broker_filled_lots=result.filled_lots,
                detail=result.status_detail,
            )
            if candidate is not None:
                candidate.record(entry.account, result)
        except (OSError, ValueError, TypeError) as exc:
            row.update(verification="error", detail=str(exc))
        results.append(row)
    return results
