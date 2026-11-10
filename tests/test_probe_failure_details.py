import io
import socket
import ssl
from datetime import UTC, datetime
from urllib.error import URLError

import pytest

from moex_bot import monitoring_probes
from moex_bot.geo_feed import GeoSource, build_geo_payload
from moex_bot.monitoring_labels import compact_checks, safe_reason


@pytest.mark.parametrize("exc,kind", [
    (TimeoutError("SECRET"), "timeout"),
    (URLError(TimeoutError("SECRET")), "timeout"),
    (URLError(socket.gaierror("SECRET")), "dns"),
    (URLError(ssl.SSLError("SECRET")), "tls"),
    (ConnectionResetError("SECRET"), "connection"),
    (RuntimeError("SECRET"), "unknown"),
])
def test_probe_has_safe_specific_failure_and_alert(monkeypatch, exc, kind):
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr(monitoring_probes, "urlopen", fail)
    result = monitoring_probes.request_probe("https://example.test/SECRET")
    assert result.startswith(f"FAIL: probe {kind} (")
    message = compact_checks({"api:geo_moex_risk": result})
    assert "SECRET" not in message + result
    assert "api:geo_moex_risk" in message
    assert safe_reason(result) != "сбой; подробности в diagnose"


@pytest.mark.parametrize("content,healthy", [
    (b'<feed xmlns="http://www.w3.org/2005/Atom"/>', True),
    (b'<rss><channel/></rss>', True),
    (b'<html/>', False), (b'broken', False),
])
def test_probe_and_geo_collector_agree_on_feed_schema(monkeypatch, content, healthy):
    monkeypatch.setattr(monitoring_probes, "urlopen", lambda *a, **k: io.BytesIO(content))
    probe = monitoring_probes.request_probe("https://www.moex.com/feed", xml=True)
    payload, ok = build_geo_payload(
        (GeoSource("moex_risk", "https://www.moex.com/feed", "primary"),),
        as_of=datetime(2026, 11, 10, tzinfo=UTC), fetch=lambda _: content)
    assert (probe == "OK") is healthy
    assert ok is healthy
    if not healthy:
        assert payload["feed_observed_at"] is None
        assert payload["source_failures"] == {"moex_risk": "schema"}


def test_geo_timeout_remains_fail_closed_without_secret():
    def fail(_):
        raise URLError(TimeoutError("SECRET"))
    payload, ok = build_geo_payload(
        (GeoSource("moex_risk", "https://www.moex.com/feed", "primary"),),
        as_of=datetime(2026, 11, 10, tzinfo=UTC), fetch=fail)
    assert not ok
    assert payload["source_failures"] == {"moex_risk": "timeout"}
    assert payload["feed_observed_at"] is None
    assert "SECRET" not in str(payload)
