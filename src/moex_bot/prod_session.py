"""Cross-process production/update exclusion on a root-owned, stable lock inode."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

LOCK_PATH = Path("/var/lib/moex-tinvest-control/prod-session.lock")
NOFOLLOW_FLAG = "O_NOFOLLOW"  # Unix-only symbol; keep Windows type checking portable.


@contextmanager
def production_session() -> Iterator[None]:
    """Ubuntu only. Never create/replace the operator-owned lock from bot code."""
    try:
        fcntl = import_module("fcntl")
    except ImportError as exc:
        raise ValueError("production sessions require native Unix flock") from exc
    for parent in (LOCK_PATH.parent, *LOCK_PATH.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(
                "production control parents must be root-owned non-writable directories"
            )
    info = LOCK_PATH.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o007
        or info.st_nlink != 1
    ):
        raise ValueError("production lock must be a private root-owned regular file")
    descriptor = os.open(LOCK_PATH, os.O_RDWR | int(getattr(os, NOFOLLOW_FLAG)))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("production lock inode changed during open")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("another production session or deployment is running") from exc
        yield
    finally:
        os.close(descriptor)
