from decimal import Decimal

from moex_bot.config import BotConfig, StrategyConfig
from moex_bot.domain import (
    ExecutionMode,
    GeoRiskLevel,
    GeoRiskSnapshot,
    Instrument,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    Side,
)
from moex_bot.risk import evaluate_intent


def _config() -> BotConfig:
    return BotConfig(
        ExecutionMode.REPLAY,
        "RUB",
        3600,
        Decimal("0.20"),
        Decimal("50000"),
        Decimal("0.80"),
        Decimal("100000"),
        Decimal("500"),
        5,
        False,
        False,
        StrategyConfig(5, Decimal("0"), True),
    )


def test_risk_checks_resulting_position_not_just_increment() -> None:
    instrument = Instrument("AAA", "uid", "TQBR", 10, Decimal("0.01"))
    portfolio = PortfolioSnapshot(
        cash=Decimal("80000"),
        positions={"AAA": Position(instrument, 20)},
    )
    intent = OrderIntent(
        "request",
        instrument,
        Side.BUY,
        1,
        Decimal("100"),
        Decimal("1000"),
        "test",
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    decision = evaluate_intent(
        intent,
        portfolio,
        _config(),
        geo,
        equity=Decimal("100000"),
        gross_exposure=Decimal("20000"),
    )
    assert not decision.allowed
    assert "resulting position" in " ".join(decision.reasons)


def test_risk_checks_projected_gross_exposure() -> None:
    instrument = Instrument("AAA", "uid", "TQBR", 10, Decimal("0.01"))
    intent = OrderIntent(
        "request",
        instrument,
        Side.BUY,
        1,
        Decimal("100"),
        Decimal("1000"),
        "test",
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    decision = evaluate_intent(
        intent,
        PortfolioSnapshot(Decimal("100000")),
        _config(),
        geo,
        equity=Decimal("100000"),
        gross_exposure=Decimal("80000"),
    )
    assert not decision.allowed
    assert "gross exposure" in " ".join(decision.reasons)
