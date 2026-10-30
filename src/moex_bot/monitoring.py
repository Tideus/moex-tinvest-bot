"""Monitoring CLI; collectors and delivery policy are separate modules."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .monitoring_config import load_settings as load_settings
from .monitoring_incidents import notify as notify
from .monitoring_probes import collect as collect
from .monitoring_probes import disk_status as disk_status
from .monitoring_probes import request_probe as request_probe
from .notifications import SQLiteOutbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=Path("/opt/moex-tinvest-bot"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/moex-tinvest-bot"))
    parser.add_argument("--test-alert", action="store_true")
    parser.add_argument("--collect-only", action="store_true",
                        help="Read-only probes and snapshot; no incident/outbox writes")
    args = parser.parse_args()
    if args.test_alert and args.collect_only:
        parser.error("--test-alert and --collect-only are mutually exclusive")
    now = datetime.now(UTC)
    if args.test_alert:
        SQLiteOutbox(args.state / "data/notifications.sqlite3").enqueue(
            kind="monitoring_test", dedupe_key=f"monitor-test:{now.isoformat()}",
            body="🧪 ТЕСТ МОНИТОРИНГА · MOEX BOT\nПроверка канала алертов. Это не авария.", now=now)
        return 0
    settings = load_settings(args.app / "config/monitoring.json")
    try:
        checks = collect(args.app, args.state, settings, now)
    except Exception as exc:
        checks = {"monitor:collector": f"FAIL: {type(exc).__name__}; inspect configuration/storage"}
    if args.collect_only:
        failures = sum(value != "OK" for value in checks.values())
    else:
        outbox = SQLiteOutbox(args.state / "data/notifications.sqlite3")
        failures = notify(checks, args.state / "data/monitoring.sqlite3", outbox, now, settings)
    output = args.state / "artifacts/monitoring-latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"schema_version": 1, "observed_at": now.isoformat(),
                                         "checks": checks}, ensure_ascii=False, indent=2))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    for key, value in checks.items():
        print(f"{key}: {value}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
