"""Read-only liveness checks for an enrolled, once-per-account sandbox LONG."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .long_auto_once import OnceLedger
from .long_auto_operator import ANCHOR, MARKER, TERMINAL


def enrolled(state: Path) -> bool:
    # Orphan evidence must fail the one-shot checks, never fall back to shadow.
    return any(
        p.exists() or p.is_symlink() for p in (MARKER, state / "long-once/sandbox.sqlite3", ANCHOR)
    )


class ReadOnlyLedger(OnceLedger):
    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)


def evidence(state: Path, now: datetime) -> dict[str, str]:
    try:
        if MARKER.is_symlink() or MARKER.read_bytes() != b"sandbox-long-once/v1\n":
            raise ValueError("invalid enrollment")
        base = state / "long-once"
        status = ReadOnlyLedger(
            base / "sandbox.sqlite3", os.environ["T_INVEST_SANDBOX_LONG_ACCOUNT_ID"], anchor=ANCHOR
        ).status()
        if status in TERMINAL:
            return {"data:long": "OK"}  # A spent attempt must not produce new plans.
        if status != "WAITING":
            return {"data:long": "FAIL: reconcile one-shot entry/protection"}
        local = now.astimezone(ZoneInfo("Europe/Moscow"))
        due = local.replace(hour=10, minute=5, second=0, microsecond=0)
        # Allow the full systemd service timeout after a scheduled wake-up.
        if local < due + timedelta(minutes=20):
            due -= timedelta(days=1)
        while due.weekday() >= 5:
            due -= timedelta(days=1)
        if MARKER.stat().st_mtime > due.timestamp():
            return {"data:long": "OK"}  # First scheduled run is still ahead.
        # Use the decision's timestamp, not merely a copied file's modification time.
        plans = sorted(base.glob(f"run-{due:%Y%m%d}T*/plan.json"), reverse=True)
        if plans:
            raw = json.loads(plans[0].read_bytes())
            run = raw["run_id"]
            at = datetime.fromisoformat(run.removeprefix("shadow-"))
            if (
                run.startswith("shadow-")
                and at.tzinfo is not None
                and due <= at <= now
                and at <= due + timedelta(minutes=20)
            ):
                return {
                    "data:long": "OK",
                    "quality:long": "OK"
                    if raw["quality"]["passed"] is True
                    else "FAIL: quality gate blocked",
                }
        return {"data:long": "FAIL: scheduled one-shot plan missing"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return {"data:long": "FAIL: one-shot enrollment/ledger/plan unreadable"}
