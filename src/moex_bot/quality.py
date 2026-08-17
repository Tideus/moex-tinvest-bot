from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .domain import MarketObservation


@dataclass(frozen=True, slots=True)
class QualityReport:
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def validate_market(
    market: Mapping[str, MarketObservation], as_of: datetime, max_age_seconds: int
) -> QualityReport:
    errors: list[str] = []
    if as_of.tzinfo is None:
        return QualityReport(False, ("as_of must be timezone-aware",))
    if not market:
        return QualityReport(False, ("market snapshot is empty",))
    for secid, item in market.items():
        if secid != item.instrument.secid:
            errors.append(f"identity mismatch for {secid}")
        age = (as_of - item.observed_at).total_seconds()
        if age < 0:
            errors.append(f"future observation for {secid}")
        if age > max_age_seconds:
            errors.append(f"stale observation for {secid}: {age:.0f}s")
        if not item.complete:
            errors.append(f"incomplete candle for {secid}")
    return QualityReport(not errors, tuple(errors))
