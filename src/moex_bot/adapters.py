from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .domain import ExecutionMode, OrderIntent, OrderRecord, OrderStatus


class ExecutionPort(Protocol):
    def submit(self, intent: OrderIntent) -> OrderRecord: ...


class AuditPort(Protocol):
    def write(self, event: dict[str, object]) -> None: ...


class JsonlAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str, sort_keys=True) + "\n")


class DryRunExecutionAdapter:
    """Records validated intents without contacting a broker."""

    def __init__(self, mode: ExecutionMode) -> None:
        if mode is ExecutionMode.LIVE:
            raise ValueError("live execution is not available")
        self.mode = mode
        self.records: list[OrderRecord] = []

    def submit(self, intent: OrderIntent) -> OrderRecord:
        record = OrderRecord(intent=intent, status=OrderStatus.VALIDATED)
        self.records.append(record)
        return record


def order_record_event(record: OrderRecord) -> dict[str, object]:
    return {
        "type": "order_record",
        "status": record.status.value,
        "intent": asdict(record.intent),
        "filled_lots": record.filled_lots,
        "broker_order_id": record.broker_order_id,
    }
