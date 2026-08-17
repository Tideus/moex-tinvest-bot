import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from moex_bot.cli import (
    _load_local_env,
    build_parser,
    environment_set,
    geo_refresh,
    integration_preflight,
    preflight,
    replay,
    session_check,
)
from moex_bot.service_config import TInvestEnvironment

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_replay_cli_writes_result_and_audit(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    status = replay(
        PROJECT_ROOT / "config" / "replay.json",
        PROJECT_ROOT / "examples" / "replay_snapshot.json",
        output,
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    assert status == 0
    assert result["quality"]["passed"] is True
    assert output.with_suffix(".audit.jsonl").exists()


def test_preflight_accepts_shadow_configuration() -> None:
    status = preflight(PROJECT_ROOT / "config" / "shadow.json")
    assert status == 0


def test_parser_accepts_snapshot_alias() -> None:
    args = build_parser().parse_args(
        [
            "replay",
            "--config",
            "config/replay.json",
            "--snapshot",
            "examples/replay_snapshot.json",
        ]
    )
    assert args.input == Path("examples/replay_snapshot.json")


def test_integration_preflight_accepts_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Credentials are optional for deterministic replay and must never be echoed.
    monkeypatch.delenv("MOEX_APIKEY", raising=False)
    monkeypatch.delenv("T_INVEST_SANDBOX_TOKEN", raising=False)
    monkeypatch.delenv("T_INVEST_SANDBOX_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("T_INVEST_PROD_TOKEN", raising=False)
    monkeypatch.delenv("T_INVEST_PROD_ACCOUNT_ID", raising=False)
    assert integration_preflight() == 0


def test_integration_preflight_enforces_required_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOEX_APIKEY", raising=False)
    assert integration_preflight(required=("moex_algopack",)) == 2


@pytest.mark.parametrize(
    ("active", "inactive_token", "inactive_account"),
    [
        ("sandbox", "T_INVEST_PROD_TOKEN", "T_INVEST_PROD_ACCOUNT_ID"),
        ("prod", "T_INVEST_SANDBOX_TOKEN", "T_INVEST_SANDBOX_ACCOUNT_ID"),
    ],
)
def test_integration_preflight_ignores_incomplete_inactive_contour(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active: str,
    inactive_token: str,
    inactive_account: str,
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"t_invest_environment": active}), encoding="utf-8")
    for name in (
        "T_INVEST_SANDBOX_TOKEN",
        "T_INVEST_SANDBOX_ACCOUNT_ID",
        "T_INVEST_PROD_TOKEN",
        "T_INVEST_PROD_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(inactive_token, "unused-token")
    monkeypatch.delenv(inactive_account, raising=False)
    assert integration_preflight(runtime_path=runtime) == 0


@pytest.mark.parametrize(
    ("active", "token_name", "account_name"),
    [
        ("sandbox", "T_INVEST_SANDBOX_TOKEN", "T_INVEST_SANDBOX_ACCOUNT_ID"),
        ("prod", "T_INVEST_PROD_TOKEN", "T_INVEST_PROD_ACCOUNT_ID"),
    ],
)
def test_integration_preflight_rejects_incomplete_active_contour(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active: str,
    token_name: str,
    account_name: str,
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"t_invest_environment": active}), encoding="utf-8")
    monkeypatch.setenv(token_name, "active-token")
    monkeypatch.delenv(account_name, raising=False)
    assert integration_preflight(runtime_path=runtime) == 2


def test_session_check_returns_skip_outside_market_window() -> None:
    assert session_check(datetime(2026, 8, 14, 21, 30, tzinfo=UTC)) == 3


def test_geo_refresh_with_bad_config_fails_closed(tmp_path: Path) -> None:
    sources = tmp_path / "sources.json"
    sources.write_text("[]", encoding="utf-8")
    assert (
        geo_refresh(
            sources_path=sources,
            output_path=tmp_path / "geo.json",
            as_of=datetime(2026, 8, 14, 10, tzinfo=UTC),
        )
        == 2
    )


def test_environment_set_writes_explicit_switch(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    assert environment_set(runtime_path=runtime, environment=TInvestEnvironment.PROD) == 0
    raw = json.loads(runtime.read_text(encoding="utf-8"))
    assert raw["t_invest_environment"] == "prod"
    assert raw["schedule"]["timezone"] == "Europe/Moscow"
    assert raw["schedule"]["diagnostics_interval_seconds"] == 60


def test_local_env_loader_is_optional() -> None:
    _load_local_env()
