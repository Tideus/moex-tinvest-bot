"""Private fsynced raw T-Invest execution evidence; never logs headers or URLs."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .integrations.tinvest_sandbox import JsonPostTransport
from .service_config import PROD_REST

PREFIX = PROD_REST + "/tinkoff.public.invest.api.contract.v1."
SECRET_KEYS = {"token", "authorization", "apikey", "accesstoken", "refreshtoken", "secret"}


def _redact(value: object, secrets: tuple[str, ...] = ()) -> object:
    if isinstance(value, Mapping):
        return {
            str(k): "[REDACTED]"
            if re.sub(r"[^a-z]", "", str(k).lower()) in SECRET_KEYS
            else _redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"https?://[^\s]+", "[REDACTED_URL]", value)
    return value


@dataclass
class AuditJsonTransport:
    delegate: JsonPostTransport
    path: Path

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append(self, event: Mapping[str, object]) -> None:
        # Dedicated private evidence directory; never silently chmod a shared directory.
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("audit paths must not be symlinks")
        created_directories = []
        for parent in (self.path.parent, *self.path.parent.parents):
            if parent.exists() and not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError("audit ancestors must be real directories")
            if not parent.exists():
                created_directories.append(parent)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = self.path.parent.stat()
        if not stat.S_ISDIR(parent_info.st_mode):
            raise ValueError("audit parent must be a directory")
        if os.name != "nt" and (
            stat.S_IMODE(parent_info.st_mode) != 0o700
            or parent_info.st_uid != int(getattr(os, "getuid", lambda: -1)())
        ):
            raise ValueError("audit directory must be private 0700 and owned by the process user")
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | int(getattr(os, "O_NOFOLLOW", 0))
            | int(getattr(os, "O_NONBLOCK", 0))
        )
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("audit evidence must be a single regular file")
            if os.name != "nt" and (
                stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != int(getattr(os, "getuid", lambda: -1)())
            ):
                raise ValueError(
                    "audit evidence must be private 0600 and owned by the process user"
                )
            encoded = (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode()
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        if os.name != "nt":
            self._sync_directory(self.path.parent)
            for directory in created_directories:
                self._sync_directory(directory.parent)

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        method = url.removeprefix(PREFIX)
        if not url.startswith(PREFIX) or not re.fullmatch(r"[A-Za-z]+Service/[A-Za-z]+", method):
            raise ValueError("audit transport accepts fixed production T-Invest methods only")
        sensitive = []
        for key, value in headers.items():
            if re.sub(r"[^a-z]", "", key.lower()) in SECRET_KEYS | {"xapikey"}:
                sensitive.append(value)
                if value.lower().startswith("bearer "):
                    sensitive.append(value[7:])
        secrets = tuple(value for value in sensitive if value)
        self._append(
            {
                "observed_at": datetime.now(UTC).isoformat(),
                "event": "request",
                "method": method,
                "payload_withoutsecrets": _redact(payload, secrets),
            }
        )
        try:
            response = self.delegate.post(
                url, headers=headers, payload=payload, timeout_seconds=timeout_seconds
            )
        except Exception as exc:
            self._append(
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "event": "failure",
                    "method": method,
                    "failure_class": type(exc).__name__,
                }
            )
            raise
        self._append(
            {
                "observed_at": datetime.now(UTC).isoformat(),
                "event": "response",
                "method": method,
                "response_raw": _redact(response, secrets),
            }
        )
        return response
