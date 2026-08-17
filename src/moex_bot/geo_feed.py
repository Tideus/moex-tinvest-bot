from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

ALLOWED_HOSTS = frozenset({"cbr.ru", "www.cbr.ru", "moex.com", "www.moex.com"})


@dataclass(frozen=True, slots=True)
class GeoSource:
    source_id: str
    url: str
    source_tier: str


@dataclass(frozen=True, slots=True)
class FeedItem:
    source_id: str
    source_tier: str
    title: str
    link: str
    published_at: datetime


Fetch = Callable[[str], bytes]


def load_sources(path: Path) -> tuple[GeoSource, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = tuple(GeoSource(**item) for item in raw)
    if not sources:
        raise ValueError("geo source list is empty")
    for source in sources:
        parsed = urlparse(source.url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"geo source is outside the allowlist: {source.url}")
    return sources


def fetch_xml(url: str, *, timeout_seconds: float = 10, max_bytes: int = 2_000_000) -> bytes:
    request = Request(url, headers={"User-Agent": "moex-tinvest-shadow/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        content = response.read(max_bytes + 1)
    if not isinstance(content, bytes):
        raise ValueError("geo feed response must be bytes")
    if len(content) > max_bytes:
        raise ValueError("geo feed exceeds size limit")
    return content


def _text(node: ElementTree.Element, names: Iterable[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return _repair_mojibake(child.text.strip())
    return ""


def _repair_mojibake(value: str) -> str:
    markers = ("Р°", "Рµ", "Рё", "Рѕ", "С‚", "СЂ")
    if not any(marker in value for marker in markers):
        return value
    try:
        return value.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _published(value: str) -> datetime:
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result


def parse_feed(source: GeoSource, content: bytes) -> tuple[FeedItem, ...]:
    root = ElementTree.fromstring(content)
    nodes = list(root.findall(".//item"))
    if not nodes:
        nodes = list(root.findall(".//{*}entry"))
    items: list[FeedItem] = []
    for node in nodes:
        title = _text(node, ("title", "{*}title"))
        link = _text(node, ("link", "{*}link"))
        if not link:
            link_node = node.find("{*}link")
            link = link_node.attrib.get("href", "") if link_node is not None else ""
        published = _text(
            node,
            ("pubDate", "published", "updated", "{*}published", "{*}updated"),
        )
        if not title or not published:
            continue
        items.append(
            FeedItem(
                source.source_id,
                source.source_tier,
                title,
                link,
                _published(published),
            )
        )
    return tuple(items)


def _severity(title: str) -> int:
    normalized = title.casefold()
    high = (
        "приостановлен",
        "приостановк",
        "остановк торгов",
        "чрезвычай",
        "дефолт",
        "кибератак",
    )
    elevated = (
        "санкц",
        "ограничительн",
        "ключевую ставку",
        "ключевой ставки",
        "риск-параметр",
        "risk parameter",
        "военн",
    )
    if any(keyword in normalized for keyword in high):
        return 4
    if any(keyword in normalized for keyword in elevated):
        return 2
    return 1


def build_geo_payload(
    sources: tuple[GeoSource, ...],
    *,
    as_of: datetime,
    fetch: Fetch = fetch_xml,
    lookback_hours: int = 6,
) -> tuple[dict[str, object], bool]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of - timedelta(hours=lookback_hours)
    items: list[FeedItem] = []
    failed: list[str] = []
    for source in sources:
        try:
            items.extend(parse_feed(source, fetch(source.url)))
        except (OSError, ValueError, ElementTree.ParseError):
            failed.append(source.source_id)
    deduplicated: dict[str, FeedItem] = {}
    for item in items:
        if cutoff <= item.published_at.astimezone(as_of.tzinfo) <= as_of:
            key = hashlib.sha256(f"{item.source_id}|{item.link}|{item.title}".encode()).hexdigest()
            deduplicated[key] = item
    events: list[dict[str, object]] = []
    for event_id, item in sorted(deduplicated.items()):
        severity = _severity(item.title)
        if severity < 2:
            continue
        events.append(
            {
                "event_id": event_id[:24],
                "severity": severity,
                "confidence": "1.0",
                "source_tier": item.source_tier,
                "confirmed": True,
                "affected_secids": [],
                "observed_at": item.published_at.isoformat(),
                "title": item.title,
                "link": item.link,
            }
        )
    healthy = not failed
    payload: dict[str, object] = {
        "feed_observed_at": as_of.isoformat() if healthy else None,
        "all_sources_healthy": healthy,
        "failed_sources": failed,
        "source_count": len(sources),
        "events": events,
    }
    return payload, healthy


def refresh_geo_feed(
    *,
    sources_path: Path,
    output_path: Path,
    as_of: datetime,
    fetch: Fetch = fetch_xml,
) -> bool:
    sources = load_sources(sources_path)
    payload, healthy = build_geo_payload(sources, as_of=as_of, fetch=fetch)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    return healthy
