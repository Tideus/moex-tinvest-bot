"""Order sizing no-op boundaries and state machine quantities."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_execution import _intent

from moex_bot.domain import MarketObservation, OrderRecord, OrderStatus, PortfolioSnapshot, Target
from moex_bot.execution import build_order_intents, transition_order


@pytest.mark.parametrize("fault", ["missing", "zero", "cap", "minimum"])
def test_sizing_can_safely_skip_untradeable_targets(fault: str) -> None:
    instrument = _intent().instrument
    observation = MarketObservation(
        instrument,
        Decimal(300),
        Decimal(290),
        Decimal(".1"),
        Decimal(".1"),
        datetime.now(UTC),
        True,
        True,
    )
    market = {} if fault == "missing" else {"SBER": observation}
    target = Target("SBER", Decimal(0) if fault == "zero" else Decimal(".1"), "test")
    assert not build_order_intents(
        "r",
        (target,),
        PortfolioSnapshot(Decimal(300000)),
        market,
        Decimal(100000) if fault == "minimum" else Decimal(1),
        Decimal(1) if fault == "cap" else None,
    )


@pytest.mark.parametrize("filled", [-1, 3, 1])
def test_state_machine_rejects_decreasing_overfilled_or_incomplete_final(filled: int) -> None:
    with pytest.raises(ValueError):
        transition_order(
            OrderRecord(_intent(), OrderStatus.ACCEPTED), OrderStatus.FILLED, filled_lots=filled
        )
