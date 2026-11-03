"""Verify restored anti-replay evidence against an independently retained trusted head.

Never creates/replaces files, grants or journals. Empty control is not proof of no BUY.
An operator must retain the latest digest separately from rollbackable backups.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path

from .prod_long_once import Claim, canonical, claim_binding, paired
from .prod_once_observer import candidate_from_claim


def control_digest(directory: Path) -> str:
    names = {"claim.json", "buy-dispatch.json", "stop-dispatch.json",
             "cancel-dispatch.json", "result.json"}
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("real existing control directory required")
    files = sorted(directory.iterdir())
    if not files or any(p.name not in names for p in files):
        raise ValueError("empty/unknown control evidence")
    raw = json.loads((directory / "claim.json").read_bytes())
    candidate = candidate_from_claim(raw)
    _, plan = paired(candidate)
    binding = claim_binding(candidate, plan, raw["journal_uuid"], raw["release_sha256"])
    Claim(directory, binding).check()
    digests: dict[str, str] = {}
    for p in files:
        meta = p.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise ValueError("unsafe evidence file")
        data = p.read_bytes()
        if p.name.endswith("dispatch.json") and data.decode() != canonical(binding):
            raise ValueError("dispatch belongs to another claim")
        digests[p.name] = hashlib.sha256(data).hexdigest()
    if "stop-dispatch.json" in digests and "buy-dispatch.json" not in digests:
        raise ValueError("child dispatch without parent fence")
    return hashlib.sha256(canonical(digests).encode()).hexdigest()


def verify(directory: Path, expected: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None or control_digest(directory) != expected:
        raise ValueError("missing/stale/mismatched control evidence; no re-arm")
    return "RESTORE_CONTROL_VERIFIED_NO_REARM"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-directory", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    try:
        print(verify(args.control_directory, args.expected_sha256))
        return 0
    except Exception:
        print("BLOCKED: control restore unverified; never create an empty claim directory")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
