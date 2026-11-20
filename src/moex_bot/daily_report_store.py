"""Serialized, atomic publication of dated reports, never trading state."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

from .scheduler import MOSCOW


def closing_window(day: date, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("aware close time required")
    return (datetime.combine(day, time(23, 50), MOSCOW) <= now
            < datetime.combine(day + timedelta(days=1), time(6, 50), MOSCOW))


@contextmanager
def report_lock(output: Path) -> Iterator[None]:
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix(".lock")
    if lock.is_symlink():
        raise ValueError("report lock cannot be a symlink")
    with lock.open("a+b") as stream:
        if os.name == "nt":
            module = import_module("msvcrt")
            if lock.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            module.locking(stream.fileno(), module.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                module.locking(stream.fileno(), module.LK_UNLCK, 1)
        else:
            module = import_module("fcntl")
            module.flock(stream.fileno(), module.LOCK_EX | module.LOCK_NB)
            try:
                yield
            finally:
                module.flock(stream.fileno(), module.LOCK_UN)


def atomic_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ValueError("report output cannot be a symlink")
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def final_report(output: Path, day: date, kind: str,
                 binding: str | None = None) -> dict[str, Any] | None:
    """Recognize legacy finals too; fail closed on foreign/corrupt evidence."""
    path = output.with_suffix(".json")
    if path.is_symlink():
        raise ValueError("report evidence cannot be a symlink")
    if not path.exists():
        return None
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or data.get("report_date", str(day)) != str(day):
        raise ValueError("report date mismatch")
    observed = datetime.fromisoformat(data["observed_at"])
    if not closing_window(day, observed):
        return None  # Old buggy daytime output is not a final report.
    if binding is not None:
        actual = data.get("account_binding")
        if actual is None and kind == "long":
            close = output.parent / f"long-close-{day}.json"
            if close.is_symlink():
                raise ValueError("unsafe close snapshot")
            raw = json.loads(close.read_bytes())
            if raw.get("report_date") != str(day) or not closing_window(
                day, datetime.fromisoformat(raw["observed_at"])
            ):
                raise ValueError("close snapshot date mismatch")
            actual = raw.get("account_binding")
        if actual is not None and actual != binding:
            raise ValueError("report account mismatch")
        if kind == "long" and actual is None:
            raise ValueError("report account unverified")
    if kind == "long":
        perf = data["performance"]
        if perf.get("pnl") is None:
            return dict(data) if data.get("publication_final") is True else None
        if not Decimal(str(perf["pnl"])).is_finite():
            raise ValueError("nonfinite report P&L")
        if not closing_window(day, datetime.fromisoformat(perf["period_ended_at"])):
            raise ValueError("report valuation date mismatch")
    elif kind == "prod":
        if data.get("environment") != "prod":
            raise ValueError("report environment mismatch")
        if data.get("check") != "OK":
            return None
    else:
        raise ValueError("unsupported report kind")
    return dict(data)


def attempt(output: Path, day: date, now: datetime, state: str, reason: str) -> None:
    atomic_text(output.with_suffix(".attempt.json"), json.dumps({
        "report_date": str(day), "attempted_at": now.isoformat(), "state": state,
        "stage": "publication", "reason": reason,
    }, ensure_ascii=False) + "\n")
