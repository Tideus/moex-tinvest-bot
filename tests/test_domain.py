from decimal import Decimal

import pytest

from moex_bot.domain import Instrument


def test_instrument_rounding_and_lots() -> None:
    instrument = Instrument("TEST", "uid", "TQBR", 10, Decimal("0.05"))
    assert instrument.round_price(Decimal("100.03")) == Decimal("100.05")
    assert instrument.floor_lots(Decimal("29")) == 2


def test_invalid_instrument_is_rejected() -> None:
    with pytest.raises(ValueError):
        Instrument("TEST", "uid", "TQBR", 0, Decimal("0.01"))
