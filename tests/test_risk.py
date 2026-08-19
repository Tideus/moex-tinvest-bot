from dataclasses import replace
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


def test_risk_preserves_configured_cash_reserve() -> None:
    instrument = Instrument("AAA", "uid", "TQBR", 1, Decimal("0.01"))
    intent = OrderIntent(
        "request", instrument, Side.BUY, 20, Decimal("100"), Decimal("2000"), "test"
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    decision = evaluate_intent(
        intent,
        PortfolioSnapshot(Decimal("10000")),
        replace(_config(), min_cash_reserve_weight=Decimal("0.10")),
        geo,
        equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )
    assert not decision.allowed
    assert "mandatory reserve" in " ".join(decision.reasons)


def test_risk_cannot_sell_blocked_position_or_create_short() -> None:
    instrument = Instrument("AAA", "uid", "TQBR", 1, Decimal("0.01"))
    intent = OrderIntent(
        "request", instrument, Side.SELL, 4, Decimal("100"), Decimal("400"), "test"
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    portfolio = PortfolioSnapshot(
        Decimal("10000"), positions={"AAA": Position(instrument, 5, blocked_lots=2)}
    )
    decision = evaluate_intent(
        intent,
        portfolio,
        _config(),
        geo,
        equity=Decimal("10500"),
        gross_exposure=Decimal("500"),
    )
    assert not decision.allowed
    assert "unblocked long position" in " ".join(decision.reasons)


def test_risk_limits_sector_and_correlated_cluster_exposure() -> None:
    instrument = Instrument(
        "GAZP",
        "uid",
        "TQBR",
        1,
        Decimal("0.01"),
        issuer_id="gazprom",
        sector="energy",
        risk_cluster="hydrocarbons",
    )
    intent = OrderIntent(
        "request", instrument, Side.BUY, 100, Decimal("100"), Decimal("10000"), "test"
    )
    config = replace(
        _config(),
        max_sector_weight=Decimal("0.35"),
        max_risk_cluster_weight=Decimal("0.40"),
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    decision = evaluate_intent(
        intent,
        PortfolioSnapshot(Decimal("100000")),
        config,
        geo,
        equity=Decimal("100000"),
        gross_exposure=Decimal("30000"),
        sector_exposure={"energy": Decimal("30000")},
        risk_cluster_exposure={"hydrocarbons": Decimal("35000")},
    )
    assert not decision.allowed
    reasons = " ".join(decision.reasons)
    assert "sector exposure" in reasons
    assert "risk-cluster exposure" in reasons


def test_risk_rejects_sell_without_existing_position() -> None:
    instrument = Instrument("AAA", "uid", "TQBR", 1, Decimal("0.01"))
    intent = OrderIntent(
        "request", instrument, Side.SELL, 1, Decimal("100"), Decimal("100"), "test"
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    decision = evaluate_intent(
        intent,
        PortfolioSnapshot(Decimal("10000")),
        _config(),
        geo,
        equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
    )
    assert not decision.allowed
    assert "existing long position" in " ".join(decision.reasons)


def test_broker_snapshot_with_active_orders_blocks_new_intents() -> None:
    instrument = Instrument("AAA", "uid", "TQBR", 1, Decimal("0.01"))
    intent = OrderIntent(
        "request", instrument, Side.BUY, 1, Decimal("100"), Decimal("100"), "test"
    )
    geo = GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ())
    decision = evaluate_intent(
        intent,
        PortfolioSnapshot(
            Decimal("10000"), open_orders=1, source="t_invest_sandbox"
        ),
        _config(),
        geo,
        equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
    )
    assert not decision.allowed
    assert "active broker orders" in " ".join(decision.reasons)


def test_risk_allows_bounded_verified_short_with_margin_confirmation() -> None:
    instrument = Instrument(
        "AAA", "uid", "TQBR", 1, Decimal("0.01"), short_enabled=True
    )
    intent = OrderIntent(
        "request",
        instrument,
        Side.SELL,
        5,
        Decimal("100"),
        Decimal("500"),
        "direction=short",
        confirm_margin_trade=True,
    )
    strategy = replace(
        _config().strategy,
        shorts_enabled=True,
        short_top_n=1,
        short_target_gross=Decimal("0.10"),
    )
    config = replace(
        _config(),
        allow_margin=True,
        strategy=strategy,
        max_short_position_weight=Decimal("0.10"),
        max_short_gross_exposure=Decimal("0.20"),
    )
    decision = evaluate_intent(
        intent,
        PortfolioSnapshot(Decimal("10000")),
        config,
        GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ()),
        equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
        short_exposure=Decimal("0"),
    )
    assert decision.allowed
