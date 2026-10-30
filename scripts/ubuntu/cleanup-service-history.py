"""Conservative one-off cleanup of explicit bot history roots. Dry-run by default."""
import argparse
import hashlib
import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKUPS = Path("/var/backups/moex-tinvest-bot")
LOGS = Path("/var/log/moex-tinvest-bot")
ARTIFACTS = Path("/var/lib/moex-tinvest-bot/artifacts")


def verified_archives(root: Path, prefix: str, keep: int) -> tuple[list[Path], list[Path]]:
    archives = sorted(root.glob(prefix + "*.tar.gz"),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    protected, retired = archives[:keep], archives[keep:]
    for path in protected:
        if path.is_symlink():
            raise ValueError("backup symlink rejected")
        checksum = Path(str(path) + ".sha256")
        expected = checksum.read_text().split()[0]
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("protected backup checksum mismatch")
        # Read each member fully to validate gzip/tar without extracting secrets.
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.isfile():
                    stream = archive.extractfile(member)
                    assert stream is not None
                    with stream:
                        while stream.read(1024 * 1024):
                            pass
    return protected, retired


def old_files(root: Path, cutoff: datetime) -> list[Path]:
    return [path for path in root.rglob("*")
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
            and path.stat().st_mtime < cutoff.timestamp()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("root required")
    for root in (BACKUPS, LOGS, ARTIFACTS):
        if not root.is_dir() or root.resolve() != root:
            raise ValueError("history roots must be existing literal, non-symlink paths")
    now = datetime.now(UTC)
    protected, retired = [], []
    for prefix, keep in (("moex-tinvest-", 5), ("moex-disaster-", 2)):
        safe, extra = verified_archives(BACKUPS, prefix, keep)
        protected.extend(safe)
        retired.extend(extra)
    candidates = retired + [Path(str(path) + ".sha256") for path in retired]
    candidates += old_files(LOGS, now - timedelta(days=14))
    candidates += old_files(ARTIFACTS, now - timedelta(days=90))
    candidates = [path for path in sorted(set(candidates))
                  if path.is_file() and not path.is_symlink()]
    for path in candidates:
        if not any(path.resolve().is_relative_to(root) for root in (BACKUPS, LOGS, ARTIFACTS)):
            raise ValueError("candidate escapes explicit roots")
    total = sum(path.stat().st_size for path in candidates)
    print(json.dumps({"apply": args.apply, "files": len(candidates), "bytes": total,
                      "protected_backups": [p.name for p in protected]}, indent=2))
    if args.apply:
        removed = []
        for path in candidates:
            path.unlink()
            removed.append(str(path))
        manifest = LOGS / ("cleanup-summary-" + now.strftime("%Y%m%dT%H%M%SZ") + ".json")
        manifest.write_text(json.dumps({"removed": removed, "bytes": total}, indent=2))
        manifest.chmod(0o600)
        print(f"Deletion manifest: {manifest}")


if __name__ == "__main__":
    main()
