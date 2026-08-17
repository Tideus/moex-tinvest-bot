from datetime import UTC, datetime
from decimal import Decimal

from moex_bot.domain import GeoEvent, GeoRiskLevel
from moex_bot.geo import assess_geo_risk


def test_confirmed_critical_event_blocks_new_risk() -> None:
    event = GeoEvent(
        "sanctions-1",
        5,
        Decimal("0.9"),
        "primary",
        True,
        frozenset({"SBER"}),
        datetime.now(UTC),
    )
    result = assess_geo_risk([event])
    assert result.level is GeoRiskLevel.CRITICAL
    assert result.multiplier == Decimal("0")
    assert "SBER" in result.blocked_secids


def test_unconfirmed_candidate_only_elevates() -> None:
    event = GeoEvent(
        "rumour",
        5,
        Decimal("0.5"),
        "social",
        False,
        frozenset({"SBER"}),
        datetime.now(UTC),
    )
    result = assess_geo_risk([event])
    assert result.level is GeoRiskLevel.ELEVATED
    assert not result.blocked_secids
