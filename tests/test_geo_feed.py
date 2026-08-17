from datetime import UTC, datetime
from pathlib import Path

from moex_bot.geo_feed import (
    GeoSource,
    _repair_mojibake,
    build_geo_payload,
    load_sources,
    refresh_geo_feed,
)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><item>
<title>MOEX: risk parameters changed</title>
<link>https://www.moex.com/n1</link>
<pubDate>Fri, 14 Aug 2026 08:00:00 +0000</pubDate>
</item></channel></rss>"""


def test_geo_feed_deduplicates_primary_events() -> None:
    source = GeoSource("moex", "https://www.moex.com/feed", "primary")
    payload, healthy = build_geo_payload(
        (source,),
        as_of=datetime(2026, 8, 14, 10, tzinfo=UTC),
        fetch=lambda url: RSS,
    )
    assert healthy
    assert payload["feed_observed_at"] is not None


def test_failed_source_marks_feed_stale() -> None:
    source = GeoSource("moex", "https://www.moex.com/feed", "primary")

    def fail(url: str) -> bytes:
        raise OSError("offline")

    payload, healthy = build_geo_payload(
        (source,),
        as_of=datetime(2026, 8, 14, 10, tzinfo=UTC),
        fetch=fail,
    )
    assert not healthy
    assert payload["feed_observed_at"] is None
    assert payload["failed_sources"] == ["moex"]


def test_source_allowlist_rejects_untrusted_host(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text(
        '[{"source_id":"bad","url":"https://example.com/feed","source_tier":"primary"}]',
        encoding="utf-8",
    )
    try:
        load_sources(path)
    except ValueError as exc:
        assert "allowlist" in str(exc)
    else:
        raise AssertionError("untrusted host was accepted")


def test_refresh_geo_feed_writes_auditable_payload(tmp_path: Path) -> None:
    sources = tmp_path / "sources.json"
    output = tmp_path / "geo.json"
    sources.write_text(
        '[{"source_id":"moex","url":"https://www.moex.com/feed","source_tier":"primary"}]',
        encoding="utf-8",
    )
    healthy = refresh_geo_feed(
        sources_path=sources,
        output_path=output,
        as_of=datetime(2026, 8, 14, 10, tzinfo=UTC),
        fetch=lambda url: RSS,
    )
    assert healthy
    assert '"all_sources_healthy": true' in output.read_text(encoding="utf-8")
    assert '"severity": 2' in output.read_text(encoding="utf-8")


def test_moex_double_encoded_cyrillic_is_repaired() -> None:
    assert _repair_mojibake("РћР± СѓСЃС‚Р°РЅРѕРІР»РµРЅРёРё") == "Об установлении"
