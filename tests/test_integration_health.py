import io
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

from moex_bot.integration_health import probe_algopack


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


NOW = datetime(2026, 10, 30, 7, tzinfo=UTC)


def test_probe_algopack_performs_authenticated_bounded_read() -> None:
    captured: dict[str, object] = {}

    def opener(request: object, *, timeout: float) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response({"data": {"columns": [], "data": []}})

    result = probe_algopack(
        base_url="https://apim.moex.com/iss", token="secret", as_of=NOW, opener=opener
    )
    request = captured["request"]
    assert result.ok
    assert result.status == "ok"
    assert request.get_header("Authorization") == "Bearer secret"  # type: ignore[attr-defined]
    assert "limit=1" in request.full_url  # type: ignore[attr-defined]


def test_probe_algopack_explains_unauthorized_token() -> None:
    def opener(request: object, *, timeout: float) -> Response:
        raise HTTPError("url", 401, "Unauthorized", {}, io.BytesIO())

    result = probe_algopack(
        base_url="https://apim.moex.com/iss", token="expired", as_of=NOW, opener=opener
    )
    assert not result.ok
    assert result.status == "unauthorized"
    assert "401" in result.detail


def test_probe_algopack_reports_missing_token_without_network() -> None:
    result = probe_algopack(base_url="https://apim.moex.com/iss", token=" ", as_of=NOW)
    assert not result.ok
    assert result.status == "missing"


def test_probe_algopack_reports_network_failure_without_exposing_token() -> None:
    def opener(request: object, *, timeout: float) -> Response:
        raise URLError("offline")

    result = probe_algopack(
        base_url="https://apim.moex.com/iss", token="do-not-print", as_of=NOW, opener=opener
    )
    assert not result.ok
    assert result.status == "network"
    assert "do-not-print" not in result.detail
