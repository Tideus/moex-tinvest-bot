"""GET-only WSGI API behind authenticated nginx; no broker/config access."""

from __future__ import annotations

import os
import sqlite3
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from .journal_readmodel import QueryError, query
from .trader_journal import encoded


def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    status = 200
    method = environ.get("REQUEST_METHOD", "")
    try:
        if method not in {"GET", "HEAD"}:
            raise QueryError("read-only API", 405)
        raw = environ.get("QUERY_STRING", "")
        if len(raw) > 2048:
            raise QueryError("query too long")
        pairs = parse_qsl(raw, keep_blank_values=True, max_num_fields=12)
        params = dict(pairs)
        if len(params) != len(pairs):
            raise QueryError("duplicate parameter")
        route = environ.get("PATH_INFO", "").removeprefix("/journal/api/")
        if "/" in route:
            raise QueryError("not found", 404)
        payload = query(
            Path(
                os.environ.get(
                    "JOURNAL_READMODEL", "/var/lib/moex-trader-journal/readmodel/readmodel.sqlite3"
                )
            ),
            route,
            params,
        )
        if route == "health" and payload["status"] != "ok":
            status = 503
    except QueryError as exc:
        status, payload = exc.status, {"error": str(exc)}
    except ValueError:
        status, payload = 400, {"error": "invalid query"}
    except (OSError, sqlite3.Error):
        status, payload = 503, {"error": "journal projection unavailable"}
    body = encoded(payload).encode()
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    if status == 405:
        headers.append(("Allow", "GET, HEAD"))
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [] if method == "HEAD" else [body]
