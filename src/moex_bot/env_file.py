from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def upsert_env_value(path: Path, key: str, value: str) -> None:
    """Atomically replace every key occurrence, or append it, without exposing secrets."""
    if not _ENV_KEY.fullmatch(key):
        raise ValueError("invalid environment variable name")
    if "\n" in value or "\r" in value:
        raise ValueError("environment value must be a single line")
    if path.exists() and path.is_symlink():
        raise ValueError("refusing to update a symlinked env file")

    original_stat = path.stat() if path.exists() else None
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    replacement = f"{key}={value}"
    found = False
    updated: list[str] = []
    for line in lines:
        if line.lstrip().startswith(f"{key}="):
            if not found:
                updated.append(replacement)
                found = True
            continue
        updated.append(line)
    if not found:
        updated.append(replacement)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(
                stat.S_IMODE(original_stat.st_mode) if original_stat is not None else 0o600
            )
            if original_stat is not None:
                chown = getattr(os, "chown", None)
                if chown is not None:
                    # A non-root owner may not be allowed to restore the original group.
                    with suppress(PermissionError):
                        chown(temporary, original_stat.st_uid, original_stat.st_gid)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
