"""Allowlisted local artifact adapters and optional sandbox-only reconciliation collector."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .journal_cards import attach_charts, attach_order_context
from .journal_readmodel import publish
from .trader_journal import Journal, build_report, digest, encoded, number, timestamp


def atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def binding(account: str) -> str:
    return hashlib.sha256(account.encode()).hexdigest()


def portfolio_events(
    raw: dict[str, Any],
    profile: dict[str, Any],
    instruments: dict[str, dict[str, Any]],
    provenance: str,
) -> list[dict[str, Any]]:
    environment = profile["environment"]
    if environment != "sandbox" or raw["source"] != "t_invest_sandbox":
        raise ValueError("initial journal adapter permits sandbox broker data only")
    account = raw.get("account_id")
    account_key = binding(account) if account else raw.get("account_binding")
    if not account_key:
        raise ValueError("portfolio account binding missing")
    at = timestamp(raw["observed_at"]).isoformat()
    common = {
        "stream": profile["id"] + ":" + account_key[:16],
        "strategy": profile["id"],
        "environment": environment,
        "known_at": at,
        "source_sha256": provenance,
    }
    positions: dict[str, Any] = {}
    for symbol, position in raw["positions"].items():
        spec = instruments.get(symbol)
        if spec is None:
            raise ValueError("instrument metadata missing")
        positions[symbol] = {
            "units": str(number(position["lots"]) * number(spec["lot_size_verified"])),
            "value": (
                None
                if position.get("current_value") is None
                else str(number(position["current_value"]))
            ),
        }
    snapshot = {
        **common,
        "kind": "snapshot",
        "identity": at,
        "at": at,
        "equity": str(number(raw["reported_equity"])),
        "positions": positions,
    }
    if "journal_coverage_from" in raw:
        snapshot.update(
            coverage_from=timestamp(raw["journal_coverage_from"]).isoformat(),
            coverage_to=timestamp(raw["journal_coverage_to"]).isoformat(),
        )
    result = [snapshot]
    for op in raw.get("executed_operations", []):
        side, symbol = op["side"], op["secid"]
        if side not in {None, "BUY", "SELL"} or not op["operation_id"]:
            raise ValueError("invalid broker operation identity/side")
        qty, gross = number(op["quantity"]), number(op["gross"])
        commission = None if op.get("commission") is None else number(op["commission"])
        if qty < 0 or gross < 0 or (commission is not None and commission < 0):
            raise ValueError("negative broker quantity/cost")
        result.append(
            {
                **common,
                "kind": "operation",
                "identity": op["operation_id"],
                "at": timestamp(op["occurred_at"]).isoformat(),
                "symbol": symbol,
                "side": side,
                "quantity": str(qty),
                "gross": str(gross),
                "commission": None if commission is None else str(commission),
                "external": str(number(op.get("external_cashflow", "0"))),
                "asset_class": instruments.get(symbol, {}).get("asset_class", "unknown"),
                "granularity": "broker_operation_vwap",
            }
        )
        # Only new journal receipts carry these fields. Keeping old event bytes
        # unchanged preserves append-only revision identities during re-import.
        if "journal_commission_source" in op:
            source = op["journal_commission_source"]
            if source not in {"broker_reported", "broker_missing", "broker_invalid"}:
                raise ValueError("invalid commission provenance")
            result[-1]["commission_source"] = source
        raw_checksum = op.get("journal_raw_operation_sha256")
        if raw_checksum is not None:
            if (not isinstance(raw_checksum, str) or len(raw_checksum) != 64
                    or any(char not in "0123456789abcdef" for char in raw_checksum)):
                raise ValueError("invalid raw operation checksum")
            result[-1]["raw_operation_sha256"] = raw_checksum
        if op.get("journal_price") is not None:
            price = number(op["journal_price"])
            if price <= 0:
                raise ValueError("invalid broker operation price")
            result[-1]["price"] = str(price)
    return result


def plan_events(raw: dict[str, Any], common: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = raw["run_id"]
    at = timestamp(run_id.split("-", 1)[1]).isoformat()
    metadata = {k: common[k] for k in ("stream", "strategy", "environment")}
    metadata.update(at=at, known_at=at, source_sha256=digest(raw))
    results = []
    decisions = raw.get("signal_journal", {}).get("decisions")
    if decisions is None:
        decisions = [
            {
                "secid": r["secid"],
                "status": "filter_passed" if r["passed"] else "filter_rejected",
                "reasons": r["reasons"],
                "candidate": None,
            }
            for r in raw.get("flow_diagnostics", [])
        ]
    input_checksum = digest(raw.get("analysis_input", raw.get("long_flow_input", {})))
    for index, row in enumerate(decisions):
        candidate = row.get("candidate") or {}
        results.append(
            {
                **metadata,
                "kind": "signal",
                "identity": f"{run_id}:{index}",
                "symbol": row["secid"],
                "status": row["status"],
                "reasons": row["reasons"],
                "side": candidate.get("side"),
                "analysis": candidate,
                "input_sha256": input_checksum,
            }
        )
    return results


def import_artifacts(config: dict[str, Any], journal: Journal) -> dict[str, Any]:
    root = Path(config["artifact_root"]).resolve()
    instruments = {r["secid"]: r for r in json.loads(Path(config["universe"]).read_bytes())}
    count, errors, omitted_orders = 0, [], 0
    for profile in config["profiles"]:
        paths: set[Path] = set()
        for pattern in profile["portfolio_globs"]:
            paths.update(root.glob(pattern))
        if config.get("collector_dir"):
            paths.update(Path(config["collector_dir"]).glob(profile["id"] + "-*.json"))
        for path in sorted(paths):
            try:
                raw_bytes = path.read_bytes()
                raw = json.loads(raw_bytes)
                events = portfolio_events(
                    raw, profile, instruments, hashlib.sha256(raw_bytes).hexdigest()
                )
                if path.name == "portfolio.json":
                    plan_path = path.with_name("plan.json")
                else:
                    plan_path = path.with_name(path.name.replace("-portfolio-", "-plan-"))
                if plan_path != path and plan_path.exists():
                    events.extend(plan_events(json.loads(plan_path.read_bytes()), events[0]))
                execution_path = path.with_name(path.name.replace("-portfolio-", "-execution-"))
                if execution_path != path and execution_path.exists():
                    execution_bytes = execution_path.read_bytes()
                    execution = json.loads(execution_bytes)
                    for i, row in enumerate(execution.get("submitted", [])):
                        occurred = row.get("occurred_at") or row.get("submitted_at")
                        if not occurred:
                            omitted_orders += 1
                            continue
                        at = timestamp(occurred).isoformat()
                        receipts = (row.get("known_at"), execution.get("completed_at"),
                                    execution.get("observed_at"))
                        known = next((timestamp(value).isoformat() for value in receipts
                                      if value and timestamp(value) >= timestamp(at)), None)
                        if known is None:
                            omitted_orders += 1
                            continue
                        events.append(
                            {
                                **{k: events[0][k] for k in ("stream", "strategy", "environment")},
                                "kind": "order",
                                "identity": execution["run_id"] + ":" + str(i),
                                "at": at,
                                "known_at": known,
                                "time_status": "known",
                                "source_sha256": hashlib.sha256(execution_bytes).hexdigest(),
                                "symbol": row["intent"]["instrument"]["secid"],
                                "status": row["status"],
                                "filled_lots": row["filled_lots"],
                            }
                        )
                count += journal.ingest(events)
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(f"{profile['id']}: {path.name}: invalid/incomplete artifact")
    return {
        "inserted_revisions": count, "errors": errors,
        "diagnostics": [f"orders: {omitted_orders} omitted: execution/receipt timestamp missing"]
        if omitted_orders else [],
        "unknown_order_times": omitted_orders,
    }


def broker_operation_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Journal-only evidence; hash is canonical decoded item, not HTTP response bytes."""
    def money(value: Any) -> Decimal | None:
        if not isinstance(value, dict) or not {"units", "nano"}.intersection(value):
            return None
        if str(value.get("currency", "rub")).lower() != "rub":
            return None
        try:
            units, nano = number(value.get("units", "0")), number(value.get("nano", 0))
            if units != units.to_integral_value() or nano != nano.to_integral_value():
                return None
            if abs(nano) >= 1_000_000_000:
                return None
            return units + nano / 1_000_000_000
        except ValueError:
            return None

    commission = money(raw.get("commission"))
    price = money(raw.get("price"))
    return {
        "commission": str(abs(commission)) if commission is not None else None,
        "journal_commission_source": "broker_reported" if commission is not None else
        "broker_missing" if raw.get("commission") is None else "broker_invalid",
        "journal_price": str(price) if price is not None and price > 0 else None,
        "journal_raw_operation_sha256": digest(raw) if raw else None,
    }


