from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class IntegrationProbe:
    ok: bool
    status: str
    detail: str


def probe_algopack(
    *,
    base_url: str,
    token: str,
    as_of: datetime,
    timeout: float = 10,
    opener: Callable[..., object] = urlopen,
) -> IntegrationProbe:
    """Perform one bounded authenticated read-only ALGOPACK request."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if not token.strip():
        return IntegrationProbe(False, "missing", "MOEX_APIKEY is missing")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    query = urlencode(
        {
            "date": as_of.astimezone(MOSCOW).date().isoformat(),
            "latest": 1,
            "limit": 1,
            "iss.meta": "off",
        }
    )
    url = f"{base_url.rstrip('/')}/datashop/algopack/eq/tradestats.json?{query}"
    request = Request(
        url,
        headers={"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:  # type: ignore[attr-defined]
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            return IntegrationProbe(
                False,
                "unauthorized",
                "HTTP 401: ALGOPACK token is expired, revoked, or rejected",
            )
        return IntegrationProbe(False, f"http_{exc.code}", f"ALGOPACK HTTP {exc.code}")
    except (TimeoutError, URLError):
        return IntegrationProbe(False, "network", "ALGOPACK network error or timeout")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return IntegrationProbe(False, "invalid_response", "ALGOPACK returned invalid JSON")
    if not isinstance(payload, Mapping):
        return IntegrationProbe(False, "invalid_response", "ALGOPACK JSON is not an object")
    return IntegrationProbe(True, "ok", "authenticated read-only request succeeded")
