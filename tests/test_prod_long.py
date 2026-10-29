import json
from pathlib import Path

import pytest

from moex_bot.prod_long import load_prod_long_policy, prepare_prod_long_sleeve
from moex_bot.shadow import load_universe, select_allowed_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _snapshot() -> dict[str, object]:
    return {
        "source": "t_invest_prod",
        "account_id": "prod-iis",
        "cash": "41302.62",
        "blocked_cash": "0",
        "reported_equity": "160033.12",
        "daily_turnover": "0",
        "open_orders": 0,
        "positions": {
            "LKOH": {"lots": 5, "blocked_lots": 0},
            "GAZP": {"lots": 30, "blocked_lots": 0},
            "RAGR": {"lots": 300, "blocked_lots": 0},
            "TATNP": {"lots": 50, "blocked_lots": 0},
        },
    }


def test_prod_observer_caps_virtual_sleeve_and_protects_manual_positions() -> None:
    policy = load_prod_long_policy(PROJECT_ROOT / "config" / "prod_long.json")
    result = prepare_prod_long_sleeve(
        policy=policy,
        broker_snapshot=_snapshot(),
        expected_account_id="prod-iis",
    )
    assert result["source"] == "t_invest_prod_observer"
    assert result["cash"] == "40000"
    assert result["reported_equity"] == "40000"
    assert result["positions"] == {}
    assert result["observer"] == {
        "execution_stage": "observer",
        "real_orders_enabled": False,
        "account_type": "ACCOUNT_TYPE_TINKOFF_IIS",
        "capital_limit_rub": "40000",
        "protected_secids": ["GAZP", "LKOH", "RAGR", "TATNP"],
        "managed_secids": [],
        "allowed_secids": sorted(policy.allowed_secids),
    }
    assert len(policy.allowed_secids) == 20
    assert not (policy.protected_secids & policy.allowed_secids)


def test_prod_observer_rejects_unclassified_real_position() -> None:
    policy = load_prod_long_policy(PROJECT_ROOT / "config" / "prod_long.json")
    snapshot = _snapshot()
    positions = dict(snapshot["positions"])  # type: ignore[arg-type]
    positions["SBER"] = {"lots": 1, "blocked_lots": 0}
    snapshot["positions"] = positions
    with pytest.raises(ValueError, match="explicit protected/managed classification"):
        prepare_prod_long_sleeve(
            policy=policy,
            broker_snapshot=snapshot,
            expected_account_id="prod-iis",
        )


def test_prod_policy_rejects_any_execution_stage(tmp_path: Path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "prod_long.json").read_text())
    raw["execution_stage"] = "live"
    candidate = tmp_path / "prod_long.json"
    candidate.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="production execution is not enabled"):
        load_prod_long_policy(candidate)


def test_prod_policy_allows_two_orders_but_rejects_excessive_count(
    tmp_path: Path,
) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "prod_long.json").read_text())
    policy = load_prod_long_policy(PROJECT_ROOT / "config" / "prod_long.json")
    assert policy.canary.max_orders_per_day == 2
    raw["canary"]["max_orders_per_day"] = 5
    candidate = tmp_path / "prod_long.json"
    candidate.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="one to four"):
        load_prod_long_policy(candidate)


def test_prod_policy_rejects_protected_in_trading_allowlist(tmp_path: Path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "prod_long.json").read_text())
    raw["allowed_secids"][0] = "GAZP"
    candidate = tmp_path / "prod_long.json"
    candidate.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="protected and allowed"):
        load_prod_long_policy(candidate)


def test_prod_strategy_universe_is_exactly_the_twenty_allowed_shares() -> None:
    policy = load_prod_long_policy(PROJECT_ROOT / "config" / "prod_long.json")
    universe = load_universe(PROJECT_ROOT / "config" / "universe.json")
    selected = select_allowed_universe(universe, policy.allowed_secids)
    assert len(selected) == 20
    assert {item.secid for item in selected} == policy.allowed_secids
    assert not ({item.secid for item in selected} & policy.protected_secids)


def test_prod_observer_rejects_wrong_account_and_non_prod_snapshot() -> None:
    policy = load_prod_long_policy(PROJECT_ROOT / "config" / "prod_long.json")
    with pytest.raises(ValueError, match="configured IIS"):
        prepare_prod_long_sleeve(
            policy=policy,
            broker_snapshot=_snapshot(),
            expected_account_id="another-account",
        )
    snapshot = _snapshot()
    snapshot["source"] = "t_invest_sandbox"
    with pytest.raises(ValueError, match="production broker snapshot"):
        prepare_prod_long_sleeve(
            policy=policy,
            broker_snapshot=snapshot,
            expected_account_id="prod-iis",
        )
