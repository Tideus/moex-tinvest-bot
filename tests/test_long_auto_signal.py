import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_shadow import FakeMarketData

from moex_bot.config import load_config
from moex_bot.integrations.algopack_intraday import IntradaySuperCandle
from moex_bot.long_auto_signal import select_candidate
from moex_bot.shadow import load_universe, run_hourly_shadow

CONFIG = Path(__file__).resolve().parents[1] / "config"
AT = datetime(2026, 11, 2, 7, 5, tzinfo=UTC)


@pytest.fixture
def decision(tmp_path):
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(
        json.dumps(
            dict(
                account_id="test-long",
                source="t_invest_sandbox",
                positions={},
                cash="300000",
                open_orders=0,
                blocked_cash="0",
                daily_turnover="0",
            )
        )
    )
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps({"feed_observed_at": AT.isoformat(), "events": []}))
    plan = tmp_path / "plan.json"
    run_hourly_shadow(
        config=load_config(CONFIG / "shadow.json"),
        universe=tuple(x for x in load_universe(CONFIG / "universe.json") if x.secid == "SBER"),
        portfolio_path=portfolio,
        geo_path=geo,
        output_path=plan,
        as_of=AT,
        market_data=FakeMarketData(),
        long_flow=(IntradaySuperCandle("SBER", AT, *[Decimal(1)] * 10),),
    )
    return dict(
        plan=plan,
        portfolio=portfolio,
        now=AT,
        universe=CONFIG / "universe.json",
        strategy_config=CONFIG / "shadow.json",
        policy=CONFIG / "prod_long.json",
        environment="sandbox",
        account_id="test-long",
    )


def test_first_actual_harness_decision_only_one_lot(decision):
    record = json.loads(decision["plan"].read_bytes())["orders"][0]
    assert record["intent"]["lots"] > 1
    c = select_candidate(**decision)
    assert c and c.lots == 1 and c.instrument.secid == "SBER"
    assert c.stop_trigger == Decimal("107.80") and c.limit_price == 110
    assert c.order_request_id != c.stop_request_id and select_candidate(**decision) == c


def test_no_valid_lower_tick_means_no_entry(decision):
    raw = json.loads(decision["plan"].read_bytes())
    order = raw["orders"][0]["intent"]
    order["instrument"]["tick_size"] = order["limit_price"]
    decision["plan"].write_text(json.dumps(raw))
    assert select_candidate(**decision) is None


@pytest.mark.parametrize(
    "case",
    [
        "quality",
        "rebalance",
        "flow",
        "target",
        "signal",
        "status",
        "side",
        "margin",
        "type",
        "manual",
        "open",
        "protected",
        "geo",
        "cash",
    ],
)
def test_no_candidate(decision, case, tmp_path):
    import hashlib

    plan = json.loads(decision["plan"].read_bytes())
    snapshot = json.loads(decision["portfolio"].read_bytes())
    if case == "quality":
        plan["quality"]["passed"] = False
    elif case == "rebalance":
        plan["rebalance_allowed"] = False
    elif case == "flow":
        plan["flow_diagnostics"][0]["passed"] = False
    elif case == "target":
        plan["targets"] = []
    elif case == "signal":
        plan["signal_diagnostics"][0]["eligible"] = False
    elif case == "status":
        plan["orders"][0]["status"] = "rejected"
    elif case in ("side", "margin", "type"):
        plan["orders"][0]["intent"].update(
            {
                "side": {"side": "sell"},
                "margin": {"confirm_margin_trade": True},
                "type": {"order_type": "market"},
            }[case]
        )
    elif case in ("open", "manual", "cash"):
        snapshot.update(
            {
                "open": {"open_orders": 1},
                "manual": {"positions": {"SBER": 10}},
                "cash": {"cash": "0"},
            }[case]
        )
        decision["portfolio"].write_text(json.dumps(snapshot))
        plan["portfolio_input_sha256"] = hashlib.sha256(
            decision["portfolio"].read_bytes()
        ).hexdigest()
    elif case == "protected":
        rules = json.loads(decision["policy"].read_bytes())
        rules["protected_secids"].append("SBER")
        rules["allowed_secids"].remove("SBER")
        rules["allowed_secids"].append("TEST")
        decision["policy"] = tmp_path / "rules.json"
        decision["policy"].write_text(json.dumps(rules))
    else:
        plan["geo"]["multiplier"] = "0"
    decision["plan"].write_text(json.dumps(plan))
    assert select_candidate(**decision) is None


@pytest.mark.parametrize(
    "case",
    [
        "future",
        "expired",
        "account",
        "cross",
        "hash",
        "config",
        "identity",
        "nonce",
        "amount",
        "run",
        "naive",
        "profile",
    ],
)
def test_corrupt_stale_and_cross_contour_rejected(decision, case):
    p = json.loads(decision["plan"].read_bytes())
    if case in ("future", "expired", "naive"):
        decision["now"] = {
            "future": AT - timedelta(seconds=1),
            "expired": AT + timedelta(minutes=5),
            "naive": AT.replace(tzinfo=None),
        }[case]
    elif case == "account":
        decision["account_id"] = "wrong"
    elif case in ("profile", "cross"):
        decision["environment"] = "prod-preview" if case == "cross" else "prod"
    elif case == "hash":
        p["portfolio_input_sha256"] = "bad"
    elif case == "config":
        p["config_snapshot"]["allow_margin"] = True
    elif case == "run":
        p["run_id"] = "intraday"
    elif case == "identity":
        p["orders"][0]["intent"]["instrument"]["uid"] = "bad"
    else:
        p["orders"][0]["intent"]["order_request_id" if case == "nonce" else "notional"] = "0"
    decision["plan"].write_text(json.dumps(p))
    with pytest.raises(ValueError):
        select_candidate(**decision)
