import io
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

import pytest

from moex_bot import geo_feed, notifications
from moex_bot.geo_feed import FeedItem, GeoSource, build_geo_payload, load_sources, parse_feed
from moex_bot.notifications import SQLiteOutbox, TelegramBotApiSender

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [dict(source_id="a", url="https://moex.com/rss", source_tier="primary")] * 2,
        [dict(source_id=" ", url="https://moex.com/rss", source_tier="primary")],
        [dict(source_id="a", url="https://moex.com/rss", source_tier="social")],
    ],
)
def test_geo_source_validation(tmp_path, rows):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        load_sources(path)


def test_fetch_xml_bounds_and_type(monkeypatch):
    monkeypatch.setattr(geo_feed, "urlopen", lambda *a, **k: io.BytesIO(b"rss"))
    assert geo_feed.fetch_xml("https://moex.com/rss") == b"rss"
    with pytest.raises(ValueError, match="size limit"):
        geo_feed.fetch_xml("https://moex.com/rss", max_bytes=2)
    monkeypatch.setattr(geo_feed, "urlopen", lambda *a, **k: io.StringIO("rss"))
    with pytest.raises(ValueError, match="bytes"):
        geo_feed.fetch_xml("https://moex.com/rss")


def test_geo_atom_dates_and_invalid_items():
    source = GeoSource("moex", "https://moex.com/rss", "primary")
    content = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Notice</title><link href="https://moex.com/n1"/>
      <updated>2026-10-30T10:00:00</updated></entry>
      <entry><title>Missing date</title></entry><entry><updated>2026-10-30</updated></entry>
      </feed>"""
    items = parse_feed(source, content)
    assert len(items) == 1
    assert items[0].link == "https://moex.com/n1"
    assert items[0].published_at.tzinfo is UTC
    assert parse_feed(source, b"<rss><item/></rss>") == ()
    assert geo_feed._repair_mojibake("Р°🙂") == "Р°🙂"
    assert geo_feed._published("2026-10-30T10:00:00Z").tzinfo is not None


@pytest.mark.parametrize(
    "title,source,category",
    [
        ("приостановка торгов", "moex_risk", "market_infrastructure"),
        ("ключевую ставку", "cbr", "macro_policy"),
        ("notice", "cbr", "official_notice"),
        ("sanction", "moex", "geopolitical"),
    ],
)
def test_geo_categories(title, source, category):
    item = FeedItem(source, "primary", title, "https://moex.com/n1", NOW)
    assert geo_feed._category(item) == category
    if source == "moex_risk":
        assert geo_feed._severity(title) == 4


def test_geo_payload_requires_aware_as_of():
    with pytest.raises(ValueError, match="timezone-aware"):
        build_geo_payload((), as_of=datetime(2026, 1, 1))


@pytest.mark.parametrize("chat,text", [("", "message"), ("chat", ""), ("chat", "x" * 4097)])
def test_telegram_invalid_input(chat, text):
    with pytest.raises(ValueError):
        TelegramBotApiSender("token").send_text(chat, text)


def test_telegram_missing_token():
    with pytest.raises(ValueError):
        TelegramBotApiSender(" ")


@pytest.mark.parametrize(
    "body,message", [(b"{}", "rejected"), (b"bad", "invalid JSON"), (b'{"ok":true}', None)]
)
def test_telegram_response_contract(monkeypatch, body, message):
    monkeypatch.setattr(notifications.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    sender = TelegramBotApiSender("SECRET")
    if message:
        with pytest.raises(RuntimeError, match=message):
            sender.send_text("chat", "message")
    else:
        sender.send_text("chat", "message")


@pytest.mark.parametrize(
    "error,message",
    [
        (HTTPError("https://example/SECRET", 401, "secret", {}, None), "HTTP error 401"),
        (URLError("SECRET"), "network error"),
        (TimeoutError("SECRET"), "network error"),
    ],
)
def test_telegram_errors_never_expose_token(monkeypatch, error, message):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(notifications.request, "urlopen", fail)
    with pytest.raises(RuntimeError, match=message) as caught:
        TelegramBotApiSender("SECRET").send_text("chat", "message")
    assert "SECRET" not in str(caught.value)


def test_outbox_requires_aware_time_and_positive_issue_limit(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(ValueError, match="positive"):
        outbox.issues(now=NOW, limit=0)
    with pytest.raises(ValueError, match="positive"):
        outbox.claim(now=NOW, limit=0)
    with pytest.raises(ValueError, match="timezone"):
        outbox.health(now=datetime(2026, 1, 1))
