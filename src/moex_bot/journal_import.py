"""Allowlisted local artifact adapters and optional sandbox-only reconciliation collector."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    count, errors = 0, []
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
                    execution = json.loads(execution_path.read_bytes())
                    for i, row in enumerate(execution.get("submitted", [])):
                        events.append(
                            {
                                **events[0],
                                "kind": "order",
                                "identity": execution["run_id"] + ":" + str(i),
                                "symbol": row["intent"]["instrument"]["secid"],
                                "status": row["status"],
                                "filled_lots": row["filled_lots"],
                            }
                        )
                count += journal.ingest(events)
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(f"{profile['id']}: {path.name}: invalid/incomplete artifact")
    return {"inserted_revisions": count, "errors": errors}


def collect(config: dict[str, Any]) -> None:
    # The web server never imports this path or receives broker credentials.
    from .domain import Instrument
    from .integrations.tinvest_sandbox import TInvestSandboxAccountService, UrlLibJsonTransport
    from .service_config import SANDBOX_REST

    class ReadOnlyTransport(UrlLibJsonTransport):
        def post(self, url: str, **kwargs: Any) -> Any:
            allowed = {
                "GetSandboxPortfolio",
                "GetSandboxPositions",
                "GetSandboxOrders",
                "GetSandboxOperationsByCursor",
            }
            if not url.startswith(SANDBOX_REST + "/") or url.rsplit("/", 1)[-1] not in allowed:
                raise ValueError("journal broker writes and production forbidden")
            return super().post(url, **kwargs)

    raw = json.loads(Path(config["universe"]).read_bytes())
    instruments = {
        r["t_invest_uid"]: Instrument(
            r["secid"], r["t_invest_uid"], r["board"], r["lot_size_verified"], number("0.01")
        )
        for r in raw
    }
    service = TInvestSandboxAccountService(
        os.environ["T_INVEST_SANDBOX_TOKEN"], ReadOnlyTransport()
    )
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
        operations = service.operations(
            account, instruments, from_time=coverage_from, to_time=after
        )
        if any(o.occurred_at > before for o in operations):
            raise ValueError("broker state changed during journal snapshot; retry next cycle")
        payload.update(
            observed_at=before.isoformat(),
            journal_snapshot_completed_at=after.isoformat(),
            executed_operations=[asdict(o) for o in operations],
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
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_bytes())
        if args.collect:
            collect(config)
        journal = Journal(Path(config["database"]))
        status = import_artifacts(config, journal)
        report = build_report(journal.latest())
        report["import"] = status
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
