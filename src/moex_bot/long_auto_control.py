"""Cross-process exclusion shared with sandbox LONG shell writers and backups, Linux only."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

CONTROL_DIR = Path("/var/lib/moex-tinvest-control")
DIRECTORY_FLAG = "O_DIRECTORY"
NOFOLLOW_FLAG = "O_NOFOLLOW"


@contextmanager
def writer_lock() -> Iterator[None]:
    """Retain a verified flock through mutation; do not unlock an inherited wrapper FD."""
    try:
        flock = import_module("fcntl")
    except ImportError as exc:
        raise ValueError("sandbox LONG writer lock requires Linux flock") from exc
    for path in (*reversed(CONTROL_DIR.parents), CONTROL_DIR):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe sandbox LONG control directory")
    # Duplicate first so we compare and use the same open file description.
    try:
        inherited = os.dup(7)
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise
        inherited = -1
    if inherited != -1:
        try:
            matches = os.path.samestat(os.fstat(inherited), info)
        except OSError:
            os.close(inherited)
            raise
        if not matches:
            os.close(inherited)
            inherited = -1
    fd = inherited
    if fd == -1:
        directory = getattr(os, DIRECTORY_FLAG, 0)
        nofollow = getattr(os, NOFOLLOW_FLAG, 0)
        if not directory or not nofollow:
            raise ValueError("platform lacks safe directory-open flags")
        fd = os.open(CONTROL_DIR, os.O_RDONLY | directory | nofollow)
    try:
        if not os.path.samestat(os.fstat(fd), info):
            raise ValueError("sandbox LONG control directory changed")
        try:
            flock.flock(fd, flock.LOCK_EX | flock.LOCK_NB)
        except OSError as exc:
            raise ValueError("sandbox LONG writer or backup already active") from exc
        if not os.path.samestat(CONTROL_DIR.lstat(), info):
            raise ValueError("sandbox LONG control directory changed while acquiring lock")
        yield
    finally:
        # Explicit LOCK_UN here would prematurely release the shell parent's lock.
        os.close(fd)
