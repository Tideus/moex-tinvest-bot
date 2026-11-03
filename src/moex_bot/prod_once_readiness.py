"""Prepare a NON-authorizing review packet. Never enables config, journal, timer or orders."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .live_execution import registered_live_journal
from .live_operator import assert_root_review_path, release_digest
from .prod_long_once_operator import CONFIG, FIXED, SETTINGS, load_settings


def draft(*, account: str, identity: str, release: str, revision: str,
          evidence: dict[str, Path], now: datetime) -> dict[str, Any]:
    """Root reviews report contents; file hashes alone never assert successful broker evidence."""
    if set(evidence) != {"tests", "independent_audit", "restore_drill"} or now.tzinfo is None:
        raise ValueError("complete technical evidence and aware time required")
    hashes = {}
    for kind, path in evidence.items():
        raw = path.read_bytes()
        report = json.loads(raw)
        if (report.get("kind") != kind or report.get("revision") != revision
                or report.get("result") != "PASS" or not report.get("scope")):
            raise ValueError("evidence is not a reviewed passing report for this revision")
        hashes[kind] = hashlib.sha256(raw).hexdigest()
    return {
        "version": 1, "decision": "DRAFT_NOT_AUTHORIZED", "prepared_at": now.isoformat(),
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "journal_uuid": identity, "current_disabled_release_sha256": release,
        "revision": revision, "entry_lots": 1, "max_total_with_fees_rub": "10000",
        "max_attempts_total": 1, "native_stop_required": True, "protect_manual_positions": True,
        "evidence_sha256": hashes | {"sandbox_fill_and_native_stop": None},
        "missing": ["sandbox_fill_and_native_stop", "fresh_operator_review_and_activation"],
        "reviewed_at": None, "expires_at": None,
        "note": "Not a grant. Recompute release binding after final reviewed settings; no re-arm.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("tests", "audit", "restore", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
            raise ValueError("root-reviewed preparation required")
        if args.output.name == Path(str(FIXED["authorization"])).name:
            raise ValueError("draft must never be written to the admission path")
        for path in (SETTINGS, args.tests, args.audit, args.restore):
            assert_root_review_path(path)
        settings = load_settings(SETTINGS)
        if settings["enabled"] is not False:
            raise ValueError("prepare only while entry is disabled")
        account = os.environ["T_INVEST_PROD_ACCOUNT_ID"].strip()
        if not account:
            raise ValueError("explicit bound account required")
        identity = registered_live_journal(Path(settings["journal"]), account)
        info = dict(line.split("=", 1)
                    for line in (CONFIG.parent / "BUILD_INFO").read_text().splitlines()
                    if "=" in line)
        packet = draft(account=account, identity=identity,
                       release=release_digest(CONFIG / "prod_long.json", CONFIG / "universe.json",
                                              SETTINGS), revision=info["git_revision"],
                       evidence={"tests": args.tests, "independent_audit": args.audit,
                                 "restore_drill": args.restore}, now=datetime.now(UTC))
        # Operator-created private directory; no overwriting an existing grant or packet.
        if not args.output.is_absolute() or args.output.resolve() != args.output:
            raise ValueError("canonical output required")
        for parent in args.output.parents:
            if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
                raise ValueError("root-owned nonwritable parent required")
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(packet, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        print("PREPARED_NOT_AUTHORIZED: technical evidence linked; waiting for sandbox BUY + stop")
        return 0
    except Exception:
        print("BLOCKED: readiness packet not created; no config/grant/trading change")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
