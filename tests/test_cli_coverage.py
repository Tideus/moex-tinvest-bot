"""Every public CLI command must dispatch exactly once and preserve return codes."""

import argparse
import inspect
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from moex_bot import cli


def command_parsers() -> dict:
    parser = cli.build_parser()
    return next(a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def argv_for(command: str) -> list[str]:
    result = ["moex-bot", command]
    for action in command_parsers()[command]._actions:
        if not action.required:
            continue
        result.append(action.option_strings[0])
        if action.nargs == 0:
            continue
        value = "input.json"
        if action.choices:
            value = str(next(iter(action.choices)))
        elif action.type is datetime.fromisoformat or action.dest == "as_of":
            value = "2026-10-30T10:00:00+00:00"
        elif "date" in action.dest:
            value = "2026-10-30"
        elif action.type in (int, float):
            value = "1"
        result.append(value)
    return result


@pytest.mark.parametrize(
    "command",
    sorted(
        set(command_parsers())
        - {
            "calendar-refresh",
            "calendar-status",
        }
    ),
)
def test_command_dispatch_is_single_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    calls = []

    def handler(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 37

    monkeypatch.setattr(cli, "_load_local_env", lambda: None)
    for name, value in list(vars(cli).items()):
        if (
            callable(value)
            and getattr(value, "__module__", "") == cli.__name__
            and name not in {"main", "build_parser", "_load_local_env"}
        ):
            monkeypatch.setattr(cli, name, handler)
    monkeypatch.setattr(cli, "live_operator", handler)
    monkeypatch.setattr(sys, "argv", argv_for(command))
    assert cli.main() == 37
    assert len(calls) == 1


@pytest.mark.parametrize(
    "command",
    [c for c, p in command_parsers().items() if any(a.dest == "as_of" for a in p._actions)],
)
def test_timezone_required_before_dispatch(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    monkeypatch.setattr(cli, "_load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", argv_for(command) + ["--as-of", "2026-10-30T10:00:00"])
    assert cli.main() == 2


@pytest.mark.parametrize(
    "name",
    [
        "preflight",
        "integration_preflight",
        "algopack_health",
        "history_reset_safety",
        "storage_maintenance",
        "sandbox_bootstrap_profiles",
        "environment_status",
        "sandbox_orders_set",
        "sandbox_execute",
        "runtime_render_systemd",
        "runtime_normalize",
        "hourly_shadow",
        "prod_long_prepare",
        "sandbox_replace_preview",
        "sandbox_journal_prepare",
        "sandbox_journal_reconcile",
        "sandbox_reconcile",
        "intraday_reconcile",
        "intraday_plan",
        "intraday_sandbox_execute",
        "intraday_trade_notifications",
        "intraday_performance_report",
        "broker_portfolio_snapshot",
        "shadow_decisions",
        "ownership_report",
        "historical_backtest",
        "geo_refresh",
    ],
)
def test_handlers_report_invalid_missing_configuration_without_network(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moex_bot.service_config import TInvestEnvironment

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("missing configuration must never reach network")

    monkeypatch.setattr(cli, "UrlLibJsonTransport", forbidden)
    function = getattr(cli, name)
    kwargs = {}
    for key, parameter in inspect.signature(function).parameters.items():
        if parameter.default is not inspect.Parameter.empty and "Path" not in str(
            parameter.annotation
        ):
            continue
        annotation = str(parameter.annotation)
        if "Path" in annotation:
            kwargs[key] = tmp_path / "missing.json"
        elif "datetime" in annotation:
            kwargs[key] = datetime.fromisoformat("2026-10-30T10:00:00+00:00")
        elif "bool" in annotation:
            kwargs[key] = False
        elif "int" in annotation:
            kwargs[key] = 0
        elif "date" in annotation:
            kwargs[key] = datetime.now().date()
        elif "TInvestEnvironment" in annotation:
            kwargs[key] = TInvestEnvironment.PROD
        else:
            kwargs[key] = "test"
    assert function(**kwargs) == 2


@pytest.mark.parametrize("amount", [None, "0", "-1", "NaN", "Infinity", "1000000000000", "100"])
@pytest.mark.parametrize("existing", [True, False])
def test_sandbox_bootstrap_amount_validation_and_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    amount: str | None,
    existing: bool,
) -> None:
    service = Mock()
    service.ensure_account.return_value = SimpleNamespace(account_id="sandbox-id", created=True)
    service.available_rub_balance.return_value = Decimal("100")
    service.pay_in.return_value = Decimal("200")
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "sandbox-id" if existing else "")
    env_path = tmp_path / "bot.env"
    result = cli.sandbox_bootstrap(
        env_path=env_path,
        account_name="test",
        top_up=Decimal(amount) if amount is not None else None,
        no_prompt=True,
        service=service,
    )
    assert result == (0 if amount in {None, "0", "100"} else 2)
    assert service.pay_in.call_count == (1 if amount == "100" else 0)
    if not existing:
        assert "T_INVEST_SANDBOX_ACCOUNT_ID=sandbox-id" in env_path.read_text()


@pytest.mark.parametrize("raw,expected", [("", "0"), ("1 234,50", "1234.50"), ("100", "100")])
def test_top_up_input_normalizes_russian_amounts(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: raw)
    assert cli._read_top_up() == Decimal(expected)


@pytest.mark.parametrize("error", [ImportError, RuntimeError])
def test_local_env_import_failure_is_not_silently_misclassified(
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    def fail(name: str) -> None:
        raise error("fixture")

    monkeypatch.setattr(cli, "import_module", fail)
    if error is ImportError:
        assert cli._load_local_env() is None
    else:
        with pytest.raises(RuntimeError):
            cli._load_local_env()


def test_local_env_optional_dependency_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    dependency = Mock()
    monkeypatch.setattr(cli, "import_module", lambda name: dependency)
    cli._load_local_env()
    dependency.load_dotenv.assert_called_once_with()


def test_main_rejects_impossible_internal_command(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = Mock()
    parser.parse_args.return_value = SimpleNamespace(command="unsupported")
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(cli, "_load_local_env", lambda: None)
    with pytest.raises(AssertionError, match="unsupported"):
        cli.main()


def test_main_rejects_negative_backtest_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", argv_for("historical-backtest") + ["--sandbox-weeks", "-1"])
    assert cli.main() == 2


def test_python_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.delitem(sys.modules, "moex_bot.cli", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["cli", "session-check", "--as-of", "2026-10-31T01:00:00+03:00"]
    )
    with pytest.raises(SystemExit) as error:
        runpy.run_module("moex_bot.cli", run_name="__main__")
    assert error.value.code == 3
