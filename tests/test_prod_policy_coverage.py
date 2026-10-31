import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.prod_long import (
    _coerce_int,
    load_prod_long_policy,
    prepare_prod_long_sleeve,
    validate_prod_long_policy,
    write_prod_long_sleeve,
)

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("execution_stage", "live"),
        ("account_id_env", "bad-name"),
        ("account_id_env", "OTHER_ACCOUNT"),
        ("account_type", "broker"),
        ("manual_position_policy", "sell"),
        ("protected_secids", frozenset({"SBER"})),
        ("managed_secids", frozenset({"GAZP"})),
        ("allowed_secids", frozenset()),
    ],
)
def test_prod_policy_boundaries(field, value):
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    with pytest.raises(ValueError):
        validate_prod_long_policy(replace(policy, **{field: value}))


def test_protected_and_managed_cannot_overlap():
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    with pytest.raises(ValueError, match="protected and managed"):
        validate_prod_long_policy(replace(policy, managed_secids=policy.protected_secids))
    with pytest.raises(ValueError, match="must belong"):
        validate_prod_long_policy(replace(policy, managed_secids=frozenset({"UNLISTED"})))


@pytest.mark.parametrize(
    "field,value",
    [
        ("capital_limit_rub", D("0")),
        ("max_order_notional_rub", D("Infinity")),
        ("max_daily_turnover_rub", D("0")),
        ("max_orders_per_day", 0),
        ("max_account_daily_loss_weight", D("0.03")),
        ("allow_margin", True),
        ("allowed_asset_classes", ("future",)),
        ("capital_limit_rub", D("1")),
        ("max_daily_turnover_rub", D("1")),
    ],
)
def test_canary_risk_contract(field, value):
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    with pytest.raises(ValueError):
        validate_prod_long_policy(replace(policy, canary=replace(policy.canary, **{field: value})))


@pytest.mark.parametrize(
    "field,value",
    [
        ("capital_limit_rub", D("40001")),
        ("max_order_notional_rub", D("10001")),
        ("max_daily_turnover_rub", D("20001")),
        ("max_orders_per_day", 3),
    ],
)
def test_live_canary_hard_limits(field, value):
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    with pytest.raises(ValueError, match="hard limits"):
        validate_prod_long_policy(
            replace(
                policy, execution_stage="canary", canary=replace(policy.canary, **{field: value})
            )
        )


@pytest.mark.parametrize(
    "location,value",
    [
        ((), []),
        (("canary",), []),
        (("version",), True),
        (("protected_secids",), ["GAZP", "GAZP"]),
        (("allowed_secids",), ["sber"]),
        (("allowed_secids",), [False]),
        (("allowed_secids",), {}),
        (("canary", "capital_limit_rub"), "notdecimal"),
        (("canary", "allow_margin"), "false"),
    ],
)
def test_prod_policy_parse_boundaries(tmp_path, location, value):
    raw = json.loads((ROOT / "config/prod_long.json").read_text())
    if not location:
        raw = value
    else:
        current = raw
        for key in location[:-1]:
            current = current[key]
        current[location[-1]] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_prod_long_policy(path)


@pytest.mark.parametrize("value", [True, "1.5", None])
def test_position_lots_must_be_integer(value):
    with pytest.raises(ValueError):
        _coerce_int(value, "lots")


def test_observer_sleeve_writes_only_managed_holdings(tmp_path):
    raw = json.loads((ROOT / "config/prod_long.json").read_text())
    raw["managed_secids"] = ["SBER"]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(raw))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = dict(
        source="t_invest_prod",
        account_id="iis",
        cash="1000",
        positions={"SBER": {"lots": 1}, "GAZP": 1, "T": 0},
    )
    snapshot_path.write_text(json.dumps(snapshot))
    output = tmp_path / "output" / "sleeve.json"
    result = write_prod_long_sleeve(
        policy_path=policy_path,
        broker_snapshot_path=snapshot_path,
        output_path=output,
        expected_account_id="iis",
    )
    assert result["positions"] == {"SBER": {"lots": 1}}
    assert json.loads(output.read_text())["cash"] == "1000"
    snapshot_path.write_text("[]")
    with pytest.raises(ValueError, match="must be an object"):
        write_prod_long_sleeve(
            policy_path=policy_path,
            broker_snapshot_path=snapshot_path,
            output_path=output,
            expected_account_id="iis",
        )


def test_observer_requires_observer_stage_and_mapping_positions():
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    with pytest.raises(ValueError, match="observer stage"):
        prepare_prod_long_sleeve(
            policy=replace(policy, execution_stage="canary"),
            broker_snapshot={},
            expected_account_id="iis",
        )
    with pytest.raises(ValueError, match="positions must"):
        prepare_prod_long_sleeve(
            policy=policy,
            broker_snapshot=dict(source="t_invest_prod", account_id="iis", positions=[]),
            expected_account_id="iis",
        )
