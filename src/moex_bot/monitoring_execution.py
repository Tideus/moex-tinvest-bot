"""Informational execution observations; never override service/incident health."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .domain import OrderRecord, OrderStatus
from .sandbox_execution import (
    SandboxExecutionResult,
    decode_validated_intent,
    expected_intraday_ttl_stop,
)

TTL_NOTE = "INTRADAY SANDBOX: штатная отмена TTL без исполнения; не сбой API."


def intraday_execution_note(state: Path, now: datetime) -> str:
    try:
        paths = list((state / "artifacts").glob("intraday-execution-*.json"))
        if not paths:
            return ""
        latest = max(paths, key=lambda p: p.stat().st_mtime)
        if latest.is_symlink():
            return ""
        raw = json.loads(latest.read_text(encoding="utf-8"))
        run_id = raw["run_id"]
        if not run_id.startswith("intraday-"):
            return ""
        at = datetime.fromisoformat(run_id.removeprefix("intraday-"))
        if not 0 <= (now - at).total_seconds() <= 600:
            return ""
        records = tuple(OrderRecord(
            intent=decode_validated_intent({"status": "validated", "intent": item["intent"]}),
            status=OrderStatus(item["status"]), filled_lots=item["filled_lots"],
            broker_order_id=item.get("broker_order_id"), status_detail=item.get("status_detail"),
        ) for item in raw["submitted"])
        result = SandboxExecutionResult(run_id, records, raw["stopped_reason"])
        return TTL_NOTE if expected_intraday_ttl_stop(result) else ""
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        # Missing/malformed evidence must never downgrade a failed service to OK.
        return ""
