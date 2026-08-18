from datetime import UTC, datetime
from decimal import Decimal

import pytest

from moex_bot.domain import Instrument, MarketObservation, PortfolioSnapshot, Position


def test_instrument_rounding_and_lots() -> None:
    instrument = Instrument("TEST", "uid", "TQBR", 10, Decimal("0.05"))
    assert instrument.round_price(Decimal("100.03")) == Decimal("100.05")
    assert instrument.floor_lots(Decimal("29")) == 2


def test_invalid_instrument_is_rejected() -> None:
    with pytest.raises(ValueError):
        Instrument("TEST", "uid", "TQBR", 0, Decimal("0.01"))


def test_broker_equity_uses_conservative_minimum_and_includes_blocked_cash() -> None:
    instrument = Instrument("TEST", "uid", "TQBR", 1, Decimal("0.01"))
    observation = MarketObservation(
        instrument,
        Decimal("100"),
        Decimal("100"),
        Decimal("0"),
        Decimal("0.1"),
        datetime.now(UTC),
        True,
        True,
    )
    portfolio = PortfolioSnapshot(
        cash=Decimal("800"),
        blocked_cash=Decimal("100"),
        positions={"TEST": Position(instrument, 2)},
        reported_equity=Decimal("1050"),
        source="t_invest_sandbox",
    )
    assert portfolio.equity({"TEST": observation}) == Decimal("1050")
