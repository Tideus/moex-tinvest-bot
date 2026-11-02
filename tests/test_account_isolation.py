"""Two actual broker identities, not two labels for the same capital."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_sandbox_execution import FakeAdapter, _write_inputs

from moex_bot import cli
from moex_bot.account_profiles import AccountPurpose, load_account_registry
from moex_bot.execution_journal import ExecutionJournal
from moex_bot.runtime_config import RuntimeConfig, RuntimeSchedule
from moex_bot.sandbox_execution import execute_shadow_plan
from moex_bot.service_config import TInvestEnvironment

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = ROOT / "config/accounts.json"
LONG = "T_INVEST_SANDBOX_LONG_ACCOUNT_ID"
DAY = "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID"
LEGACY = "T_INVEST_SANDBOX_ACCOUNT_ID"
NOW = datetime(2026, 8, 18, 10, 1, tzinfo=UTC)
RUNTIME = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)


@pytest.mark.parametrize("missing", [True, False])
def test_no_adapter_or_cancel_or_submit_on_ambiguous_binding(monkeypatch, tmp_path, missing):
    monkeypatch.delenv(LEGACY, raising=False)
    monkeypatch.setenv(LONG, "Secret-Test-1")
    monkeypatch.setenv(DAY, "" if missing else " secret-test-1 ")
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "test")
    adapter, transport = Mock(), Mock()
    monkeypatch.setattr(cli.TInvestSandboxExecutionAdapter, "from_environment", adapter)
    monkeypatch.setattr(cli, "UrlLibJsonTransport", transport)
    monkeypatch.setattr(cli, "load_runtime_config", lambda _: RUNTIME)
    kw = dict(
        accounts_path=ACCOUNTS,
        portfolio_path=tmp_path,
        output_path=tmp_path,
        journal_path=tmp_path / "orders",
        outbox_path=None,
        as_of=NOW,
    )
    assert cli.sandbox_execute(**kw, shadow_path=tmp_path, runtime_path=tmp_path) == 2
    assert (
        cli.intraday_sandbox_execute(
            **kw, config_path=ROOT / "config/intraday.json", plan_path=tmp_path
        )
        == 2
    )
    for name in ("long", "intraday"):
        assert cli.sandbox_reconcile(accounts_path=ACCOUNTS, profile_id=name) == 2
    adapter.assert_not_called()
    transport.assert_not_called()
    assert not (tmp_path / "orders").exists()


def test_cli_preflight_no_ids_in_output(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bot", "sandbox-check-bindings", "--accounts", str(ACCOUNTS)])
    monkeypatch.delenv(LEGACY, raising=False)
    monkeypatch.setenv(LONG, "secret-A")
    monkeypatch.setenv(DAY, "secret-A")
    assert cli.main() == 2
    monkeypatch.setenv(DAY, "secret-B")
    assert cli.main() == 0
    assert "secret-" not in capsys.readouterr().out


def test_bootstrap_never_funds_duplicate_broker_result(monkeypatch, tmp_path):
    for key in (LONG, DAY, LEGACY):
        monkeypatch.delenv(key, raising=False)
    service = Mock()
    service.ensure_account.return_value = SimpleNamespace(account_id="duplicate", created=False)
    env_path = tmp_path / "bot.env"
    assert (
        cli.sandbox_bootstrap_profiles(
            env_path=env_path, accounts_path=ACCOUNTS, fund_targets=True, service=service
        )
        == 2
    )
    service.pay_in.assert_not_called()
    assert not env_path.exists()
    with pytest.raises(ValueError, match="different"):
        load_account_registry(ACCOUNTS).sandbox_bindings({LEGACY: "x", DAY: "x"}, bootstrap=True)


def test_profile_cannot_impersonate_other_purpose(tmp_path):
    data = json.loads(ACCOUNTS.read_text())
    data["profiles"][0]["profile_id"], data["profiles"][1]["profile_id"] = "intraday", "long"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="purpose"):
        load_account_registry(path)


@pytest.mark.parametrize("profile", list(AccountPurpose))
@pytest.mark.parametrize("fault", ["foreign_plan", "foreign_account", "no_evidence"])
def test_crossed_plan_rejected_without_journal_or_submission(tmp_path, profile, fault):
    plan, portfolio = _write_inputs(tmp_path)
    raw = json.loads(plan.read_text())
    prefix = "intraday" if profile is AccountPurpose.INTRADAY else "shadow"
    if fault == "foreign_plan":
        prefix = "shadow" if prefix == "intraday" else "intraday"
    raw["run_id"] = f"{prefix}-{NOW.isoformat()}"
    raw.pop("portfolio_input")
    container = raw if prefix == "shadow" else raw.setdefault("analysis_input", {})
    if fault != "no_evidence":
        container["portfolio_input"] = {
            "account_id": "other-account" if fault == "foreign_account" else FakeAdapter.account_id
        }
    plan.write_text(json.dumps(raw))
    adapter = Mock(account_id=FakeAdapter.account_id)
    journal = tmp_path / "orders.sqlite3"
    with pytest.raises(ValueError, match="target sandbox"):
        execute_shadow_plan(
            shadow_path=plan,
            portfolio_path=portfolio,
            output_path=tmp_path / "out",
            runtime=RUNTIME,
            adapter=adapter,
            as_of=NOW,
            journal_path=journal,
            profile=profile,
        )
    adapter.submit.assert_not_called()
    assert not journal.exists()


def test_identical_signal_ids_and_pending_orders_are_isolated_and_survive_restart(tmp_path):
    plan, portfolio = _write_inputs(tmp_path)
    raw = json.loads(plan.read_text())
    holdings = json.loads(portfolio.read_text())
    journal = tmp_path / "orders.sqlite3"
    for profile in (AccountPurpose.LONG, AccountPurpose.INTRADAY):
        account = f"fixture-{profile.value}"
        prefix = "shadow" if profile is AccountPurpose.LONG else "intraday"
        raw["run_id"] = f"{prefix}-{NOW.isoformat()}"
        evidence = raw if profile is AccountPurpose.LONG else raw.setdefault("analysis_input", {})
        evidence["portfolio_input"] = {"account_id": account}
        holdings["account_id"] = account
        plan.write_text(json.dumps(raw))
        portfolio.write_text(json.dumps(holdings))
        adapter = FakeAdapter()
        adapter.account_id = account
        args = dict(
            shadow_path=plan,
            portfolio_path=portfolio,
            output_path=tmp_path / "out",
            runtime=RUNTIME,
            adapter=adapter,
            as_of=NOW,
            journal_path=journal,
            profile=profile,
        )
        assert len(execute_shadow_plan(**args).submitted) == 1
        with pytest.raises(ValueError, match="pending/UNKNOWN"):
            execute_shadow_plan(**args)
    restored = ExecutionJournal(journal)
    long, day = (restored.unresolved(name)[0] for name in ("fixture-long", "fixture-intraday"))
    assert long["intent"]["order_request_id"] != day["intent"]["order_request_id"]
