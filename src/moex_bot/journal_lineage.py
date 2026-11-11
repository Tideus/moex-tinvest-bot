"""Prospective, journal-only evidence. Never match executions by price/time proximity.

Private receipts keep broker identifiers; the public projection exposes only hashes.
Neither the execution ledger nor old normalized operations are modified.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .trader_journal import digest, encoded, match_trades, number, operation_display, timestamp


def read_db(path: Path, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def load(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 10_000_000:
        raise ValueError("oversized evidence")
    raw = json.loads(path.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError("invalid evidence")
    return raw


def activation(directory: Path, now: datetime) -> datetime:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "activation.json"
    if not path.exists():
        with path.open("x", encoding="utf-8") as out:
            out.write(encoded({"protocol": "execution-lineage-v1", "from": now.isoformat()}))
    value = load(path)
    if value["protocol"] != "execution-lineage-v1":
        raise ValueError("invalid lineage activation")
    return timestamp(value["from"])


def plan_order(plan: dict[str, Any], logical: str) -> dict[str, Any]:
    rows = [r["intent"] for r in plan["orders"]
            if r.get("status") == "validated" and r["intent"]["order_request_id"] == logical]
    if len(rows) != 1:
        raise ValueError("plan order identity missing/ambiguous")
    return dict(rows[0])


def intraday_evidence(row: dict[str, Any], plan: dict[str, Any], account: str) -> dict[str, Any]:
    if (row["account"] != account or row["plan_id"] != plan["run_id"]
            or plan["analysis_input"]["portfolio_input"]["account_id"] != account):
        raise ValueError("intraday account/plan mismatch")
    original = plan_order(plan, row["logical_id"])
    checksum = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    record = json.loads(row["record"])
    actual = dict(record["intent"])
    if actual["order_request_id"] != row["request_id"]:
        raise ValueError("request identity mismatch")
    actual["order_request_id"] = row["logical_id"]
    if actual != original or checksum != row["payload_hash"]:
        raise ValueError("reserved payload mismatch")
    signals = [s for s in plan["signal_journal"]["decisions"]
               if s.get("order_request_id") == row["logical_id"]]
    exits = [s for s in plan["signal_journal"].get("exit_intents", [])
             if s.get("order_request_id") == row["logical_id"]]
    if len(signals) + len(exits) != 1:
        raise ValueError("signal identity missing/ambiguous")
    signal = (signals + exits)[0]
    side = signal.get("side", (signal.get("candidate") or {}).get("side"))
    if signal["secid"] != original["instrument"]["secid"] or side != original["side"]:
        raise ValueError("signal instrument/side mismatch")
    if exits and signal["reason"] != original["rationale"]:
        raise ValueError("exit reason mismatch")
    return {"account": account, "strategy": "intraday", "request": row["request_id"],
            "logical": row["logical_id"], "intent": original, "record": record,
            "plan_sha256": digest(plan), "signal_sha256": digest(signal),
            "decision_at": timestamp(plan["run_id"].split("-", 1)[1]).isoformat()}


def long_evidence(row: dict[str, Any], plan_bytes: bytes, account: str) -> dict[str, Any]:
    candidate, record = json.loads(row["candidate"]), json.loads(row["record"])
    sha = hashlib.sha256(plan_bytes).hexdigest()
    plan = json.loads(plan_bytes)
    if (candidate["account_id"] != account or candidate["decision_sha256"] != sha
            or plan["portfolio_input"]["account_id"] != account):
        raise ValueError("long account/plan mismatch")
    rows = [r["intent"] for r in plan["orders"] if r.get("status") == "validated"
            and str(uuid5(NAMESPACE_URL,
                f"long-once/v1/sandbox/{account}/{sha}/{r['intent']['order_request_id']}"))
            == candidate["order_request_id"]]
    if len(rows) != 1:
        raise ValueError("long candidate identity missing/ambiguous")
    original = rows[0]
    intent = record["intent"]
    if (intent["order_request_id"] != candidate["order_request_id"]
            or intent["instrument"] != candidate["instrument"]
            or original["instrument"] != candidate["instrument"]
            or intent["side"] != "buy" or original["side"] != "buy"
            or number(intent["limit_price"]) != number(candidate["limit_price"])
            or number(original["limit_price"]) != number(candidate["limit_price"])
            or intent["lots"] != candidate["lots"] or candidate["lots"] != 1):
        raise ValueError("long selected order mismatch")
    # This is the persisted strategy plan, not a re-evaluation with today's parameters.
    return {"account": account, "strategy": "long", "request": candidate["order_request_id"],
            "logical": original["order_request_id"], "intent": {**intent,
                "rationale": original["rationale"]}, "record": record,
            "plan_sha256": sha, "signal_sha256": digest(original),
            "decision_at": timestamp(plan["run_id"].split("-", 1)[1]).isoformat()}


def candidates(root: Path, strategy: str, account: str, since: datetime) -> list[dict[str, Any]]:
    plans: dict[str, Path] = {}
    pattern = ("artifacts/intraday-plan-*.json" if strategy == "intraday"
               else "long-once/run-*/plan.json")
    for path in root.glob(pattern):
        if path.stat().st_mtime < since.timestamp():
            continue
        plan = load(path)
        if timestamp(plan["run_id"].split("-", 1)[1]) >= since:
            plans[plan["run_id"]] = path
    if not plans:
        return []
    results = []
    if strategy == "intraday":
        path = root / "execution/orders.sqlite3"
        if not path.exists():
            return []
        rows = read_db(path, "SELECT * FROM actions WHERE account=?", (account,))
        for row in rows:
            if row["plan_id"] in plans:
                results.append(intraday_evidence(row, load(plans[row["plan_id"]]), account))
    elif strategy == "long":
        path = root / "long-once/sandbox.sqlite3"
        if not path.exists():
            return []
        meta = read_db(path, "SELECT value FROM metadata WHERE key='account_id'")
        if len(meta) != 1 or meta[0]["value"] != account:
            raise ValueError("long ledger account mismatch")
        rows = read_db(path, "SELECT * FROM attempt WHERE record IS NOT NULL")
        for row in rows:
            sha = json.loads(row["candidate"])["decision_sha256"]
            for plan_path in plans.values():
                raw = plan_path.read_bytes()
                if hashlib.sha256(raw).hexdigest() == sha:
                    results.append(long_evidence(row, raw, account))
    return results


def verify_state(evidence: dict[str, Any], state: dict[str, Any], now: datetime) -> None:
    intent = evidence["intent"]
    record = evidence["record"]
    if (state.get("orderRequestId") != evidence["request"]
            or state.get("orderId") != record.get("broker_order_id")
            or not state.get("orderId")
            or state.get("instrumentUid") != intent["instrument"]["uid"]
            or state.get("direction") != "ORDER_DIRECTION_" + intent["side"].upper()
            or number(state["lotsRequested"]) != number(intent["lots"])
            or not timestamp(evidence["decision_at"]) <= timestamp(state["orderDate"]) <= now):
        raise ValueError("broker order identity mismatch")
    # Known sandbox bug: lotsExecuted sometimes contains shares. Never silently divide.
    filled = number(state["lotsExecuted"])
    if filled < 0 or filled > number(intent["lots"]) or filled != filled.to_integral_value():
        raise ValueError("broker filled quantity inconsistent; linkage withheld")
    if filled < number(record["filled_lots"]):
        raise ValueError("broker execution regressed")
    status = state.get("executionReportStatus")
    if status not in {"EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                      "EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_REJECTED",
                      "EXECUTION_REPORT_STATUS_CANCELLED"}:
        raise ValueError("unknown broker execution status")
    if ((status == "EXECUTION_REPORT_STATUS_FILL" and filled != number(intent["lots"]))
            or (status in {"EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_REJECTED"}
                and filled != 0)):
        raise ValueError("broker execution status/quantity mismatch")


def collect_lineage(
    config: dict[str, Any], account: str, strategy: str,
    lookup: Callable[[str, str], dict[str, Any]], now: datetime,
) -> None:
    directory = Path(config["output"]).parent / "lineage"
    since = activation(directory, now)
    records = directory / "receipts"
    records.mkdir(mode=0o700, exist_ok=True)
    status: dict[str, Any] = {"from": since.isoformat(), "observed_at": now.isoformat(),
                              "verified": 0, "deferred": 0, "errors": []}
    try:
        items = candidates(Path(config["artifact_root"]), strategy, account, since)
    except (KeyError, ValueError, OSError, TypeError, sqlite3.Error) as exc:
        items = []
        status["errors"].append("plan_or_ledger:" + type(exc).__name__)
    budget = 8
    deadline = time.monotonic() + 15
    for evidence in sorted(items, key=lambda e: e["decision_at"], reverse=True):
        if timestamp(evidence["decision_at"]) < since:
            continue
        key = digest([account, evidence["request"]])
        previous = [load(p) for p in records.glob(key + "-*.json")]
        if any(p["state"].get("executionReportStatus") in {
            "EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_CANCELLED",
            "EXECUTION_REPORT_STATUS_REJECTED"} for p in previous):
            status["verified"] += 1
            continue
        if not evidence["record"].get("broker_order_id"):
            status["deferred"] += 1
            continue
        if budget == 0 or time.monotonic() >= deadline:
            status["deferred"] += 1
            continue
        budget -= 1
        try:
            if now - timestamp(evidence["decision_at"]) > timedelta(days=7):
                raise ValueError("broker retention exceeded")
            state = lookup(account, evidence["request"])
            verify_state(evidence, state, now)
            receipt = {"evidence": evidence, "state": state, "observed_at": now.isoformat()}
            path = records / (key + "-" + digest(receipt) + ".json")
            with path.open("x", encoding="utf-8") as out:
                out.write(encoded(receipt))
            status["verified"] += 1
        except Exception as exc:
            status["errors"].append("broker_order_state:" + type(exc).__name__)
    temporary = directory / (strategy + ".tmp")
    temporary.write_text(encoded(status), encoding="utf-8")
    temporary.replace(directory / (strategy + "-status.json"))


def public_reason(receipt: dict[str, Any]) -> dict[str, Any]:
    e = receipt["evidence"]
    return {"status": "verified_order", "reason": e["intent"]["rationale"],
            "decision_at": e["decision_at"], "verified_at": receipt["observed_at"],
            "plan_ref": e["plan_sha256"][:16], "signal_ref": e["signal_sha256"][:16],
            "request_ref": digest([e["account"], e["request"]])[:16],
            "broker_ref": digest([e["account"], receipt["state"]["orderId"]])[:16]}


def exact_operation(receipt: dict[str, Any], operation: dict[str, Any]) -> bool:
    """Only explicit shared trade IDs, plus full quantities. No timestamp fallback.

    Operation IDs and parentOperationId are NOT order IDs in T-Invest.
    Missing or different trade namespaces deliberately remain unlinked.
    """
    raw = operation.get("journal_trade_identity") or {}
    e, state = receipt["evidence"], receipt["state"]
    intent = e["intent"]
    if (raw.get("instrument_uid") != intent["instrument"]["uid"]
            or operation["side"] != intent["side"].upper()
            or operation["secid"] != intent["instrument"]["secid"]):
        return False
    stage_rows = state.get("stages", [])
    stages = {str(s["tradeId"]): s for s in stage_rows if s.get("tradeId")}
    if len(stages) != len(stage_rows):
        return False
    trades = raw.get("trades", [])
    if not trades or len({t["id"] for t in trades}) != len(trades):
        return False
    qty = number(0)
    for trade in trades:
        stage = stages.get(trade["id"])
        if stage is None:
            return False
        units = number(stage["quantity"]) * number(intent["instrument"]["lot_size"])
        if number(trade["quantity"]) != units or units <= 0:
            return False
        def money(value: dict[str, Any]) -> Any:
            return number(value["units"]) + number(value["nano"]) / 1_000_000_000
        if (money(trade["price"]) != money(stage["price"])
                or timestamp(trade["at"]) != timestamp(stage["executionTime"])):
            return False
        qty += units
    return qty == number(operation["quantity"])


def attach_lineage(report: dict[str, Any], events: list[dict[str, Any]],
                   config: dict[str, Any]) -> None:
    directory = Path(config["output"]).parent / "lineage"
    if not (directory / "activation.json").exists():
        return
    since = timestamp(load(directory / "activation.json")["from"])
    receipts: dict[str, dict[str, Any]] = {}
    for path in (directory / "receipts").glob("*.json"):
        receipt = load(path)
        e = receipt["evidence"]
        if timestamp(e["decision_at"]) < since:
            continue
        verify_state(e, receipt["state"], timestamp(receipt["observed_at"]))
        key = digest([e["account"], e["request"]])
        if key not in receipts or receipt["observed_at"] > receipts[key]["observed_at"]:
            receipts[key] = receipt
    for stream in report["streams"]:
        selected = [r for r in receipts.values() if r["evidence"]["strategy"] + ":"
                    + hashlib.sha256(r["evidence"]["account"].encode()).hexdigest()[:16]
                    == stream["id"]]
        status_path = directory / (stream["strategy"] + "-status.json")
        stream["lineage_status"] = load(status_path) if status_path.exists() else {
            "from": since.isoformat(), "errors": [], "verified": 0, "deferred": 0}
        stream["verified_orders"] = []
        for receipt in selected:
            state, e = receipt["state"], receipt["evidence"]
            if number(state["lotsExecuted"]) <= 0:
                continue
            stream["verified_orders"].append({"kind": "broker_order",
                "ref": digest([stream["id"], "broker_order", e["request"]]),
                "symbol": e["intent"]["instrument"]["secid"],
                "at": state["orderDate"], "verified_at": receipt["observed_at"],
                "side": e["intent"]["side"].upper(),
                "filled_lots": str(number(state["lotsExecuted"])),
                "quantity": str(number(state["lotsExecuted"])
                                * number(e["intent"]["instrument"]["lot_size"])),
                "operation_reason": public_reason(receipt), "markers": []})
        # Match only new receipts; the old append-only event bytes and FIFO rows stay unchanged.
        linked: dict[str, dict[str, dict[str, Any]]] = {}
        for path in Path(config["collector_dir"]).glob(stream["strategy"] + "-*.json"):
            if not selected or path.stat().st_mtime < since.timestamp():
                continue
            raw = load(path)
            if (hashlib.sha256(str(raw.get("account_id", "")).encode()).hexdigest()[:16]
                    != stream["id"].split(":", 1)[1]):
                continue
            for op in raw.get("executed_operations", []):
                if timestamp(op["occurred_at"]) < since:
                    continue
                matches = [r for r in selected if exact_operation(r, op)]
                if len(matches) == 1:
                    proof = {**public_reason(matches[0]), "status": "verified_execution"}
                    linked.setdefault(op["operation_id"], {})[
                        op.get("journal_raw_operation_sha256", "") + ":" + proof["request_ref"]
                    ] = {"proof": proof, "raw_sha": op.get("journal_raw_operation_sha256")}
        rows = [r for r in events if r["stream"] == stream["id"]]
        ops = sorted((r for r in rows if r["kind"] == "operation"),
                     key=lambda r: timestamp(r["at"]))
        confirmed: dict[str, dict[str, Any]] = {}
        for op in ops:
            proofs = list(linked.get(op["identity"], {}).values())
            if (len(proofs) == 1 and proofs[0]["raw_sha"]
                    and proofs[0]["raw_sha"] == op.get("raw_operation_sha256")):
                confirmed[op["identity"]] = proofs[0]["proof"]
        display_keys = [digest(operation_display(o)) for o in ops]
        stream["operation_links"] = {digest(operation_display(o)): confirmed[o["identity"]]
            for o in ops if o["identity"] in confirmed
            and display_keys.count(digest(operation_display(o))) == 1}
        snapshots = sorted((r for r in rows if r["kind"] == "snapshot"),
                           key=lambda r: timestamp(r["at"]))
        pairs: list[tuple[str, str]] = []
        trades, _ = match_trades(ops, snapshots[0] if snapshots else None, lineage_pairs=pairs)
        stream["trade_links"] = {}
        if trades == stream["trades"]:
            trade_keys = [digest(t) for t in trades]
            for trade, (entry, exit_id) in zip(trades, pairs, strict=True):
                if trade_keys.count(digest(trade)) != 1:
                    continue
                stream["trade_links"][digest(trade)] = {"entry": confirmed.get(entry),
                                                        "exit": confirmed.get(exit_id)}
