import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.config import load_config
from moex_bot.domain import ExecutionMode, Instrument, MarketObservation
from moex_bot.shadow import (
    load_geo_feed,
    load_portfolio,
    load_universe,
    run_hourly_shadow,
    select_allowed_universe,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "field,value",
    [
        ("secid", ""),
        ("board", ""),
        ("lot_size_verified", 0),
        ("api_trade_available", False),
        ("issuer_id", ""),
        ("sector", ""),
        ("risk_cluster", ""),
        ("asset_class", "future"),
    ],
)
def test_universe_identity_contract(field, value):
    entry = load_universe(ROOT / "config/universe.json")[0]
    with pytest.raises(ValueError):
        replace(entry, **{field: value}).validate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_trade_available", "true"),
        ("short_enabled_verified", "false"),
    ],
)
def test_universe_requires_json_boolean(tmp_path, field, value):
    raw = json.loads((ROOT / "config/universe.json").read_text())
    raw[0][field] = value
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="JSON boolean"):
        load_universe(path)


def test_universe_rejects_duplicate_uid_and_missing_allowlist(tmp_path):
    raw = json.loads((ROOT / "config/universe.json").read_text())
    raw[1]["t_invest_uid"] = raw[0]["t_invest_uid"]
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="duplicate T-Invest UID"):
        load_universe(path)
    with pytest.raises(ValueError, match="not fully present"):
        select_allowed_universe((), frozenset({"SBER"}))


@pytest.mark.parametrize("timestamp", ["2026-10-30T10:00:00", "2027-01-01T00:00:00+00:00"])
def test_geo_feed_rejects_naive_or_future_timestamp(tmp_path, timestamp):
    path = tmp_path / "geo.json"
    path.write_text(json.dumps(dict(feed_observed_at=timestamp, events=[])))
    with pytest.raises(ValueError):
        load_geo_feed(path, as_of=NOW)


def test_geo_feed_parses_event_and_portfolio_legacy_positions(tmp_path):
    path = tmp_path / "geo.json"
    path.write_text(
        json.dumps(
            dict(
                feed_observed_at=NOW.isoformat(),
                events=[
                    dict(
                        event_id="event",
                        severity=2,
                        confidence="1",
                        source_tier="primary",
                        confirmed=True,
                        affected_secids=["SBER"],
                        observed_at=NOW.isoformat(),
                    )
                ],
            )
        )
    )
    events, stale = load_geo_feed(path, as_of=NOW)
    assert len(events) == 1 and not stale
    instrument = Instrument("SBER", "uid", "TQBR", 10, Decimal("0.01"))
    observation = MarketObservation(
        instrument, Decimal(100), Decimal(90), Decimal("0.1"), Decimal(0), NOW, True, True
    )
    path.write_text(json.dumps(dict(cash="1000", positions={"SBER": 2})))
    portfolio = load_portfolio(path, {"SBER": observation})
    assert portfolio.positions["SBER"].lots == 2
    path.write_text(json.dumps(dict(cash="1000", positions={"OTHER": 2})))
    with pytest.raises(ValueError, match="outside verified"):
        load_portfolio(path, {"SBER": observation})


def test_shadow_refuses_wrong_mode_and_lot_mismatch(tmp_path):
    config = load_config(ROOT / "config/shadow.json")
    entry = load_universe(ROOT / "config/universe.json")[0]

    class WrongLotData:
        def hourly_observation(self, **kwargs):
            instrument = Instrument(
                entry.secid,
                entry.t_invest_uid,
                entry.board,
                entry.lot_size_verified + 1,
                Decimal("0.01"),
            )
            return MarketObservation(
                instrument, Decimal(100), Decimal(90), Decimal("0.1"), Decimal(0), NOW, True, True
            )

    kwargs = dict(
        config=config,
        universe=(entry,),
        portfolio_path=tmp_path / "portfolio.json",
        geo_path=tmp_path / "geo.json",
        output_path=tmp_path / "out.json",
        as_of=NOW,
        market_data=WrongLotData(),
    )
    with pytest.raises(ValueError, match="requires mode=shadow"):
        run_hourly_shadow(**(kwargs | {"config": replace(config, mode=ExecutionMode.REPLAY)}))
    with pytest.raises(ValueError, match="lot mismatch"):
        run_hourly_shadow(**kwargs)
