import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from moex_bot.config import BotConfig, StrategyConfig
from moex_bot.domain import ExecutionMode, Instrument, MarketObservation
from moex_bot.shadow import UniverseEntry, load_geo_feed, load_universe, run_hourly_shadow


class FakeMarketData:
    def hourly_observation(
        self,
        *,
        secid: str,
        uid: str,
        board: str,
        as_of: datetime,
        trend_window: int = 20,
        momentum_window: int = 5,
        lookback_days: int = 30,
    ) -> MarketObservation:
        return MarketObservation(
            Instrument(secid, uid, board, 1, Decimal("0.01")),
            Decimal("110"),
            Decimal("100"),
            Decimal("0.10"),
            Decimal("0.20"),
            as_of - timedelta(minutes=5),
            True,
            True,
        )


def _config() -> BotConfig:
    return BotConfig(
        ExecutionMode.SHADOW,
        "RUB",
        3900,
        Decimal("0.15"),
        Decimal("10000"),
        Decimal("0.8"),
        Decimal("30000"),
        Decimal("500"),
        5,
        False,
        False,
        StrategyConfig(5, Decimal("0"), True),
    )


def test_missing_geo_timestamp_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "geo.json"
    path.write_text('{"feed_observed_at": null, "events": []}', encoding="utf-8")
    events, stale = load_geo_feed(path, as_of=datetime.now(UTC))
    assert events == ()
    assert stale


def test_load_universe_validates_uuid_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "universe.json"
    path.write_text(
        json.dumps(
            [
                {
                    "secid": "SBER",
                    "board": "TQBR",
                    "t_invest_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "lot_size_verified": 1,
                    "api_trade_available": True,
                    "issuer_id": "sberbank",
                    "sector": "financials",
                    "risk_cluster": "domestic_financial",
                    "asset_class": "share",
                }
            ]
        ),
        encoding="utf-8",
    )
    universe = load_universe(path)
    assert universe[0].secid == "SBER"
    assert universe[0].lot_size_verified == 1
    assert universe[0].sector == "financials"


def test_hourly_shadow_reduces_risk_when_geo_feed_is_stale(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.json"
    geo = tmp_path / "geo.json"
    output = tmp_path / "result.json"
    portfolio.write_text(
        '{"cash": "100000", "positions": {}, "daily_turnover": "0", "open_orders": 0}',
        encoding="utf-8",
    )
    geo.write_text('{"feed_observed_at": null, "events": []}', encoding="utf-8")
    result = run_hourly_shadow(
        config=_config(),
        universe=(
            UniverseEntry(
                "SBER",
                "TQBR",
                "e6123145-9665-43e0-8413-cd61b8aa9b13",
                1,
                True,
                "sberbank",
                "financials",
                "domestic_financial",
                "share",
            ),
        ),
        portfolio_path=portfolio,
        geo_path=geo,
        output_path=output,
        as_of=datetime.now(UTC),
        market_data=FakeMarketData(),
    )
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert result.quality.passed
    assert result.geo.multiplier == Decimal("0.70")
    assert result.targets[0].weight == Decimal("0.1050")
    assert persisted["geo"]["level"] == "elevated"
    assert persisted["market"][0]["instrument"]["secid"] == "SBER"
    assert persisted["portfolio_input"]["cash"] == "100000"
    assert persisted["config_snapshot"]["strategy"]["top_n"] == 5
    assert output.with_suffix(".audit.jsonl").exists()
