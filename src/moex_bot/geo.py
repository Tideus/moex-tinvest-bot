from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .domain import GeoEvent, GeoRiskLevel, GeoRiskSnapshot


def assess_geo_risk(events: Iterable[GeoEvent], news_stale: bool = False) -> GeoRiskSnapshot:
    event_list = list(events)
    reasons: list[str] = []
    blocked: set[str] = set()
    level = GeoRiskLevel.NORMAL
    multiplier = Decimal("1")

    if news_stale:
        level = GeoRiskLevel.ELEVATED
        multiplier = Decimal("0.70")
        reasons.append("news feed is stale")

    for event in event_list:
        if not event.confirmed:
            if level is GeoRiskLevel.NORMAL:
                level = GeoRiskLevel.ELEVATED
                multiplier = min(multiplier, Decimal("0.70"))
            reasons.append(f"unconfirmed material candidate: {event.event_id}")
            continue
        if event.severity >= 5 and event.confidence >= Decimal("0.80"):
            level = GeoRiskLevel.CRITICAL
            multiplier = Decimal("0")
            blocked.update(event.affected_secids)
            reasons.append(f"critical confirmed event: {event.event_id}")
        elif event.severity >= 4 and event.confidence >= Decimal("0.70"):
            if level is not GeoRiskLevel.CRITICAL:
                level = GeoRiskLevel.HIGH
                multiplier = min(multiplier, Decimal("0.30"))
            blocked.update(event.affected_secids)
            reasons.append(f"high confirmed event: {event.event_id}")
        elif event.severity >= 2:
            if level is GeoRiskLevel.NORMAL:
                level = GeoRiskLevel.ELEVATED
                multiplier = min(multiplier, Decimal("0.70"))
            reasons.append(f"elevated event: {event.event_id}")

    return GeoRiskSnapshot(level, multiplier, frozenset(blocked), tuple(reasons))
