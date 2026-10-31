import json
import runpy
import sys
from pathlib import Path

import pytest

from moex_bot import prod_experiment as module

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config/prod_experiment.json"


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    target = tmp_path / "settings.json"
    target.write_bytes(TEMPLATE.read_bytes())
    return target


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("enabled", True),
        ("enabled", 1),
        ("mode", "autonomous"),
        ("stage", "active"),
        ("stage", []),
        ("admission", "relative.json"),
        ("admission", "/var/lib/moex-tinvest-bot/execution/prod-orders.sqlite3"),
        ("account_id_env", "T_INVEST_SANDBOX_ACCOUNT_ID"),
        ("automatic_entries", True),
        ("protection_required", False),
        ("protect_manual_positions", False),
        ("allow_margin", True),
        ("allow_short", True),
        ("allowed_asset_classes", ["future"]),
        ("capital_limit_rub", 40000),
        ("capital_limit_rub", "x"),
        ("capital_limit_rub", "NaN"),
        ("capital_limit_rub", "Infinity"),
        ("capital_limit_rub", "-1"),
        ("capital_limit_rub", "0"),
        ("capital_limit_rub", "40001"),
        ("max_order_notional_rub", "10001"),
        ("max_daily_turnover_rub", "20001"),
        ("max_daily_loss_rub", "601"),
        ("max_attempts_per_day", True),
        ("max_attempts_per_day", 0),
        ("max_attempts_per_day", 3),
        ("maximum_first_entry_lots", 2),
        ("journal", None),
        ("journal", "relative.sqlite3"),
        ("journal", "/tmp/order.sqlite3"),
        ("journal", "/var/lib/moex-tinvest-bot/execution/../orders.sqlite3"),
        ("journal", "/var/lib/moex-tinvest-bot/execution/orders.json"),
        ("protection_journal", "/var/lib/moex-tinvest-bot/execution/prod-orders.sqlite3"),
    ],
)
def test_unsafe_experiment_configuration_fails_closed(
    profile: Path,
    field: str,
    value: object,
) -> None:
    raw = json.loads(profile.read_text())
    raw[field] = value
    profile.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        module.load_experiment_settings(profile)


@pytest.mark.parametrize("raw", [[], {"version": 1}])
def test_nonobject_or_incomplete_settings_rejected(profile: Path, raw: object) -> None:
    profile.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        module.load_experiment_settings(profile)


@pytest.mark.parametrize("enabled", [False, True])
def test_supervised_stage_is_valid_but_readiness_is_not_admission(
    profile: Path, enabled: bool
) -> None:
    raw = json.loads(profile.read_text())
    raw.update(stage="supervised_canary", enabled=enabled)
    profile.write_text(json.dumps(raw))
    assert module.load_experiment_settings(profile)["enabled"] is enabled
    assert module.readiness_report(ROOT, profile)["ready_for_orders"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"capital_limit_rub": "5000"},
        {"capital_limit_rub": "15000", "max_order_notional_rub": "1000"},
        {"capital_limit_rub": "20000", "max_daily_loss_rub": "400"},
    ],
)
def test_limits_scale_conservatively_with_capital(profile: Path, changes: dict) -> None:
    raw = json.loads(profile.read_text())
    raw.update(changes)
    profile.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="inconsistent"):
        module.load_experiment_settings(profile)


@pytest.mark.parametrize(
    "policy",
    [
        [],
        {"manual_position_policy": "sell"},
        {"manual_position_policy": "protect", "protected_secids": []},
        {"manual_position_policy": "protect", "protected_secids": [None]},
        {"manual_position_policy": "protect", "protected_secids": [" "]},
        {"manual_position_policy": "protect", "protected_secids": "SBER"},
    ],
)
def test_readiness_requires_explicit_manual_position_protection(
    tmp_path: Path,
    profile: Path,
    policy: object,
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/prod_long.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        module.readiness_report(tmp_path, profile)


def test_committed_profile_is_read_only_and_does_not_claim_evidence(profile: Path) -> None:
    before = profile.read_bytes()
    report = module.readiness_report(ROOT, profile)
    assert not report["ready_for_orders"]
    assert not report["api_called"] and not report["order_submitted"]
    assert report["limits"]["maximum_first_entry_lots"] == 1
    assert report["limits"]["max_daily_loss_rub"] == "600"
    assert report["required_evidence"]["native_protection"] == "NOT_VERIFIED_BY_THIS_REPORT"
    assert profile.read_bytes() == before


@pytest.mark.parametrize("explicit", [True, False])
def test_readonly_cli_emits_safe_preparation_report(
    profile: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    explicit: bool,
) -> None:
    argv = ["experiment", "--root", str(ROOT)]
    if explicit:
        argv += ["--settings", str(profile)]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["ready_for_orders"] is False


def test_readonly_cli_invalid_input_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(sys, "argv", ["experiment", "--root", str(tmp_path)])
    assert module.main() == 2
    assert json.loads(capsys.readouterr().out)["ready_for_orders"] is False


def test_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["experiment", "--root", str(ROOT)])
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as result:
        runpy.run_module("moex_bot.prod_experiment", run_name="__main__")
    assert result.value.code == 0
