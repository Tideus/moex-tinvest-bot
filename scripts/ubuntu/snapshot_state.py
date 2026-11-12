"""Read-only per-database online snapshots, not an atomic cross-database checkpoint."""

import argparse
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def snapshot(source: Path, target: Path) -> None:
    source = source.absolute()
    if source.is_symlink() or not source.is_dir() or source.resolve() != source:
        raise ValueError("state source must be a real directory without linked parents")
    if target.exists() or target.resolve().is_relative_to(source):
        raise ValueError("snapshot target must be new and outside state")
    target.mkdir(mode=0o700, parents=True)
    for path in source.rglob("*"):
        destination = target / path.relative_to(source)
        if path.is_symlink():
            raise ValueError("linked state evidence refused")
        if path.is_dir():
            destination.mkdir(mode=0o700, exist_ok=True)
            continue
        if not path.is_file():
            raise ValueError("non-regular state evidence refused")
        if any(path.name.endswith(s) for s in ("-wal", "-shm", "-journal")):
            main = path.with_name(path.name.rsplit("-", 1)[0])
            if main.is_symlink() or not main.is_file():
                raise ValueError("orphan SQLite companion")
            with main.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\0":
                    raise ValueError("invalid SQLite companion")
            continue
        before = path.stat()
        with path.open("rb") as handle:
            is_db = handle.read(16) == b"SQLite format 3\0"
        if is_db or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            deadline = time.monotonic() + 30

            def progress(status, remaining, total, limit=deadline):
                if time.monotonic() > limit:
                    raise TimeoutError("SQLite snapshot deadline exceeded")

            with (
                closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(destination)) as dst,
            ):
                src.backup(dst, pages=256, progress=progress)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite snapshot integrity failed")
        else:
            shutil.copyfile(path, destination)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("state file changed during snapshot; retry backup")
        destination.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    snapshot(args.source, args.target)
