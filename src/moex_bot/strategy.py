from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .config import StrategyConfig
from .domain import MarketObservation, Target


def calculate_targets(
    observations: Iterable[MarketObservation], config: StrategyConfig
) -> tuple[Target, ...]:
    eligible = [
        item
        for item in observations
        if item.tradable
        and item.complete
        and item.momentum > config.min_momentum
        and (not config.require_above_trend or item.price > item.trend)
    ]
    ranked = sorted(eligible, key=lambda item: (-item.momentum, item.instrument.secid))[
        : config.top_n
    ]
    if not ranked:
        return ()
    weight = Decimal("1") / Decimal(len(ranked))
    return tuple(
        Target(
            secid=item.instrument.secid,
            weight=weight,
            rationale=f"momentum={item.momentum}; price={item.price}>trend={item.trend}",
        )
        for item in ranked
    )
