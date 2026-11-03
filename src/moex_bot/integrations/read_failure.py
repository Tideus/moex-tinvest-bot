"""Bounded retries and credential-free evidence for MOEX reads, never broker writes."""

from __future__ import annotations

import re
import socket
import ssl
import time
from collections.abc import Callable
from importlib import import_module
from urllib.parse import urlsplit


class MarketDataReadError(RuntimeError):
    def __init__(self, evidence: dict[str, str | int | None]) -> None:
        self.evidence = evidence
        super().__init__("MOEX read failed: " + "; ".join(f"{k}={v}" for k, v in evidence.items()))


def _category(exc: Exception) -> str:
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            return "tls"
        if isinstance(cause, socket.gaierror):
            return "dns"
        cause = cause.__cause__ or cause.__context__
    # Some transports flatten SSL failures into ConnectError's text. Never output that text.
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return "tls"
    return "connection"


def read_moex[T](
    call: Callable[[], T], *, stage: str, secid: str, sleeper: Callable[[float], None] = time.sleep
) -> T:
    """Retry only failed connection establishment, max three attempts; same read parameters."""
    for attempt in range(1, 4):
        try:
            return call()
        except Exception as exc:
            try:
                httpx = import_module("httpx")
            except ImportError:
                raise exc from None
            if not isinstance(exc, httpx.HTTPError):
                raise
            category = _category(exc)
            retry = (
                isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) and category != "tls"
            )
            if retry and attempt < 3:
                sleeper(0.25 * attempt)
                continue
            try:
                url = urlsplit(str(exc.request.url))
            except (AttributeError, RuntimeError, ValueError):
                url = urlsplit("")
            host = url.hostname if url.hostname in {"apim.moex.com", "iss.moex.com"} else "unknown"
            # Only public MOEX path grammar; never query, userinfo, headers, body or raw exception.
            path = (
                url.path
                if host != "unknown"
                and re.fullmatch(
                    r"/iss/(?:securities/[A-Z0-9_]{1,20}|engines/stock/markets/shares/"
                    r"(?:boards/[A-Z0-9]{1,8}/)?securities(?:/[A-Z0-9_]{1,20}(?:/candles)?)?)\.json",
                    url.path,
                )
                else "withheld"
            )
            code = getattr(getattr(exc, "response", None), "status_code", None)
            evidence: dict[str, str | int | None] = {
                "api": "MOEX market data (shared; not T-Invest)",
                "host": host,
                "path": path,
                "stage": stage,
                "secid": secid if re.fullmatch(r"[A-Z0-9_]{1,20}", secid) else "withheld",
                "error": type(exc).__name__,
                "category": "http" if code else category,
                "http_status": code if isinstance(code, int) else None,
                "attempts": attempt,
            }
            raise MarketDataReadError(evidence) from None
    raise AssertionError("unreachable read retry")
