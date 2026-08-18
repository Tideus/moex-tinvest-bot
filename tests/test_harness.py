from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from moex_bot.adapters import DryRunExecutionAdapter, JsonlAuditLog
from moex_bot.config import BotConfig, StrategyConfig
from moex_bot.domain import (
    ExecutionMode,
    GeoEvent,
    Instrument,
    MarketObservation,
    PortfolioSnapshot,
)
from moex_bot.harness import TradingHarness


def _config() -> BotConfig:
    return BotConfig(
        mode=ExecutionMode.REPLAY,
        base_currency="RUB",
        max_data_age_seconds=3600,
        max_position_weight=Decimal("0.50"),
        max_order_notional=Decimal("60000"),
        max_gross_exposure=Decimal("1"),
        max_daily_turnover=Decimal("200000"),
        min_trade_notional=Decimal("100"),
        max_open_orders=5,
        allow_margin=False,
        live_interlock=False,
        strategy=StrategyConfig(2, Decimal("0"), True),
    )


def _market(now: datetime) -> dict[str, MarketObservation]:
    first = Instrument("AAA", "uid-a", "TQBR", 10, Decimal("0.01"))
    second = Instrument("BBB", "uid-b", "TQBR", 1, Decimal("0.10"))
    return {
        "AAA": MarketObservation(
            first,
            Decimal("100"),
            Decimal("90"),
            Decimal("0.2"),
            Decimal("0.3"),
            now,
            True,
            True,
        ),
        "BBB": MarketObservation(
            second,
            Decimal("1000"),
            Decimal("900"),
            Decimal("0.1"),
            Decimal("0.2"),
            now,
            True,
            True,
        ),
    }


def test_replay_harness_produces_audited_dry_run_orders(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    config = _config()
    adapter = DryRunExecutionAdapter(config.mode)
    log_path = tmp_path / "audit.jsonl"
    harness = TradingHarness(config, adapter, JsonlAuditLog(log_path))
    result = harness.run(
        run_id="run-1",
        as_of=now,
        market=_market(now),
        portfolio=PortfolioSnapshot(Decimal("100000")),
        geo_events=(),
    )
    assert result.quality.passed
    assert len(result.targets) == 2
    assert len(result.orders) == 2
    assert log_path.read_text(encoding="utf-8").count("order_record") == 2


def test_projected_shadow_orders_do_not_masquerade_as_broker_orders(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    config = _config()
    harness = TradingHarness(
        config,
        DryRunExecutionAdapter(config.mode),
        JsonlAuditLog(tmp_path / "audit.jsonl"),
    )
    result = harness.run(
        run_id="run-tinvest-projection",
        as_of=now,
        market=_market(now),
        portfolio=PortfolioSnapshot(
            Decimal("100000"), open_orders=0, source="t_invest_sandbox"
        ),
        geo_events=(),
    )
    assert len(result.orders) == 2
    assert not result.rejected


def test_stale_market_blocks_run(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    config = _config()
    harness = TradingHarness(
        config,
        DryRunExecutionAdapter(config.mode),
        JsonlAuditLog(tmp_path / "audit.jsonl"),
    )
    result = harness.run(
        run_id="run-2",
        as_of=now,
        market=_market(now - timedelta(hours=2)),
        portfolio=PortfolioSnapshot(Decimal("100000")),
        geo_events=(),
    )
    assert not result.quality.passed
    assert not result.orders


def test_harness_reserves_risk_across_multiple_intents(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    base = _config()
    config = BotConfig(
        mode=base.mode,
        base_currency=base.base_currency,
        max_data_age_seconds=base.max_data_age_seconds,
        max_position_weight=Decimal("0.50"),
        max_order_notional=base.max_order_notional,
        max_gross_exposure=Decimal("0.60"),
        max_daily_turnover=base.max_daily_turnover,
        min_trade_notional=base.min_trade_notional,
        max_open_orders=base.max_open_orders,
        allow_margin=False,
        live_interlock=False,
        strategy=base.strategy,
    )
    harness = TradingHarness(
        config,
        DryRunExecutionAdapter(config.mode),
        JsonlAuditLog(tmp_path / "audit.jsonl"),
    )
    result = harness.run(
        run_id="run-3",
        as_of=now,
        market=_market(now),
        portfolio=PortfolioSnapshot(Decimal("100000")),
        geo_events=(),
    )
    assert len(result.orders) == 1
    assert len(result.rejected) == 1
    assert "gross exposure" in " ".join(result.rejected[0]["reasons"])


def test_geo_multiplier_reduces_target_weights(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    config = _config()
    event = GeoEvent(
        "verified-elevated-event",
        2,
        Decimal("0.90"),
        "primary",
        True,
        frozenset(),
        now,
    )
    harness = TradingHarness(
        config,
        DryRunExecutionAdapter(config.mode),
        JsonlAuditLog(tmp_path / "audit.jsonl"),
    )
    result = harness.run(
        run_id="run-geo",
        as_of=now,
        market=_market(now),
        portfolio=PortfolioSnapshot(Decimal("100000")),
        geo_events=(event,),
    )
    assert result.geo.multiplier == Decimal("0.70")
    assert all(target.weight == Decimal("0.35") for target in result.targets)
