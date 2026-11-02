"""Research-only daily long 20/10 challenger; never construct a broker adapter."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from .adapters import AuditPort, DryRunExecutionAdapter
from .config import BotConfig
from .domain import MarketObservation, PortfolioSnapshot, Target
from .harness import TradingHarness
from .integrations.moexalgo_data import HistoricalCandle
from .strategy import allocation_units


def channel_levels(previous: Sequence[HistoricalCandle]) -> tuple[Decimal, Decimal] | None:
    """Caller supplies completed sessions strictly before the decision bar."""
    if len(previous) < 20:
        return None
    return max(c.high for c in previous[-20:]), min(c.low for c in previous[-10:])


class ChannelReplayHarness(TradingHarness):
    def __init__(
        self,
        config: BotConfig,
        execution: DryRunExecutionAdapter,
        audit: AuditPort,
        histories: Mapping[str, Sequence[HistoricalCandle]],
    ) -> None:
        super().__init__(config, execution, audit)
        self.histories = histories
        self.liquidating: set[str] = set()

    def _select_targets(
        self, market: Mapping[str, MarketObservation], portfolio: PortfolioSnapshot, as_of: datetime
    ) -> tuple[Target, ...]:
        held = {s for s, pos in portfolio.positions.items() if pos.lots > 0}
        self.liquidating.intersection_update(held)
        ranking: list[tuple[Decimal, str]] = []
        for s, obs in market.items():
            bars = self.histories[s]
            if bars[-1].trading_date != as_of.date() or not (obs.complete and obs.tradable):
                continue
            levels = channel_levels(bars[-21:-1])
            if levels is None:
                continue
            upper, lower = levels
            if s in held:
                if obs.price < lower:
                    self.liquidating.add(s)
            elif obs.price > upper:
                ranking.append((obs.price / upper - 1, s))
        slots = max(0, self.config.strategy.top_n - len(held))
        entrants = [s for _, s in sorted(ranking, key=lambda r: (-r[0], r[1]))[:slots]]
        names = sorted(held - self.liquidating) + entrants
        selected = tuple(market[s] for s in names)
        units = allocation_units(selected, self.config.strategy)
        total = sum(units.values(), Decimal(0))
        return tuple(
            Target(
                s,
                self.config.strategy.long_target_gross * units[s] / total,
                "channel_20_10:" + ("hold" if s in held else "breakout"),
            )
            for s in names
        )
