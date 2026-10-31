"""CLI application orchestration exercised with isolated broker/data ports."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_tinvest_live import _intent

from moex_bot import cli
from moex_bot.config import load_config
from moex_bot.performance import TradeOperation
from moex_bot.service_config import TInvestEnvironment

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 30, 7, 5, tzinfo=UTC)


@pytest.mark.parametrize("passed,safe", [(True, False), (False, False), (False, True)])
@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("allowed_policy", [True, False])
def test_hourly_shadow_orchestrates_flow_quality_and_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passed: bool,
    safe: bool,
    notify: bool,
    allowed_policy: bool,
) -> None:
    config = load_config(ROOT / "config/shadow.json")
    config = replace(
        config,
        strategy=replace(
            config.strategy, flow_confirmation_enabled=True, rebalance_hours_moscow=(10,)
        ),
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    result = SimpleNamespace(
        run_id="test",
        quality=SimpleNamespace(passed=passed),
        geo=SimpleNamespace(level=SimpleNamespace(value="normal")),
        targets=(),
        orders=(),
    )
    monkeypatch.setattr(cli, "run_hourly_shadow", lambda **kwargs: result)
    monkeypatch.setattr(cli.MoexAlgoReadOnlyAdapter, "from_environment", lambda **kwargs: Mock())
    flow = Mock()
    monkeypatch.setattr(cli.AlgoPackIntradayAdapter, "from_environment", lambda: flow)
    monkeypatch.setattr(cli, "should_send_long_morning", lambda *args: notify)
    monkeypatch.setattr(cli, "render_shadow_report", lambda *args, **kwargs: "report")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    result_code = cli.hourly_shadow(
        config_path=ROOT / "config/shadow.json",
        universe_path=ROOT / "config/universe.json",
        portfolio_path=tmp_path / "portfolio.json",
        geo_path=tmp_path / "geo.json",
        output_path=tmp_path / "shadow.json",
        as_of=NOW,
        require_token=False,
        outbox_path=tmp_path / "outbox.sqlite3",
        notifications_path=ROOT / "config/notifications.json",
        report_environment="PROD OBSERVER",
        allowed_secids_policy_path=ROOT / "config/prod_long.json" if allowed_policy else None,
        quality_block_is_safe=safe,
    )
    assert result_code == (0 if passed or safe else 2)
    assert outbox.enqueue.call_count == int(notify)
    flow.latest.assert_called_once()


def test_quality_safe_flag_cannot_hide_sandbox_failure(tmp_path: Path) -> None:
    assert (
        cli.hourly_shadow(
            config_path=tmp_path,
            universe_path=tmp_path,
            portfolio_path=tmp_path,
            geo_path=tmp_path,
            output_path=tmp_path,
            as_of=NOW,
            require_token=False,
            quality_block_is_safe=True,
        )
        == 2
    )


@pytest.mark.parametrize("metadata", [True, False])
def test_prod_sleeve_cli_validates_returned_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: bool,
) -> None:
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "fake-iis")
    monkeypatch.setattr(
        cli,
        "write_prod_long_sleeve",
        lambda **kwargs: {
            "reported_equity": "40000",
            "observer": {"protected_secids": ["SBER"]} if metadata else [],
        },
    )
    assert cli.prod_long_prepare(
        policy_path=tmp_path, broker_snapshot_path=tmp_path, output_path=tmp_path
    ) == (0 if metadata else 2)


@pytest.mark.parametrize("cycles,outbox_enabled", [(0, True), (1, True), (1, False)])
def test_daily_report_writes_and_only_enqueues_existing_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cycles: int,
    outbox_enabled: bool,
) -> None:
    summary = SimpleNamespace(cycles=cycles, rows=[])
    monkeypatch.setattr(cli, "summarize_shadow_artifacts", lambda *args, **kwargs: summary)
    monkeypatch.setattr(cli, "render_daily_shadow_report", lambda x: "daily report")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    output = tmp_path / "report.txt"
    assert (
        cli.daily_trade_report(
            artifacts_dir=tmp_path,
            report_date=NOW.date(),
            timezone="Europe/Moscow",
            output_path=output,
            outbox_path=tmp_path / "outbox.sqlite3" if outbox_enabled else None,
            as_of=NOW,
        )
        == 0
    )
    assert output.read_text(encoding="utf-8").strip() == "daily report"
    assert outbox.enqueue.call_count == int(cycles > 0 and outbox_enabled)


@pytest.mark.parametrize("fails", [True, False])
def test_telegram_delivery_reports_transport_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setattr(
        cli, "deliver_pending", lambda *args, **kwargs: (0 if fails else 1, int(fails))
    )
    monkeypatch.setattr(cli, "TelegramBotApiSender", lambda token: Mock())
    assert cli.telegram_send(outbox_path=tmp_path / "outbox.sqlite3", as_of=NOW, limit=1) == (
        2 if fails else 0
    )


@pytest.mark.parametrize("dead,pending", [(0, 0), (1, 0), (0, 2)])
def test_outbox_health_includes_error_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    dead: int,
    pending: int,
) -> None:
    outbox = Mock()
    outbox.health.return_value = {"dead": dead, "pending_due": pending}
    outbox.issues.return_value = [
        SimpleNamespace(last_error=None, status="dead", kind="report", dedupe_key="key", attempts=2)
    ]
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    assert cli.outbox_health(outbox_path=tmp_path, as_of=NOW, max_pending_due=1) == (
        2 if dead or pending > 1 else 0
    )
    if dead or pending > 1:
        assert "error=unknown" in capsys.readouterr().out


def test_outbox_negative_threshold_rejected(tmp_path: Path) -> None:
    assert cli.outbox_health(outbox_path=tmp_path, as_of=NOW, max_pending_due=-1) == 2


@pytest.mark.parametrize("healthy", [True, False])
def test_geo_refresh_surfaces_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    healthy: bool,
) -> None:
    monkeypatch.setattr(cli, "refresh_geo_feed", lambda **kwargs: healthy)
    assert cli.geo_refresh(sources_path=tmp_path, output_path=tmp_path, as_of=NOW) == (
        0 if healthy else 2
    )


@pytest.mark.parametrize("open_session", [True, False])
def test_session_check_distinguishes_skip(
    monkeypatch: pytest.MonkeyPatch, open_session: bool
) -> None:
    monkeypatch.setattr(cli, "is_conservative_stock_window", lambda moment: open_session)
    assert cli.session_check(NOW) == (0 if open_session else 3)


@pytest.mark.parametrize("fault", ["none", "security", "benchmark"])
@pytest.mark.parametrize("promoted", [True, False])
def test_backtest_cli_fetches_evidence_runs_base_and_stress_and_writes_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    promoted: bool,
) -> None:
    universe = cli.load_universe(ROOT / "config/universe.json")[:1]
    monkeypatch.setattr(cli, "load_universe", lambda path: universe)
    adapter = Mock()
    adapter.historical_daily_candles.side_effect = [
        [] if fault == "security" else [_intent()],
        [] if fault == "benchmark" else [_intent()],
    ]
    monkeypatch.setattr(cli.MoexAlgoReadOnlyAdapter, "from_environment", lambda **kwargs: adapter)
    run = Mock(
        return_value=SimpleNamespace(
            oos_metrics=SimpleNamespace(
                return_pct=Decimal("1"), benchmark_return_pct=Decimal("0.5")
            )
        )
    )
    monkeypatch.setattr(cli, "run_backtest", run)
    monkeypatch.setattr(cli, "assess_promotion", lambda *args: SimpleNamespace(passed=promoted))
    writer = Mock()
    monkeypatch.setattr(cli, "write_backtest_bundle", writer)
    result = cli.historical_backtest(
        strategy_config_path=ROOT / "config/shadow.json",
        backtest_config_path=ROOT / "config/backtest.json",
        promotion_gates_path=ROOT / "config/promotion_gates.json",
        universe_path=ROOT / "config/universe.json",
        output_dir=tmp_path,
        require_token=False,
        sandbox_weeks=0,
        reconciled_orders=0,
        unresolved_orders=0,
    )
    assert result == (0 if fault == "none" else 2)
    assert run.call_count == (2 if fault == "none" else 0)
    assert writer.call_count == int(fault == "none")
    if fault == "none":
        assert "market-data.json" in {path.name for path in tmp_path.iterdir()}


@pytest.mark.parametrize(
    "phase,passed,unavailable",
    [
        ("entry", True, False),
        ("force_flat", True, True),
        ("entry", False, True),
    ],
)
def test_intraday_plan_persists_analysis_inputs_and_broker_exit_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    passed: bool,
    unavailable: bool,
) -> None:
    adapter = Mock()
    if unavailable:
        adapter.latest.side_effect = OSError("data unavailable")
    else:
        adapter.latest.return_value = ()
    monkeypatch.setattr(cli.AlgoPackIntradayAdapter, "from_environment", lambda: adapter)
    plan = Mock(phase=phase, quality_passed=passed, signals=(), orders=(), run_id="test")
    monkeypatch.setattr(cli, "build_intraday_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "render_intraday_report", lambda plan: "plan")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text("{}")
    assert cli.intraday_plan(
        config_path=ROOT / "config/intraday.json",
        universe_path=ROOT / "config/universe.json",
        portfolio_path=portfolio,
        state_path=tmp_path / "state.sqlite3",
        output_path=tmp_path / "plan.json",
        as_of=NOW,
        outbox_path=tmp_path / "outbox.sqlite3",
    ) == (0 if passed else 2)
    assert plan.write.call_args.kwargs["analysis_input"]["supercandles"] == []
    assert "config_snapshot" in plan.write.call_args.kwargs["analysis_input"]
    assert outbox.enqueue.call_count == int(phase == "force_flat")


@pytest.mark.parametrize("environment", [TInvestEnvironment.PROD, TInvestEnvironment.SANDBOX])
def test_broker_snapshot_chooses_correct_read_paths_and_persists_turnover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: TInvestEnvironment,
) -> None:
    broker = SimpleNamespace(
        token="fake",
        account_id="fake-account",
        environment=environment,
        rest_endpoint="https://broker.invalid",
    )
    monkeypatch.setattr(cli, "resolve_tinvest_runtime", lambda *args, **kwargs: broker)
    service = Mock()
    service.operations.return_value = [
        TradeOperation("trade", NOW, "SBER", "BUY", 1, Decimal("100"), Decimal("1")),
        TradeOperation("deposit", NOW, None, None, 0, Decimal("1000"), Decimal("0")),
    ]
    service.broker_snapshot.return_value = SimpleNamespace(
        cash_available=Decimal("40000"),
        cash_blocked=Decimal("0"),
        reported_equity=Decimal("40000"),
        positions_lots={},
        open_orders=0,
        as_portfolio_payload=lambda **kwargs: {"daily_turnover": kwargs["daily_turnover"]},
    )
    monkeypatch.setattr(cli, "TInvestSandboxAccountService", lambda *args, **kwargs: service)
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: Mock())
    output = tmp_path / "portfolio.json"
    assert (
        cli.broker_portfolio_snapshot(
            universe_path=ROOT / "config/universe.json",
            output_path=output,
            runtime_path=ROOT / "config/runtime.json",
            services_path=ROOT / "config/services.json",
        )
        == 0
    )
    payload = json.loads(output.read_text())
    assert payload["daily_turnover"] == "100"
    assert len(payload["executed_operations"]) == 2
    assert "snapshot_started_at" in payload and "observed_at" in payload
    assert ("portfolio_path" in service.broker_snapshot.call_args.kwargs) == (
        environment is TInvestEnvironment.PROD
    )


@pytest.mark.parametrize("environment", [TInvestEnvironment.PROD, TInvestEnvironment.SANDBOX])
@pytest.mark.parametrize("fault", ["none", "snapshots", "data", "network"])
@pytest.mark.parametrize("weekly", [True, False])
@pytest.mark.parametrize("notify", [True, False])
def test_performance_report_reads_correct_contour_and_distinguishes_missing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: TInvestEnvironment,
    fault: str,
    weekly: bool,
    notify: bool,
) -> None:
    broker = SimpleNamespace(
        token="fake",
        account_id="fake",
        environment=environment,
        rest_endpoint="https://broker.invalid",
    )
    monkeypatch.setattr(cli, "resolve_tinvest_runtime", lambda *args, **kwargs: broker)
    service = Mock()
    service.operations.return_value = ()
    if fault == "network":
        service.operations.side_effect = OSError("fixture offline")
    monkeypatch.setattr(cli, "TInvestSandboxAccountService", lambda *args, **kwargs: service)
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: Mock())
    summarize = Mock(return_value=_intent())
    if fault in {"snapshots", "data"}:
        summarize.side_effect = ValueError(
            "requires at least two portfolio snapshots"
            if fault == "snapshots"
            else "inconsistent data"
        )
    monkeypatch.setattr(cli, "summarize_performance", summarize)
    monkeypatch.setattr(cli, "render_performance_report", lambda *args, **kwargs: "performance")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    output = tmp_path / "report.txt"
    result = cli.account_performance_report(
        artifacts_dir=tmp_path,
        start_date=NOW.date(),
        end_date=NOW.date(),
        timezone="Europe/Moscow",
        universe_path=ROOT / "config/universe.json",
        runtime_path=ROOT / "config/runtime.json",
        services_path=ROOT / "config/services.json",
        output_path=output,
        outbox_path=tmp_path / "outbox.sqlite3" if notify else None,
        weekly=weekly,
        as_of=NOW,
        notifications_path=ROOT / "config/notifications.json",
    )
    assert result == (0 if fault == "none" else 3 if fault == "snapshots" else 2)
    if fault == "none":
        assert output.read_text(encoding="utf-8").strip() == "performance"
        assert output.with_suffix(".json").exists()
        assert ("operations_path" in service.operations.call_args.kwargs) == (
            environment is TInvestEnvironment.PROD
        )
        assert outbox.enqueue.call_count == int(notify)


@pytest.mark.parametrize("unresolved", [0, 1])
def test_journal_reconciliation_cli_is_read_only_and_blocks_unknowns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unresolved: int,
) -> None:
    journal = tmp_path / "journal.sqlite3"
    journal.touch()
    monkeypatch.setattr(
        cli.TInvestSandboxExecutionAdapter, "from_environment", lambda **kwargs: Mock()
    )
    monkeypatch.setattr(cli, "reconcile_execution_journal", lambda *args: unresolved)
    assert cli.sandbox_journal_reconcile(
        accounts_path=ROOT / "config/accounts.json", profile_id="long", journal_path=journal
    ) == (2 if unresolved else 0)


@pytest.mark.parametrize("cancelled", [(), ("order-1",)])
def test_sandbox_cancel_reconcile_notifies_only_changes_using_profile_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: tuple,
) -> None:
    registry = cli.load_account_registry(ROOT / "config/accounts.json")
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fake")
    monkeypatch.setenv(registry.by_id("long").account_id_env, "long-fake")
    service = Mock()
    service.reconcile_cancel_active_orders.return_value = cancelled
    monkeypatch.setattr(cli, "TInvestSandboxAccountService", lambda *args, **kwargs: service)
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: Mock())
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    assert (
        cli.sandbox_reconcile(
            accounts_path=ROOT / "config/accounts.json",
            profile_id="long",
            outbox_path=tmp_path / "outbox.sqlite3",
        )
        == 0
    )
    service.reconcile_cancel_active_orders.assert_called_once_with("long-fake")
    assert outbox.enqueue.call_count == int(bool(cancelled))


@pytest.mark.parametrize(
    "issues,verify,candidate", [((), True, True), (("bad",), False, False), ((), False, False)]
)
def test_migration_cli_creates_review_only_inventory_without_granting_trading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issues: tuple,
    verify: bool,
    candidate: bool,
) -> None:
    artifacts = tmp_path / "state" / "artifacts"
    artifacts.mkdir(parents=True)
    inventory = SimpleNamespace(issues=issues, entries=("entry",), files=1, duplicates=0)
    monkeypatch.setattr(cli, "scan_legacy", lambda *args: inventory)
    monkeypatch.setattr(cli, "inventory_report", lambda inventory: {})
    journal = Mock()
    journal.unresolved_count.return_value = 0
    monkeypatch.setattr(cli, "stage_inventory", lambda *args: journal)
    monkeypatch.setattr(
        cli,
        "verify_inventory",
        lambda *args, **kwargs: [{"verification": "broker_response", "broker_status": "filled"}],
    )
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fake")
    report = tmp_path / "review" / "report.json"
    assert cli.sandbox_journal_prepare(
        accounts_path=ROOT / "config/accounts.json",
        artifacts_path=artifacts,
        report_path=report,
        candidate_path=tmp_path / "candidate.sqlite3" if candidate else None,
        verify_broker=verify,
    ) == (2 if issues else 0)
    assert report.exists()
    if verify:
        value = json.loads(report.read_text())
        assert value["broker_confirmed_orders"] == 1
        assert value["unverified_orders"] == 0


@pytest.mark.parametrize("profile", ["long", "intraday"])
@pytest.mark.parametrize("fault", ["none", "gate", "prod", "stopped", "empty"])
def test_sandbox_execution_handlers_gate_contour_and_route_durable_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    fault: str,
) -> None:
    actual_registry = cli.load_account_registry(ROOT / "config/accounts.json")
    actual_profile = actual_registry.by_id(profile)
    actual_profile = replace(
        actual_profile,
        order_execution_enabled=fault != "gate",
        environment=TInvestEnvironment.PROD if fault == "prod" else TInvestEnvironment.SANDBOX,
    )
    registry = Mock()
    registry.by_id.return_value = actual_profile
    monkeypatch.setattr(cli, "load_account_registry", lambda path: registry)
    monkeypatch.setattr(
        cli,
        "load_runtime_config",
        lambda path: cli.RuntimeConfig(
            environment=TInvestEnvironment.SANDBOX,
            schedule=cli.RuntimeSchedule(),
            sandbox_orders_enabled=True,
        ),
    )
    config = cli.load_intraday_config(ROOT / "config/intraday.json")
    monkeypatch.setattr(
        cli, "load_intraday_config", lambda path: replace(config, execution_stage="sandbox")
    )
    monkeypatch.setattr(
        cli.TInvestSandboxExecutionAdapter, "from_environment", lambda **kwargs: Mock()
    )
    result = SimpleNamespace(
        run_id="run",
        submitted=() if fault == "empty" else ("order",),
        stopped_reason="unknown" if fault == "stopped" else None,
    )
    executor = Mock(return_value=result)
    monkeypatch.setattr(cli, "execute_shadow_plan", executor)
    monkeypatch.setattr(cli, "render_sandbox_execution_report", lambda result: "T‑INVEST SANDBOX")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    kwargs = dict(
        accounts_path=tmp_path,
        portfolio_path=tmp_path,
        output_path=tmp_path,
        journal_path=tmp_path / "durable.sqlite3",
        outbox_path=tmp_path,
        as_of=NOW,
    )
    code = (
        cli.sandbox_execute(**kwargs, shadow_path=tmp_path, runtime_path=tmp_path)
        if profile == "long"
        else cli.intraday_sandbox_execute(**kwargs, config_path=tmp_path, plan_path=tmp_path)
    )
    assert code == (2 if fault in {"gate", "prod", "stopped"} else 0)
    if fault not in {"gate", "prod"}:
        assert executor.call_args.kwargs["journal_path"] == tmp_path / "durable.sqlite3"
        assert outbox.enqueue.call_count == int(fault != "empty")


@pytest.mark.parametrize(
    "critical,warning,apply", [(False, False, False), (False, True, True), (True, True, False)]
)
def test_storage_cli_describes_thresholds_without_deleting_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    critical: bool,
    warning: bool,
    apply: bool,
) -> None:
    result = SimpleNamespace(
        before=SimpleNamespace(
            total=100,
            used=95 if warning else 10,
            free=5 if warning else 90,
            used_percent=95 if warning else 10,
        ),
        after=SimpleNamespace(used_percent=95 if critical else 10, free=5 if critical else 90),
        cleanup_triggered=warning,
        critical=critical,
        deleted_files=0,
        deleted_bytes=0,
    )
    maintain = Mock(return_value=result)
    monkeypatch.setattr(cli, "maintain_storage", maintain)
    assert cli.storage_maintenance(
        config_path=ROOT / "config/maintenance.json",
        filesystem_path=tmp_path,
        state_dir=tmp_path,
        log_dir=tmp_path,
        backup_dir=tmp_path,
        apply=apply,
        as_of=NOW,
    ) == (2 if critical else 0)
    assert maintain.call_args.kwargs["apply"] is apply


@pytest.mark.parametrize("fault", ["none", "snapshots", "data", "network"])
@pytest.mark.parametrize("notify", [True, False])
def test_intraday_performance_report_preserves_skip_vs_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    notify: bool,
) -> None:
    profile = cli.load_account_registry(ROOT / "config/accounts.json").by_id("intraday")
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fake")
    monkeypatch.setenv(profile.account_id_env, "intraday-account")
    service = Mock()
    service.operations.return_value = ()
    if fault == "network":
        service.operations.side_effect = OSError("offline")
    monkeypatch.setattr(cli, "TInvestSandboxAccountService", lambda *args: service)
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: Mock())
    summary = Mock(return_value=_intent())
    if fault in {"snapshots", "data"}:
        summary.side_effect = ValueError(
            "requires at least two portfolio snapshots" if fault == "snapshots" else "bad data"
        )
    monkeypatch.setattr(cli, "summarize_intraday_performance", summary)
    monkeypatch.setattr(cli, "render_intraday_performance_report", lambda value: "report")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    assert cli.intraday_performance_report(
        accounts_path=ROOT / "config/accounts.json",
        universe_path=ROOT / "config/universe.json",
        notifications_path=ROOT / "config/notifications.json",
        artifacts_dir=tmp_path,
        report_date=NOW.date(),
        output_path=tmp_path / "report.txt",
        outbox_path=tmp_path / "outbox.sqlite3" if notify else None,
        as_of=NOW,
    ) == (0 if fault == "none" else 3 if fault == "snapshots" else 2)
    assert outbox.enqueue.call_count == int(fault == "none" and notify)


def test_intraday_trade_notifications_only_use_actual_trade_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = cli.load_account_registry(ROOT / "config/accounts.json").by_id("intraday")
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fake")
    monkeypatch.setenv(profile.account_id_env, "intraday-account")
    service = Mock()
    service.operations.return_value = [
        TradeOperation("buy", NOW, "SBER", "BUY", 10, Decimal("1000"), Decimal("1")),
        TradeOperation("deposit", NOW, None, None, 0, Decimal("1000"), Decimal("0")),
    ]
    monkeypatch.setattr(cli, "TInvestSandboxAccountService", lambda *args: service)
    monkeypatch.setattr(cli, "UrlLibJsonTransport", lambda: Mock())
    outbox = Mock()
    outbox.enqueue.return_value = True
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    assert (
        cli.intraday_trade_notifications(
            accounts_path=ROOT / "config/accounts.json",
            universe_path=ROOT / "config/universe.json",
            notifications_path=ROOT / "config/notifications.json",
            outbox_path=tmp_path / "outbox.sqlite3",
            as_of=NOW,
        )
        == 0
    )
    outbox.enqueue.assert_called_once()
    assert outbox.enqueue.call_args.kwargs["dedupe_key"] == "intraday-operation:buy"


def test_runtime_admin_commands_are_local_only(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"t_invest_environment":"sandbox"}')
    assert cli.runtime_normalize(runtime_path=runtime) == 0
    assert cli.runtime_normalize(runtime_path=runtime) == 0
    assert cli.sandbox_orders_set(runtime_path=runtime, enabled=True) == 0
    assert (
        cli.environment_status(runtime_path=runtime, services_path=ROOT / "config/services.json")
        == 0
    )
    assert cli.runtime_render_systemd(runtime_path=runtime, output_dir=tmp_path / "units") == 0
    assert list((tmp_path / "units").rglob("runtime.conf"))
    assert cli.config_check(root=ROOT) == 0
    assert cli.environment_set(runtime_path=runtime, environment=TInvestEnvironment.SANDBOX) == 0


@pytest.mark.parametrize("futures", [None, "SiZ6"])
@pytest.mark.parametrize("notify", [True, False])
def test_flow_cli_persists_typed_data_and_queues_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    futures: str | None,
    notify: bool,
) -> None:
    from moex_bot.reporting import EquityFlowSnapshot

    equity = EquityFlowSnapshot("SBER", NOW, NOW, Decimal("100"), Decimal("50"), 1)
    adapter = Mock()
    adapter.equity_flow.return_value = equity
    adapter.futoi.return_value = _intent()
    adapter.concentration.return_value = _intent()
    monkeypatch.setattr(cli.AlgoPackFlowAdapter, "from_environment", lambda: adapter)
    monkeypatch.setattr(cli, "render_flow_report", lambda *args: "flow")
    outbox = Mock()
    monkeypatch.setattr(cli, "SQLiteOutbox", lambda path: outbox)
    output = tmp_path / "flow.json"
    assert (
        cli.algopack_flow(
            secid="SBER",
            futures_ticker=futures,
            as_of=NOW,
            output_path=output,
            outbox_path=tmp_path if notify else None,
        )
        == 0
    )
    assert json.loads(output.read_text())["report"] == "flow"
    assert adapter.futoi.call_count == int(futures is not None)
    assert outbox.enqueue.call_count == int(notify)


@pytest.mark.parametrize("failure", [False, True])
def test_snapshot_cli_persists_observation_or_reports_fetch_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    adapter = Mock()
    adapter.hourly_observation.return_value = _intent()
    if failure:
        adapter.hourly_observation.side_effect = OSError("offline")
    monkeypatch.setattr(cli.MoexAlgoReadOnlyAdapter, "from_environment", lambda **kwargs: adapter)
    output = tmp_path / "snapshot.json"
    assert cli.moex_snapshot(
        secid="SBER", uid="uid", board="TQBR", as_of=NOW, output_path=output, require_token=False
    ) == (2 if failure else 0)
    assert output.exists() is not failure


@pytest.mark.parametrize("fund,balance", [(False, "0"), (True, "0"), (True, "300000")])
def test_profile_bootstrap_keeps_distinct_bindings_and_funds_only_deficits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fund: bool,
    balance: str,
) -> None:
    for name in (
        "T_INVEST_SANDBOX_ACCOUNT_ID",
        "T_INVEST_SANDBOX_LONG_ACCOUNT_ID",
        "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    service = Mock()
    service.ensure_account.side_effect = [
        SimpleNamespace(account_id="new-long", created=True),
        SimpleNamespace(account_id="new-day", created=False),
    ]
    service.available_rub_balance.return_value = Decimal(balance)
    service.pay_in.return_value = Decimal("300000")
    env = tmp_path / "bot.env"
    assert (
        cli.sandbox_bootstrap_profiles(
            env_path=env,
            accounts_path=ROOT / "config/accounts.json",
            fund_targets=fund,
            service=service,
        )
        == 0
    )
    assert service.pay_in.call_count == (2 if fund and balance == "0" else 0)
    content = env.read_text()
    assert "T_INVEST_SANDBOX_LONG_ACCOUNT_ID=new-long" in content
    assert "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID=new-day" in content
    assert "T_INVEST_SANDBOX_ACCOUNT_ID=new-long" in content


@pytest.mark.parametrize("active", [True, False])
def test_replacement_cli_requires_stopped_services_before_lifecycle_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
) -> None:
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="active" if active else "inactive"),
    )
    replacement = Mock(return_value={"state": "verified"})
    monkeypatch.setattr(cli, "replace_sandbox_accounts", replacement)
    monkeypatch.setattr(cli.TInvestSandboxAccountService, "from_environment", lambda: Mock())
    preview = tmp_path / "preview.json"
    preview.write_text("{}")
    assert cli.sandbox_replace(
        accounts_path=ROOT / "config/accounts.json",
        preview_path=preview,
        confirmation_hash="hash",
        env_path=tmp_path / "bot.env",
        state_path=tmp_path / "replacement.json",
    ) == (2 if active else 0)
    assert replacement.call_count == int(not active)


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_replacement_preview_cli_persists_preview_without_executing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    monkeypatch.setattr(cli.TInvestSandboxAccountService, "from_environment", lambda: Mock())
    monkeypatch.setattr(cli, "replacement_preview", lambda *args, **kwargs: {"preview": "safe"})
    monkeypatch.setattr(cli, "masked_preview", lambda value: "masked")
    monkeypatch.setattr(cli, "os", SimpleNamespace(name=platform, getenv=os.getenv))
    output = tmp_path / "preview.json"
    assert (
        cli.sandbox_replace_preview(accounts_path=ROOT / "config/accounts.json", output_path=output)
        == 0
    )
    assert json.loads(output.read_text()) == {"preview": "safe"}


@pytest.mark.parametrize("healthy", [True, False])
def test_algopack_health_cli_preserves_diagnostic_status(
    monkeypatch: pytest.MonkeyPatch,
    healthy: bool,
) -> None:
    monkeypatch.setattr(
        cli,
        "probe_algopack",
        lambda **kwargs: SimpleNamespace(
            status="ok" if healthy else "auth", detail="diagnostic", ok=healthy
        ),
    )
    assert cli.algopack_health(services_path=ROOT / "config/services.json", as_of=NOW) == (
        0 if healthy else 2
    )


def test_report_utilities_write_and_read_local_artifacts(tmp_path: Path) -> None:
    assert (
        cli.ownership_report(
            registry_path=ROOT / "config/ownership_disclosures.json",
            secid="SBER",
            as_of=NOW,
            output_path=tmp_path / "ownership.txt",
        )
        == 0
    )
    artifact = tmp_path / "shadow.json"
    artifact.write_text("[]")
    assert cli.shadow_decisions(input_path=artifact) == 2
    artifact.write_text(
        json.dumps(
            {"quality": {"passed": True}, "geo": {}, "targets": [], "orders": [], "rejected": []}
        )
    )
    assert cli.shadow_decisions(input_path=artifact) == 0
    assert cli.outbox_retry_dead(outbox_path=tmp_path / "outbox.sqlite3", as_of=NOW) == 0


@pytest.mark.parametrize(
    "command,status",
    [
        ("calendar-refresh", "OK"),
        ("calendar-status", "OK"),
        ("calendar-status", "INCOMPLETE/STALE"),
        ("calendar-status", "error"),
    ],
)
def test_calendar_main_dispatch_and_storage_errors(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    status: str,
) -> None:
    import sys

    from moex_bot import event_calendar

    store = Mock()
    store.status.return_value = status
    if status == "error":
        store.status.side_effect = OSError("offline")
    monkeypatch.setattr(event_calendar, "EventStore", lambda path: store)
    monkeypatch.setattr(event_calendar, "refresh", lambda *args: 0)
    monkeypatch.setattr(cli, "_load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["cli", command])
    assert cli.main() == (2 if status in {"INCOMPLETE/STALE", "error"} else 0)


@pytest.mark.parametrize(
    "function",
    [
        "intraday_trade_notifications",
        "intraday_performance_report",
        "sandbox_reconcile",
        "sandbox_journal_reconcile",
        "sandbox_replace_preview",
        "sandbox_journal_prepare",
    ],
)
@pytest.mark.parametrize("prod", [True, False])
def test_contour_guards_and_missing_credentials_fail_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    prod: bool,
) -> None:
    import inspect

    registry = cli.load_account_registry(ROOT / "config/accounts.json")
    fake = Mock()
    fake.by_id.side_effect = lambda key: replace(
        registry.by_id(key),
        environment=(TInvestEnvironment.PROD if prod else TInvestEnvironment.SANDBOX),
    )
    monkeypatch.setattr(cli, "load_account_registry", lambda path: fake)
    monkeypatch.delenv("T_INVEST_SANDBOX_TOKEN", raising=False)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise ValueError("no credentials")

    monkeypatch.setattr(cli.TInvestSandboxAccountService, "from_environment", forbidden)
    args = {}
    for key, value in inspect.signature(getattr(cli, function)).parameters.items():
        if value.default is not inspect.Parameter.empty:
            continue
        if key == "notifications_path":
            args[key] = ROOT / "config/notifications.json"
        elif key == "universe_path":
            args[key] = ROOT / "config/universe.json"
        elif key == "profile_id":
            args[key] = "long"
        elif key == "as_of":
            args[key] = NOW
        elif key == "report_date":
            args[key] = NOW.date()
        elif key == "verify_broker":
            args[key] = True
        elif key == "candidate_path":
            args[key] = None
        elif key == "report_path":
            args[key] = tmp_path / "review" / "report.json"
        else:
            args[key] = tmp_path / "state" / "artifacts"
    assert getattr(cli, function)(**args) == 2


@pytest.mark.parametrize("fault", ["naive", "disabled"])
def test_trade_notification_input_and_policy_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    policy = cli.load_notification_policy(ROOT / "config/notifications.json")
    monkeypatch.setattr(
        cli,
        "load_notification_policy",
        lambda path: replace(policy, intraday_notify_filled_operations=False),
    )
    assert cli.intraday_trade_notifications(
        accounts_path=tmp_path,
        universe_path=tmp_path,
        notifications_path=tmp_path,
        outbox_path=tmp_path,
        as_of=NOW.replace(tzinfo=None) if fault == "naive" else NOW,
    ) == (2 if fault == "naive" else 3)


@pytest.mark.parametrize(
    "function,dependency",
    [
        ("daily_trade_report", "summarize_shadow_artifacts"),
        ("config_check", "validate_project_configs"),
    ],
)
def test_reporting_dependency_failure_returns_error_not_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    dependency: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise ValueError("bad persisted data")

    monkeypatch.setattr(cli, dependency, fail)
    if function == "config_check":
        assert cli.config_check(root=tmp_path) == 2
    else:
        assert (
            cli.daily_trade_report(
                artifacts_dir=tmp_path,
                report_date=NOW.date(),
                timezone="Europe/Moscow",
                output_path=tmp_path,
                outbox_path=None,
                as_of=NOW,
            )
            == 2
        )


def test_flow_fetch_exception_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise RuntimeError("expired token")

    monkeypatch.setattr(cli.AlgoPackFlowAdapter, "from_environment", fail)
    assert (
        cli.algopack_flow(
            secid="SBER", futures_ticker=None, as_of=NOW, output_path=tmp_path, outbox_path=None
        )
        == 2
    )


@pytest.mark.parametrize("stage,status", [(" ", 1), ("test", 0)])
def test_health_alert_rejects_undefined_stage_or_success_status(
    tmp_path: Path,
    stage: str,
    status: int,
) -> None:
    assert cli.health_alert(outbox_path=tmp_path, stage=stage, status=status, as_of=NOW) == 2


def test_telegram_missing_credentials_is_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert cli.telegram_send(outbox_path=tmp_path, as_of=NOW, limit=1) == 3


@pytest.mark.parametrize("case", ["telegram_pair", "unknown", "missing", "prod"])
def test_integration_preflight_reports_pairs_and_explicit_requirements(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    for key in (
        "MOEX_APIKEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "T_INVEST_PROD_TOKEN",
        "T_INVEST_PROD_ACCOUNT_ID",
        "T_INVEST_SANDBOX_TOKEN",
        "T_INVEST_SANDBOX_ACCOUNT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    required = ()
    runtime = ROOT / "config/runtime.json"
    if case == "telegram_pair":
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    elif case == "unknown":
        required = ("unknown",)
    elif case == "missing":
        required = ("moex_algopack",)
    else:
        monkeypatch.setenv("T_INVEST_PROD_TOKEN", "fake")
        monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "fake")
        runtime = ROOT / "config/prod_runtime.json"
    assert cli.integration_preflight(ROOT / "config/services.json", required, runtime) == (
        0 if case == "prod" else 2
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("long", []),
        ("long", {"open_orders": 1}),
        ("intraday", {"open_orders": 1}),
        ("intraday", {"positions": []}),
    ],
)
def test_reset_refuses_invalid_or_active_broker_snapshots(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    long = tmp_path / "long.json"
    intraday = tmp_path / "intraday.json"
    long.write_text(json.dumps(value if field == "long" else {}))
    intraday.write_text(json.dumps(value if field == "intraday" else {}))
    assert (
        cli.history_reset_safety(
            runtime_path=ROOT / "config/runtime.json",
            long_snapshot=long,
            intraday_snapshot=intraday,
        )
        == 2
    )


def test_bootstrap_interactive_topup_uses_parsed_amount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    service.ensure_account.return_value = SimpleNamespace(account_id="sandbox", created=False)
    service.available_rub_balance.return_value = Decimal("0")
    service.pay_in.return_value = Decimal("100")
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "sandbox")
    monkeypatch.setattr("builtins.input", lambda prompt: "100")
    assert (
        cli.sandbox_bootstrap(
            env_path=tmp_path / "env",
            account_name="test",
            top_up=None,
            no_prompt=False,
            service=service,
        )
        == 0
    )
    service.pay_in.assert_called_once_with("sandbox", Decimal("100"))


def test_profile_bootstrap_rejects_binding_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "one")
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "two")
    service = Mock()
    assert (
        cli.sandbox_bootstrap_profiles(
            env_path=tmp_path,
            accounts_path=ROOT / "config/accounts.json",
            fund_targets=False,
            service=service,
        )
        == 2
    )
    service.ensure_account.assert_not_called()


def test_intraday_portfolio_must_be_object(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text("[]")
    assert (
        cli.intraday_plan(
            config_path=ROOT / "config/intraday.json",
            universe_path=ROOT / "config/universe.json",
            portfolio_path=portfolio,
            state_path=tmp_path,
            output_path=tmp_path,
            outbox_path=None,
            as_of=NOW,
        )
        == 2
    )


@pytest.mark.parametrize("case", ["same", "candidate_state", "missing_token"])
def test_journal_preparation_path_and_verification_authority_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    artifacts = tmp_path / "state" / "artifacts"
    inventory = SimpleNamespace(issues=(), entries=(), files=0, duplicates=0)
    monkeypatch.setattr(cli, "scan_legacy", lambda *args: inventory)
    monkeypatch.setattr(cli, "inventory_report", lambda inventory: {})
    monkeypatch.delenv("T_INVEST_SANDBOX_TOKEN", raising=False)
    report = tmp_path / "review" / "report.json"
    candidate = (
        report
        if case == "same"
        else tmp_path / "state" / "candidate.sqlite3"
        if case == "candidate_state"
        else None
    )
    assert (
        cli.sandbox_journal_prepare(
            accounts_path=ROOT / "config/accounts.json",
            artifacts_path=artifacts,
            report_path=report,
            candidate_path=candidate,
            verify_broker=case == "missing_token",
        )
        == 2
    )


def test_sandbox_runtime_disabled_is_skip_not_failed_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "load_runtime_config",
        lambda path: cli.RuntimeConfig(
            environment=TInvestEnvironment.SANDBOX,
            schedule=cli.RuntimeSchedule(),
            sandbox_orders_enabled=False,
        ),
    )
    assert (
        cli.sandbox_execute(
            accounts_path=ROOT / "config/accounts.json",
            shadow_path=tmp_path,
            portfolio_path=tmp_path,
            runtime_path=tmp_path,
            output_path=tmp_path,
            journal_path=tmp_path,
            outbox_path=None,
            as_of=NOW,
        )
        == 3
    )


def test_reset_cannot_erase_unresolved_durable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "orders.sqlite3"
    journal.touch()
    ledger = Mock()
    ledger.unresolved_count.return_value = 1
    monkeypatch.setattr(cli, "ExecutionJournal", lambda path: ledger)
    assert (
        cli.history_reset_safety(
            runtime_path=ROOT / "config/runtime.json",
            long_snapshot=tmp_path,
            intraday_snapshot=tmp_path,
            journal_path=journal,
        )
        == 2
    )


def test_profile_bootstrap_refuses_oversized_deficit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = cli.load_account_registry(ROOT / "config/accounts.json")
    monkeypatch.setattr(
        cli,
        "load_account_registry",
        lambda path: SimpleNamespace(
            profiles=(replace(registry.by_id("long"), target_balance_rub=Decimal("1000000000000")),)
        ),
    )
    monkeypatch.delenv("T_INVEST_SANDBOX_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", raising=False)
    service = Mock()
    service.ensure_account.return_value = SimpleNamespace(account_id="long", created=False)
    service.available_rub_balance.return_value = Decimal("0")
    assert (
        cli.sandbox_bootstrap_profiles(
            env_path=tmp_path / "env", accounts_path=tmp_path, fund_targets=True, service=service
        )
        == 2
    )
    service.pay_in.assert_not_called()


@pytest.mark.parametrize("kind", ["snapshot", "performance", "dates"])
def test_broker_reports_require_account_binding_and_valid_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(
        cli,
        "resolve_tinvest_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            token="", account_id="", environment=TInvestEnvironment.SANDBOX
        ),
    )
    if kind == "snapshot":
        assert (
            cli.broker_portfolio_snapshot(
                universe_path=ROOT / "config/universe.json",
                output_path=tmp_path,
                runtime_path=ROOT / "config/runtime.json",
                services_path=ROOT / "config/services.json",
            )
            == 2
        )
    else:
        assert (
            cli.account_performance_report(
                artifacts_dir=tmp_path,
                start_date=NOW.date(),
                end_date=NOW.date().replace(day=29) if kind == "dates" else NOW.date(),
                timezone="Europe/Moscow",
                universe_path=ROOT / "config/universe.json",
                runtime_path=ROOT / "config/runtime.json",
                services_path=ROOT / "config/services.json",
                output_path=tmp_path,
                outbox_path=None,
                weekly=False,
                as_of=NOW,
            )
            == 2
        )


def test_replacement_rejects_prod_profile_even_when_units_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = cli.load_account_registry(ROOT / "config/accounts.json")
    fake = Mock()
    fake.by_id.side_effect = lambda key: replace(
        registry.by_id(key), environment=TInvestEnvironment.PROD
    )
    monkeypatch.setattr(cli, "load_account_registry", lambda path: fake)
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="inactive")
    )
    assert (
        cli.sandbox_replace(
            accounts_path=tmp_path,
            preview_path=tmp_path,
            confirmation_hash="hash",
            env_path=tmp_path,
            state_path=tmp_path,
        )
        == 2
    )


def test_existing_profile_ids_are_not_rewritten_except_long_legacy_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "long")
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "long")
    monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "intraday")
    service = Mock()
    service.ensure_account.side_effect = [
        SimpleNamespace(account_id="long", created=False),
        SimpleNamespace(account_id="intraday", created=False),
    ]
    service.available_rub_balance.return_value = Decimal("300000")
    writer = Mock()
    monkeypatch.setattr(cli, "upsert_env_value", writer)
    assert (
        cli.sandbox_bootstrap_profiles(
            env_path=tmp_path,
            accounts_path=ROOT / "config/accounts.json",
            fund_targets=False,
            service=service,
        )
        == 0
    )
    writer.assert_called_once_with(tmp_path, "T_INVEST_SANDBOX_ACCOUNT_ID", "long")
