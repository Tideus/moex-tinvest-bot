from datetime import UTC, datetime
from decimal import Decimal

from moex_bot.config import StrategyConfig
from moex_bot.domain import Instrument, MarketObservation
from moex_bot.strategy import calculate_targets


def _observation(secid: str, momentum: str, volatility: str) -> MarketObservation:
    return MarketObservation(
        Instrument(secid, f"uid-{secid}", "TQBR", 1, Decimal("0.01")),
        Decimal("110"),
        Decimal("100"),
        Decimal(momentum),
        Decimal(volatility),
        datetime(2026, 8, 18, tzinfo=UTC),
        True,
        True,
    )


def test_inverse_volatility_assigns_less_weight_to_riskier_share() -> None:
    config = StrategyConfig(
        2,
        Decimal("0"),
        True,
        inverse_volatility_weights=True,
    )
    targets = calculate_targets(
        [_observation("CALM", "0.10", "0.01"), _observation("RISKY", "0.20", "0.04")],
        config,
    )
    weights = {item.secid: item.weight for item in targets}
    assert weights["CALM"] > weights["RISKY"]
    assert sum(weights.values()) == Decimal("1")


def test_exit_rank_buffer_retains_existing_position_beyond_entry_cutoff() -> None:
    config = StrategyConfig(1, Decimal("0"), True, exit_rank_buffer=1)
    targets = calculate_targets(
        [_observation("NEW", "0.20", "0.02"), _observation("HELD", "0.10", "0.02")],
        config,
        held_secids=frozenset({"HELD"}),
    )
    assert {item.secid for item in targets} == {"NEW", "HELD"}