def collect(config: dict[str, Any]) -> None:
    # The web server never imports this path or receives broker credentials.
    from .domain import Instrument
    from .integrations.tinvest_sandbox import TInvestSandboxAccountService, UrlLibJsonTransport
    from .service_config import SANDBOX_REST

    class ReadOnlyTransport(UrlLibJsonTransport):
        def __init__(self) -> None:
            self.operation_fields: dict[str, dict[str, Any]] = {}

        def post(self, url: str, **kwargs: Any) -> Any:
            allowed = {
                "GetSandboxPortfolio",
                "GetSandboxPositions",
                "GetSandboxOrders",
                "GetSandboxOperationsByCursor",
            }
            if not url.startswith(SANDBOX_REST + "/") or url.rsplit("/", 1)[-1] not in allowed:
                raise ValueError("journal broker writes and production forbidden")
            response = super().post(url, **kwargs)
            if url.rsplit("/", 1)[-1] == "GetSandboxOperationsByCursor":
                for operation in response.get("items", []):
                    if isinstance(operation, dict) and operation.get("id"):
                        # The shared reader also keeps the first occurrence of an ID.
                        self.operation_fields.setdefault(
                            str(operation["id"]), broker_operation_fields(operation)
                        )
            return response

    raw = json.loads(Path(config["universe"]).read_bytes())
    instruments = {
        r["t_invest_uid"]: Instrument(
            r["secid"], r["t_invest_uid"], r["board"], r["lot_size_verified"], number("0.01")
        )
        for r in raw
    }
    transport = ReadOnlyTransport()
    service = TInvestSandboxAccountService(os.environ["T_INVEST_SANDBOX_TOKEN"], transport)
    accounts = [os.environ[p["account_env"]] for p in config["profiles"]]
    if len(set(accounts)) != len(accounts):
        raise ValueError("strategies must use distinct accounts")
    for profile, account in zip(config["profiles"], accounts, strict=True):
        if profile["environment"] != "sandbox":
            raise ValueError("only sandbox collector is enabled")
        before = datetime.now(UTC)
        # A broker snapshot is not atomic. Reject a fill during its acquisition
        # rather than assign a post-fill position to an earlier timestamp.
        payload = service.broker_snapshot(account, instruments).as_portfolio_payload()
        after = datetime.now(UTC)
        coverage_from = timestamp(config["history_from"])
        transport.operation_fields.clear()
        operations = service.operations(
            account, instruments, from_time=coverage_from, to_time=after
        )
        if any(o.occurred_at > before for o in operations):
            raise ValueError("broker state changed during journal snapshot; retry next cycle")
        payload.update(
            observed_at=before.isoformat(),
            journal_snapshot_completed_at=after.isoformat(),
            executed_operations=[
                {**asdict(o), **transport.operation_fields.get(o.operation_id,
                                                             broker_operation_fields({}))}
                for o in operations
            ],
            journal_coverage_from=coverage_from.isoformat(),
            journal_coverage_to=before.isoformat(),
        )
        target = Path(config["collector_dir"]) / (
            profile["id"] + "-" + before.strftime("%Y%m%dT%H%M%SZ") + ".json"
        )
        atomic(target, encoded(payload))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--prices", action="store_true", help="refresh public chart prices only")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_bytes())
        if args.collect:
            collect(config)
        journal = Journal(Path(config["database"]))
        status = import_artifacts(config, journal)
        report = build_report(journal.latest())
        report["import"] = status
        attach_order_context(report, config)
        attach_charts(report, Path(config["output"]).parent / "prices",
                      refresh=args.collect or args.prices)
        publish(
            report,
            Path(
                config.get(
                    "readmodel",
                    str(Path(config["output"]).parent / "readmodel" / "readmodel.sqlite3"),
                )
            ),
        )
        atomic(Path(config["output"]) / "data.json", encoded(report))
        print(
            f"Journal refreshed: streams={len(report['streams'])}, errors={len(status['errors'])}"
        )
        return 2 if status["errors"] else 0
    except Exception as exc:
        # Never print broker payloads, account identifiers, URLs containing secrets.
        print(f"Journal refresh FAILED: {type(exc).__name__}; previous report retained")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
