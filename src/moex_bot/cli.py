from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .adapters import DryRunExecutionAdapter, JsonlAuditLog
from .backtest import load_backtest_settings, run_backtest
from .backtest_reporting import (
    OperationalEvidence,
    assess_promotion,
    load_promotion_gates,
    write_backtest_bundle,
)
from .config import load_config
from .daily_report import render_daily_shadow_report, summarize_shadow_artifacts
from .domain import GeoEvent, Instrument, MarketObservation, PortfolioSnapshot, Position
from .env_file import upsert_env_value
from .geo_feed import refresh_geo_feed
from .harness import TradingHarness
from .integrations.algopack_flow import AlgoPackFlowAdapter
from .integrations.moexalgo_data import MoexAlgoReadOnlyAdapter
from .integrations.tinvest_sandbox import (
    GET_OPERATIONS_BY_CURSOR_PATH,
    GET_ORDERS_PATH,
    GET_PORTFOLIO_PATH,
    GET_POSITIONS_PATH,
    MAX_SANDBOX_PAY_IN_RUB,
    TInvestSandboxAccountService,
    TInvestSandboxExecutionAdapter,
    UrlLibJsonTransport,
)
from .notifications import SQLiteOutbox, TelegramBotApiSender, deliver_pending
from .ownership import load_ownership_disclosures, render_ownership_report
from .performance import render_performance_report, summarize_performance
from .reporting import (
    render_flow_report,
    render_persisted_shadow_decisions,
    render_shadow_report,
)
from .runtime_config import (
    load_runtime_config,
    materialize_runtime_defaults,
    render_systemd_timer_overrides,
    set_runtime_environment,
    set_sandbox_orders_enabled,
)
from .sandbox_execution import execute_shadow_plan, render_sandbox_execution_report
from .scheduler import is_conservative_stock_window
from .service_config import (
    TInvestEnvironment,
    load_service_config,
    resolve_tinvest_runtime,
)
from .shadow import load_universe, run_hourly_shadow
from .validation import validate_project_configs


def _load_local_env() -> None:
    """Load an untracked local .env when python-dotenv is installed."""
    try:
        dotenv_module = import_module("dotenv")
    except ImportError:
        return
    dotenv_module.load_dotenv()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _load_snapshot(
    path: Path,
) -> tuple[datetime, dict[str, MarketObservation], PortfolioSnapshot, tuple[GeoEvent, ...]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    market: dict[str, MarketObservation] = {}
    for item in raw["market"]:
        instrument = Instrument(
            secid=item["secid"],
            uid=item["uid"],
            board=item["board"],
            lot_size=int(item["lot_size"]),
            tick_size=_decimal(item["tick_size"]),
        )
        market[instrument.secid] = MarketObservation(
            instrument=instrument,
            price=_decimal(item["price"]),
            trend=_decimal(item["trend"]),
            momentum=_decimal(item["momentum"]),
            volatility=_decimal(item["volatility"]),
            observed_at=datetime.fromisoformat(item["observed_at"]),
            complete=bool(item["complete"]),
            tradable=bool(item["tradable"]),
        )
    positions = {
        secid: Position(market[secid].instrument, int(lots))
        for secid, lots in raw.get("positions", {}).items()
    }
    portfolio = PortfolioSnapshot(
        cash=_decimal(raw["cash"]),
        positions=positions,
        daily_turnover=_decimal(raw.get("daily_turnover", "0")),
        open_orders=int(raw.get("open_orders", 0)),
    )
    geo_events = tuple(
        GeoEvent(
            event_id=item["event_id"],
            severity=int(item["severity"]),
            confidence=_decimal(item["confidence"]),
            source_tier=item["source_tier"],
            confirmed=bool(item["confirmed"]),
            affected_secids=frozenset(item.get("affected_secids", [])),
            observed_at=datetime.fromisoformat(item["observed_at"]),
        )
        for item in raw.get("geo_events", [])
    )
    return datetime.fromisoformat(raw["as_of"]), market, portfolio, geo_events


def replay(config_path: Path, input_path: Path, output_path: Path) -> int:
    config = load_config(config_path)
    as_of, market, portfolio, geo_events = _load_snapshot(input_path)
    audit_path = output_path.with_suffix(".audit.jsonl")
    harness = TradingHarness(
        config,
        DryRunExecutionAdapter(config.mode),
        JsonlAuditLog(audit_path),
    )
    run_id = f"replay-{as_of.isoformat()}"
    result = harness.run(
        run_id=run_id,
        as_of=as_of,
        market=market,
        portfolio=portfolio,
        geo_events=geo_events,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    print(
        f"quality={result.quality.passed} targets={len(result.targets)} orders={len(result.orders)}"
    )
    print(f"result={output_path}")
    print(f"audit={audit_path}")
    return 0 if result.quality.passed else 2


def preflight(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: configuration valid for mode={config.mode.value}")
    print("Production execution is unavailable; sandbox execution has a separate runtime gate.")
    return 0


def integration_preflight(
    services_path: Path = Path("config/services.json"),
    required: tuple[str, ...] = (),
    runtime_path: Path = Path("config/runtime.json"),
) -> int:
    checks = {
        "MOEX_APIKEY": bool(os.getenv("MOEX_APIKEY", "").strip()),
        "T_INVEST_SANDBOX_TOKEN": bool(os.getenv("T_INVEST_SANDBOX_TOKEN", "").strip()),
        "T_INVEST_SANDBOX_ACCOUNT_ID": bool(os.getenv("T_INVEST_SANDBOX_ACCOUNT_ID", "").strip()),
        "T_INVEST_PROD_TOKEN": bool(os.getenv("T_INVEST_PROD_TOKEN", "").strip()),
        "T_INVEST_PROD_ACCOUNT_ID": bool(os.getenv("T_INVEST_PROD_ACCOUNT_ID", "").strip()),
        "TELEGRAM_BOT_TOKEN": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        "TELEGRAM_CHAT_ID": bool(os.getenv("TELEGRAM_CHAT_ID", "").strip()),
    }
    try:
        services = load_service_config(services_path)
        active_environment = load_runtime_config(runtime_path).environment
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: integration configuration: {exc}")
        return 2
    print("Integration preflight (secret values are never printed):")
    print(f"- Active T-Invest environment: {active_environment.value}")
    print(f"- T-Invest prod gRPC: {services.t_invest.prod_grpc}")
    print(f"- T-Invest sandbox gRPC: {services.t_invest.sandbox_grpc}")
    for name, present in checks.items():
        print(f"- {name}: {'present' if present else 'missing'}")
    pairs = {
        TInvestEnvironment.SANDBOX: (
            checks["T_INVEST_SANDBOX_TOKEN"], checks["T_INVEST_SANDBOX_ACCOUNT_ID"]
        ),
        TInvestEnvironment.PROD: (
            checks["T_INVEST_PROD_TOKEN"], checks["T_INVEST_PROD_ACCOUNT_ID"]
        ),
    }
    for environment, (token_present, account_present) in pairs.items():
        if token_present == account_present:
            continue
        if environment is active_environment:
            if environment is TInvestEnvironment.SANDBOX and token_present:
                print("INFO: run sandbox-bootstrap to create or restore the sandbox account id")
            print(
                f"FAIL: active {environment.value} token and account id "
                "must be configured together"
            )
            return 2
        print(
            f"WARN: incomplete {environment.value} credentials are ignored while "
            f"{active_environment.value} is active"
        )
    if checks["TELEGRAM_BOT_TOKEN"] != checks["TELEGRAM_CHAT_ID"]:
        print("FAIL: Telegram token and chat id must be configured together")
        return 2
    if (
        active_environment is TInvestEnvironment.PROD
        and checks["T_INVEST_PROD_TOKEN"]
        and checks["T_INVEST_PROD_ACCOUNT_ID"]
    ):
        print("SAFE: prod credentials detected, but live execution remains disabled")
    requirements = {
        "moex_algopack": checks["MOEX_APIKEY"],
        "telegram": checks["TELEGRAM_BOT_TOKEN"] and checks["TELEGRAM_CHAT_ID"],
        "tinvest_sandbox": (
            checks["T_INVEST_SANDBOX_TOKEN"] and checks["T_INVEST_SANDBOX_ACCOUNT_ID"]
        ),
        "tinvest_prod": checks["T_INVEST_PROD_TOKEN"] and checks["T_INVEST_PROD_ACCOUNT_ID"],
    }
    unknown = set(required) - set(requirements)
    if unknown:
        print(f"FAIL: unknown required integration: {sorted(unknown)}")
        return 2
    missing = [name for name in required if not requirements[name]]
    if missing:
        print(f"FAIL: required integrations are missing: {', '.join(missing)}")
        return 2
    print("PASS: replay remains available; missing credentials only disable their integration")
    return 0


def _format_rub(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,.2f}".replace(",", " ")


def _read_top_up() -> Decimal:
    raw = input("На сколько рублей пополнить sandbox? [0 — пропустить]: ").strip()
    if not raw:
        return Decimal("0")
    return Decimal(raw.replace(" ", "").replace(",", "."))


def sandbox_bootstrap(
    *,
    env_path: Path,
    account_name: str,
    top_up: Decimal | None,
    no_prompt: bool,
    service: TInvestSandboxAccountService | None = None,
) -> int:
    try:
        sandbox = service or TInvestSandboxAccountService.from_environment()
        configured_id = os.getenv("T_INVEST_SANDBOX_ACCOUNT_ID", "").strip()
        result = sandbox.ensure_account(configured_id, account_name=account_name)
        if result.account_id != configured_id:
            upsert_env_value(env_path, "T_INVEST_SANDBOX_ACCOUNT_ID", result.account_id)
            os.environ["T_INVEST_SANDBOX_ACCOUNT_ID"] = result.account_id
            action = "created" if result.created else "restored"
            print(f"PASS: sandbox account {action}; id saved to {env_path}")
        else:
            print("PASS: configured sandbox account exists and is open")

        balance = sandbox.available_rub_balance(result.account_id)
        print(f"Available sandbox balance: {_format_rub(balance)} RUB")
        amount = top_up
        if amount is None and not no_prompt:
            amount = _read_top_up()
        if amount is None or amount == 0:
            print("SKIP: sandbox balance was not topped up")
            return 0
        if not amount.is_finite() or amount < 0:
            raise ValueError("top-up must be zero or a positive finite amount")
        if amount > MAX_SANDBOX_PAY_IN_RUB:
            raise ValueError(
                f"top-up exceeds the sandbox limit of {_format_rub(MAX_SANDBOX_PAY_IN_RUB)} RUB"
            )
        new_balance = sandbox.pay_in(result.account_id, amount)
        print(
            f"PASS: added {_format_rub(amount)} RUB; "
            f"new sandbox balance: {_format_rub(new_balance)} RUB"
        )
    except (EOFError, OSError, ValueError) as exc:
        print(f"FAIL: sandbox bootstrap: {exc}")
        return 2
    return 0


def environment_status(*, runtime_path: Path, services_path: Path) -> int:
    try:
        config = load_runtime_config(runtime_path)
        runtime = resolve_tinvest_runtime(
            load_service_config(services_path), environment=config.environment
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: runtime configuration: {exc}")
        return 2
    print(f"T-Invest environment: {runtime.environment.value}")
    print(f"gRPC endpoint: {runtime.grpc_endpoint}")
    print(f"REST endpoint: {runtime.rest_endpoint}")
    print(f"token source: {runtime.token_env} ({'present' if runtime.token else 'missing'})")
    print(
        f"account source: {runtime.account_id_env} "
        f"({'present' if runtime.account_id else 'missing'})"
    )
    print("production orders: DISABLED")
    print(
        "sandbox orders: "
        f"{'ENABLED' if config.sandbox_orders_enabled else 'DISABLED'} "
        f"(max {config.sandbox_max_orders_per_cycle}/cycle)"
    )
    print(
        "shadow schedule: "
        f"{config.schedule.shadow_on_calendar} {config.schedule.timezone}"
    )
    print(
        "daily report schedule: "
        f"{config.schedule.daily_report_on_calendar} {config.schedule.timezone}"
    )
    print(f"health interval: {config.schedule.health_interval}")
    print(f"diagnostics interval: {config.schedule.diagnostics_interval_seconds}s")
    return 0


def environment_set(*, runtime_path: Path, environment: TInvestEnvironment) -> int:
    set_runtime_environment(runtime_path, environment)
    print(f"T-Invest environment switched to {environment.value}")
    if environment is TInvestEnvironment.PROD:
        print("SAFE: production server selected; live orders remain DISABLED")
    return 0


def sandbox_orders_set(*, runtime_path: Path, enabled: bool) -> int:
    try:
        set_sandbox_orders_enabled(runtime_path, enabled)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: sandbox execution switch: {exc}")
        return 2
    print(f"PASS: sandbox order submission {'ENABLED' if enabled else 'DISABLED'}")
    print("Production order submission remains impossible in this build.")
    return 0


def sandbox_execute(
    *,
    shadow_path: Path,
    portfolio_path: Path,
    runtime_path: Path,
    output_path: Path,
    outbox_path: Path | None,
    as_of: datetime,
) -> int:
    try:
        runtime = load_runtime_config(runtime_path)
        if not runtime.sandbox_orders_enabled:
            print("SKIP: sandbox order submission is disabled")
            return 3
        adapter = TInvestSandboxExecutionAdapter.from_environment()
        result = execute_shadow_plan(
            shadow_path=shadow_path,
            portfolio_path=portfolio_path,
            output_path=output_path,
            runtime=runtime,
            adapter=adapter,
            as_of=as_of,
        )
        if outbox_path is not None:
            SQLiteOutbox(outbox_path).enqueue(
                kind="sandbox_execution",
                dedupe_key=f"sandbox-execution:{result.run_id}",
                body=render_sandbox_execution_report(result),
                now=as_of,
            )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"FAIL: sandbox execution: {exc}")
        return 2
    print(
        f"PASS: sandbox submitted={len(result.submitted)} "
        f"stopped={result.stopped_reason or 'no'} result={output_path}"
    )
    return 2 if result.stopped_reason else 0


def runtime_render_systemd(*, runtime_path: Path, output_dir: Path) -> int:
    try:
        config = load_runtime_config(runtime_path)
        paths = render_systemd_timer_overrides(config, output_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: runtime schedule: {exc}")
        return 2
    for path in paths:
        print(f"PASS: wrote systemd schedule override: {path}")
    return 0


def runtime_normalize(*, runtime_path: Path) -> int:
    try:
        changed = materialize_runtime_defaults(runtime_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: runtime migration: {exc}")
        return 2
    state = "completed missing fields" if changed else "already complete"
    print(f"PASS: runtime configuration {state}: {runtime_path}")
    return 0


def config_check(*, root: Path) -> int:
    try:
        checks = validate_project_configs(root)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"FAIL: configuration validation: {exc}")
        return 2
    for item in checks:
        print(f"PASS: {item}")
    print(f"PASS: {len(checks)} project configuration files validated")
    return 0


def moex_snapshot(
    *,
    secid: str,
    uid: str,
    board: str,
    as_of: datetime,
    output_path: Path,
    require_token: bool,
) -> int:
    try:
        adapter = MoexAlgoReadOnlyAdapter.from_environment(require_token=require_token)
        observation = adapter.hourly_observation(
            secid=secid,
            uid=uid,
            board=board,
            as_of=as_of,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 2
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(observation), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    print(f"PASS: completed hourly observation written to {output_path}")
    return 0


def hourly_shadow(
    *,
    config_path: Path,
    universe_path: Path,
    portfolio_path: Path,
    geo_path: Path,
    output_path: Path,
    as_of: datetime,
    require_token: bool,
    outbox_path: Path | None = None,
) -> int:
    try:
        config = load_config(config_path)
        universe = load_universe(universe_path)
        market_data = MoexAlgoReadOnlyAdapter.from_environment(require_token=require_token)
        result = run_hourly_shadow(
            config=config,
            universe=universe,
            portfolio_path=portfolio_path,
            geo_path=geo_path,
            output_path=output_path,
            as_of=as_of,
            market_data=market_data,
        )
        if outbox_path is not None:
            SQLiteOutbox(outbox_path).enqueue(
                kind="shadow_run",
                dedupe_key=f"shadow:{result.run_id}",
                body=render_shadow_report(result, as_of),
                now=as_of,
            )
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(
        f"quality={result.quality.passed} geo={result.geo.level.value} "
        f"targets={len(result.targets)} orders={len(result.orders)}"
    )
    print(f"result={output_path}")
    return 0 if result.quality.passed else 2


def broker_portfolio_snapshot(
    *,
    universe_path: Path,
    output_path: Path,
    runtime_path: Path,
    services_path: Path,
) -> int:
    try:
        runtime_config = load_runtime_config(runtime_path)
        service_config = load_service_config(services_path)
        broker = resolve_tinvest_runtime(
            service_config, environment=runtime_config.environment
        )
        if not broker.token or not broker.account_id:
            raise ValueError(f"{broker.environment.value} token and account id are required")
        universe = load_universe(universe_path)
        instruments = _verified_instruments(universe)
        service = TInvestSandboxAccountService(broker.token, transport=UrlLibJsonTransport())
        method_paths = (
            {}
            if broker.environment is TInvestEnvironment.SANDBOX
            else {
                "portfolio_path": GET_PORTFOLIO_PATH,
                "positions_path": GET_POSITIONS_PATH,
                "orders_path": GET_ORDERS_PATH,
            }
        )
        snapshot = service.broker_snapshot(
            broker.account_id,
            instruments,
            base_url=broker.rest_endpoint,
            source=f"t_invest_{broker.environment.value}",
            **method_paths,
        )
        zone = ZoneInfo(runtime_config.schedule.timezone)
        now = datetime.now(UTC)
        local_day = now.astimezone(zone).date()
        day_start = datetime.combine(local_day, time.min, zone)
        operations_path = (
            None
            if broker.environment is TInvestEnvironment.SANDBOX
            else GET_OPERATIONS_BY_CURSOR_PATH
        )
        operations = service.operations(
            broker.account_id,
            instruments,
            from_time=day_start,
            to_time=now + timedelta(seconds=1),
            base_url=broker.rest_endpoint,
            **({} if operations_path is None else {"operations_path": operations_path}),
        )
        daily_turnover = sum(
            (item.gross for item in operations if item.side in {"BUY", "SELL"}), Decimal("0")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                snapshot.as_portfolio_payload(daily_turnover=daily_turnover),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: broker portfolio snapshot: {exc}")
        return 2
    print(
        f"PASS: {broker.environment.value} broker portfolio snapshot "
        f"cash={snapshot.cash_available} blocked={snapshot.cash_blocked} "
        f"equity={snapshot.reported_equity} positions={len(snapshot.positions_lots)} "
        f"open_orders={snapshot.open_orders} daily_turnover={daily_turnover}"
    )
    print(f"result={output_path}")
    return 0


def _verified_instruments(universe: tuple[Any, ...]) -> dict[str, Instrument]:
    return {
        item.t_invest_uid: Instrument(
            item.secid,
            item.t_invest_uid,
            item.board,
            item.lot_size_verified,
            Decimal("0.01"),
            issuer_id=item.issuer_id,
            sector=item.sector,
            risk_cluster=item.risk_cluster,
            asset_class=item.asset_class,
        )
        for item in universe
    }


def shadow_decisions(*, input_path: Path) -> int:
    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("shadow artifact must be a JSON object")
        report = render_persisted_shadow_decisions(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: shadow decisions: {exc}")
        return 2
    print(report)
    return 0


def algopack_flow(
    *,
    secid: str,
    futures_ticker: str | None,
    as_of: datetime,
    output_path: Path,
    outbox_path: Path | None,
) -> int:
    try:
        adapter = AlgoPackFlowAdapter.from_environment()
        equity = adapter.equity_flow(secid=secid, as_of=as_of)
        futoi = (
            None
            if futures_ticker is None
            else adapter.futoi(ticker=futures_ticker, as_of=as_of)
        )
        concentration = adapter.concentration(secid=secid, as_of=as_of)
        report = render_flow_report(equity, futoi, concentration)
        payload = {
            "equity": asdict(equity),
            "futoi": None if futoi is None else asdict(futoi),
            "concentration": asdict(concentration),
            "report": report,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8"
        )
        if outbox_path is not None:
            SQLiteOutbox(outbox_path).enqueue(
                kind="market_flow",
                dedupe_key=f"flow:{secid}:{equity.window_end.isoformat()}",
                body=report,
                now=as_of,
            )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: ALGOPACK flow written to {output_path}")
    return 0


def ownership_report(
    *, registry_path: Path, secid: str, as_of: datetime, output_path: Path
) -> int:
    try:
        items = load_ownership_disclosures(registry_path, as_of=as_of, secid=secid)
        report = render_ownership_report(items, secid=secid)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: ownership report written to {output_path}")
    return 0


def telegram_send(*, outbox_path: Path, as_of: datetime, limit: int) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("SKIP: Telegram credentials are not configured")
        return 3
    outbox = SQLiteOutbox(outbox_path)
    sent, failed = deliver_pending(
        outbox,
        TelegramBotApiSender(token),
        chat_id=chat_id,
        now=as_of,
        limit=limit,
    )
    print(f"telegram sent={sent} failed={failed} pending={outbox.counts().get('pending', 0)}")
    return 0 if failed == 0 else 2


def daily_trade_report(
    *,
    artifacts_dir: Path,
    report_date: date,
    timezone: str,
    output_path: Path,
    outbox_path: Path | None,
    as_of: datetime,
) -> int:
    try:
        summary = summarize_shadow_artifacts(
            artifacts_dir.glob("shadow-*.json"),
            report_date=report_date,
            timezone=timezone,
        )
        report = render_daily_shadow_report(summary)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report + "\n", encoding="utf-8")
        if outbox_path is not None and summary.cycles > 0:
            SQLiteOutbox(outbox_path).enqueue(
                kind="daily_trade_report",
                dedupe_key=f"daily-shadow:{report_date.isoformat()}",
                body=report,
                now=as_of,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: daily trade report: {exc}")
        return 2
    print(
        f"PASS: daily report date={report_date} cycles={summary.cycles} "
        f"trade_rows={len(summary.rows)} output={output_path}"
    )
    return 0


def account_performance_report(
    *,
    artifacts_dir: Path,
    start_date: date,
    end_date: date,
    timezone: str,
    universe_path: Path,
    runtime_path: Path,
    services_path: Path,
    output_path: Path,
    outbox_path: Path | None,
    weekly: bool,
    as_of: datetime,
) -> int:
    try:
        if start_date > end_date:
            raise ValueError("report start date must not be after end date")
        runtime_config = load_runtime_config(runtime_path)
        broker = resolve_tinvest_runtime(
            load_service_config(services_path), environment=runtime_config.environment
        )
        if not broker.token or not broker.account_id:
            raise ValueError("sandbox token and account id are required")
        instruments = _verified_instruments(load_universe(universe_path))
        zone = ZoneInfo(timezone)
        from_time = datetime.combine(start_date, time.min, zone)
        to_time = datetime.combine(end_date + timedelta(days=1), time.min, zone)
        service = TInvestSandboxAccountService(broker.token, UrlLibJsonTransport())
        operations_path = (
            None
            if broker.environment is TInvestEnvironment.SANDBOX
            else GET_OPERATIONS_BY_CURSOR_PATH
        )
        operations = service.operations(
            broker.account_id,
            instruments,
            from_time=from_time,
            to_time=to_time,
            base_url=broker.rest_endpoint,
            **({} if operations_path is None else {"operations_path": operations_path}),
        )
        label = (
            f"{start_date.strftime('%d.%m.%Y')} — {end_date.strftime('%d.%m.%Y')}"
            if weekly
            else start_date.strftime("%d.%m.%Y")
        )
        summary = summarize_performance(
            artifacts_dir.glob("shadow-*.json"),
            operations,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            label=label,
        )
        report = render_performance_report(summary, weekly=weekly)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report + "\n", encoding="utf-8")
        output_path.with_suffix(".json").write_text(
            json.dumps(asdict(summary), ensure_ascii=False, default=str, indent=2) + "\n",
            encoding="utf-8",
        )
        if outbox_path is not None:
            period = "weekly" if weekly else "daily"
            SQLiteOutbox(outbox_path).enqueue(
                kind=f"{period}_account_performance",
                dedupe_key=f"{period}-account-performance:{start_date}:{end_date}",
                body=report,
                now=as_of,
            )
    except ValueError as exc:
        if "requires at least two portfolio snapshots" in str(exc):
            print(f"SKIP: account performance report: {exc}")
            return 3
        print(f"FAIL: account performance report: {exc}")
        return 2
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"FAIL: account performance report: {exc}")
        return 2
    print(
        f"PASS: {'weekly' if weekly else 'daily'} account report "
        f"period={start_date}:{end_date} output={output_path}"
    )
    return 0


def historical_backtest(
    *,
    strategy_config_path: Path,
    backtest_config_path: Path,
    promotion_gates_path: Path,
    universe_path: Path,
    output_dir: Path,
    require_token: bool,
    sandbox_weeks: int,
    reconciled_orders: int,
    unresolved_orders: int,
) -> int:
    try:
        bot_config = load_config(strategy_config_path)
        settings = load_backtest_settings(backtest_config_path)
        gates = load_promotion_gates(promotion_gates_path)
        universe = load_universe(universe_path)
        adapter = MoexAlgoReadOnlyAdapter.from_environment(require_token=require_token)
        candles = {}
        for entry in universe:
            print(f"FETCH: {entry.secid} {settings.start_date}:{settings.end_date}")
            series = adapter.historical_daily_candles(
                secid=entry.secid,
                board=entry.board,
                start=settings.start_date,
                end=settings.end_date,
            )
            if not series:
                raise ValueError(f"no historical daily candles for {entry.secid}")
            candles[entry.secid] = series
        benchmark = adapter.historical_daily_candles(
            secid=settings.benchmark_secid,
            board=settings.benchmark_board,
            start=settings.start_date,
            end=settings.end_date,
        )
        if not benchmark:
            raise ValueError(f"no benchmark candles for {settings.benchmark_secid}")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "market-data.json").write_text(
            json.dumps(
                {
                    "source": "MOEX via moexalgo Ticker.candles(period='1D')",
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "requested_period": {
                        "start": settings.start_date.isoformat(),
                        "end": settings.end_date.isoformat(),
                    },
                    "securities": {
                        secid: [asdict(item) for item in series]
                        for secid, series in candles.items()
                    },
                    "benchmark": {
                        "secid": settings.benchmark_secid,
                        "board": settings.benchmark_board,
                        "candles": [asdict(item) for item in benchmark],
                    },
                },
                ensure_ascii=False,
                default=str,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        base = run_backtest(
            bot_config=bot_config,
            settings=settings,
            universe=universe,
            candles=candles,
            benchmark_candles=benchmark,
        )
        stress = run_backtest(
            bot_config=bot_config,
            settings=settings,
            universe=universe,
            candles=candles,
            benchmark_candles=benchmark,
            cost_multiplier=settings.cost_stress_multiplier,
        )
        assessment = assess_promotion(
            base,
            stress,
            gates,
            OperationalEvidence(sandbox_weeks, reconciled_orders, unresolved_orders),
        )
        write_backtest_bundle(
            output_dir=output_dir,
            base=base,
            stress=stress,
            assessment=assessment,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: historical backtest: {exc}")
        return 2
    print(
        f"PASS: backtest OOS={base.oos_metrics.return_pct:+.2f}% "
        f"benchmark={base.oos_metrics.benchmark_return_pct} "
        f"promotion={'PASS' if assessment.passed else 'BLOCKED'}"
    )
    print(f"report={output_dir / 'REPORT.md'}")
    print(f"chart={output_dir / 'equity-curve.html'}")
    return 0


def outbox_health(*, outbox_path: Path, as_of: datetime, max_pending_due: int) -> int:
    if max_pending_due < 0:
        print("FAIL: max_pending_due must be non-negative")
        return 2
    outbox = SQLiteOutbox(outbox_path)
    health = outbox.health(now=as_of)
    print(f"outbox pending_due={health['pending_due']} dead={health['dead']}")
    if health["dead"] or health["pending_due"] > max_pending_due:
        for issue in outbox.issues(now=as_of):
            reason = issue.last_error or "unknown"
            print(
                f"- {issue.status} kind={issue.kind} key={issue.dedupe_key} "
                f"attempts={issue.attempts} error={reason}"
            )
        print("FAIL: Telegram outbox is unhealthy")
        return 2
    print("PASS: Telegram outbox is healthy")
    return 0


def outbox_retry_dead(*, outbox_path: Path, as_of: datetime) -> int:
    count = SQLiteOutbox(outbox_path).requeue_dead(now=as_of)
    print(f"PASS: requeued dead Telegram messages={count}")
    return 0


def geo_refresh(*, sources_path: Path, output_path: Path, as_of: datetime) -> int:
    try:
        healthy = refresh_geo_feed(
            sources_path=sources_path,
            output_path=output_path,
            as_of=as_of,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"geo_feed={'healthy' if healthy else 'stale'} output={output_path}")
    return 0 if healthy else 2


def session_check(as_of: datetime) -> int:
    if is_conservative_stock_window(as_of):
        print("OPEN: inside conservative MOEX stock window")
        return 0
    print("SKIP: outside conservative MOEX stock window")
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MOEX/T-Invest safety harness")
    sub = parser.add_subparsers(dest="command", required=True)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--config", type=Path, required=True)
    replay_parser.add_argument(
        "--input",
        "--snapshot",
        dest="input",
        type=Path,
        required=True,
        help="Path to a deterministic replay snapshot",
    )
    replay_parser.add_argument("--output", type=Path, default=Path("artifacts/replay_result.json"))
    preflight_parser = sub.add_parser("preflight")
    preflight_parser.add_argument("--config", type=Path, required=True)
    integration_parser = sub.add_parser("integration-preflight")
    integration_parser.add_argument("--services", type=Path, default=Path("config/services.json"))
    integration_parser.add_argument("--runtime", type=Path, default=Path("config/runtime.json"))
    integration_parser.add_argument(
        "--require",
        action="append",
        choices=("moex_algopack", "telegram", "tinvest_sandbox", "tinvest_prod"),
        default=[],
    )
    sandbox_parser = sub.add_parser(
        "sandbox-bootstrap",
        help="Verify/create the sandbox account, show cash and optionally top it up",
    )
    sandbox_parser.add_argument("--env-file", type=Path, default=Path(".env"))
    sandbox_parser.add_argument("--account-name", default="moex-tinvest-bot")
    sandbox_parser.add_argument(
        "--top-up",
        type=Decimal,
        help="RUB amount; when omitted an interactive prompt is shown",
    )
    sandbox_parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Only verify the account and show balance",
    )
    broker_snapshot_parser = sub.add_parser(
        "broker-portfolio-snapshot",
        help="Read cash, positions and active orders from the selected account",
    )
    broker_snapshot_parser.add_argument(
        "--universe", type=Path, default=Path("config/universe.json")
    )
    broker_snapshot_parser.add_argument("--output", type=Path, required=True)
    broker_snapshot_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    broker_snapshot_parser.add_argument(
        "--services", type=Path, default=Path("config/services.json")
    )
    environment_parser = sub.add_parser("environment-status")
    environment_parser.add_argument("--runtime", type=Path, default=Path("config/runtime.json"))
    environment_parser.add_argument("--services", type=Path, default=Path("config/services.json"))
    environment_set_parser = sub.add_parser("environment-set")
    environment_set_parser.add_argument(
        "--environment", type=TInvestEnvironment, choices=list(TInvestEnvironment), required=True
    )
    environment_set_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    runtime_render_parser = sub.add_parser("runtime-render-systemd")
    runtime_render_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    runtime_render_parser.add_argument(
        "--output-dir", type=Path, default=Path("/etc/systemd/system")
    )
    runtime_normalize_parser = sub.add_parser("runtime-normalize")
    runtime_normalize_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    sandbox_switch_parser = sub.add_parser("sandbox-orders-set")
    sandbox_switch_parser.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, required=True
    )
    sandbox_switch_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    config_parser = sub.add_parser("config-check")
    config_parser.add_argument("--root", type=Path, default=Path("."))
    snapshot_parser = sub.add_parser("moex-snapshot")
    snapshot_parser.add_argument("--secid", required=True)
    snapshot_parser.add_argument("--uid", required=True, help="Verified T-Invest instrument UID")
    snapshot_parser.add_argument("--board", default="TQBR")
    snapshot_parser.add_argument("--as-of", type=datetime.fromisoformat)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    snapshot_parser.add_argument("--require-token", action="store_true")
    shadow_parser = sub.add_parser("hourly-shadow")
    shadow_parser.add_argument("--config", type=Path, default=Path("config/shadow.json"))
    shadow_parser.add_argument("--universe", type=Path, default=Path("config/universe.json"))
    shadow_parser.add_argument(
        "--portfolio", type=Path, default=Path("examples/portfolio_empty.json")
    )
    shadow_parser.add_argument("--geo", type=Path, default=Path("examples/geo_events_empty.json"))
    shadow_parser.add_argument("--output", type=Path, default=Path("artifacts/hourly_shadow.json"))
    shadow_parser.add_argument("--as-of", type=datetime.fromisoformat)
    shadow_parser.add_argument("--require-token", action="store_true")
    shadow_parser.add_argument("--outbox", type=Path)
    sandbox_execute_parser = sub.add_parser("sandbox-execute")
    sandbox_execute_parser.add_argument("--shadow", type=Path, required=True)
    sandbox_execute_parser.add_argument("--portfolio", type=Path, required=True)
    sandbox_execute_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    sandbox_execute_parser.add_argument("--output", type=Path, required=True)
    sandbox_execute_parser.add_argument("--outbox", type=Path)
    sandbox_execute_parser.add_argument("--as-of", type=datetime.fromisoformat)
    decisions_parser = sub.add_parser("shadow-decisions")
    decisions_parser.add_argument("--input", type=Path, required=True)
    flow_parser = sub.add_parser("algopack-flow")
    flow_parser.add_argument("--secid", required=True)
    flow_parser.add_argument("--futures-ticker")
    flow_parser.add_argument("--as-of", type=datetime.fromisoformat)
    flow_parser.add_argument("--output", type=Path, required=True)
    flow_parser.add_argument("--outbox", type=Path)
    ownership_parser = sub.add_parser("ownership-report")
    ownership_parser.add_argument(
        "--registry", type=Path, default=Path("config/ownership_disclosures.json")
    )
    ownership_parser.add_argument("--secid", required=True)
    ownership_parser.add_argument("--as-of", type=datetime.fromisoformat)
    ownership_parser.add_argument("--output", type=Path, required=True)
    telegram_parser = sub.add_parser("telegram-send")
    telegram_parser.add_argument("--outbox", type=Path, default=Path("data/notifications.sqlite3"))
    telegram_parser.add_argument("--as-of", type=datetime.fromisoformat)
    telegram_parser.add_argument("--limit", type=int, default=20)
    daily_parser = sub.add_parser(
        "daily-trade-report",
        help="Aggregate one Moscow trading day and enqueue a Telegram shadow-trade report",
    )
    daily_parser.add_argument("--artifacts", type=Path, required=True)
    daily_parser.add_argument("--date", type=date.fromisoformat, required=True)
    daily_parser.add_argument("--timezone", default="Europe/Moscow")
    daily_parser.add_argument("--output", type=Path, required=True)
    daily_parser.add_argument("--outbox", type=Path)
    daily_parser.add_argument("--as-of", type=datetime.fromisoformat)
    performance_parser = sub.add_parser(
        "account-performance-report",
        help="Build daily/weekly account P&L from broker operations and portfolio snapshots",
    )
    performance_parser.add_argument("--artifacts", type=Path, required=True)
    performance_parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    performance_parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    performance_parser.add_argument("--timezone", default="Europe/Moscow")
    performance_parser.add_argument(
        "--universe", type=Path, default=Path("config/universe.json")
    )
    performance_parser.add_argument(
        "--runtime", type=Path, default=Path("config/runtime.json")
    )
    performance_parser.add_argument(
        "--services", type=Path, default=Path("config/services.json")
    )
    performance_parser.add_argument("--output", type=Path, required=True)
    performance_parser.add_argument("--outbox", type=Path)
    performance_parser.add_argument("--weekly", action="store_true")
    performance_parser.add_argument("--as-of", type=datetime.fromisoformat)
    backtest_parser = sub.add_parser(
        "historical-backtest",
        help="Fetch real MOEX daily candles and run a next-session cost-aware backtest",
    )
    backtest_parser.add_argument(
        "--strategy-config", type=Path, default=Path("config/shadow.json")
    )
    backtest_parser.add_argument(
        "--backtest-config", type=Path, default=Path("config/backtest.json")
    )
    backtest_parser.add_argument(
        "--promotion-gates", type=Path, default=Path("config/promotion_gates.json")
    )
    backtest_parser.add_argument(
        "--universe", type=Path, default=Path("config/universe.json")
    )
    backtest_parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/historical-backtest")
    )
    backtest_parser.add_argument("--require-token", action="store_true")
    backtest_parser.add_argument("--sandbox-weeks", type=int, default=0)
    backtest_parser.add_argument("--reconciled-orders", type=int, default=0)
    backtest_parser.add_argument("--unresolved-orders", type=int, default=0)
    outbox_parser = sub.add_parser("outbox-health")
    outbox_parser.add_argument("--outbox", type=Path, default=Path("data/notifications.sqlite3"))
    outbox_parser.add_argument("--as-of", type=datetime.fromisoformat)
    outbox_parser.add_argument("--max-pending-due", type=int, default=20)

    retry_dead_parser = sub.add_parser("outbox-retry-dead")
    retry_dead_parser.add_argument(
        "--outbox", type=Path, default=Path("data/notifications.sqlite3")
    )
    geo_parser = sub.add_parser("geo-refresh")
    geo_parser.add_argument("--sources", type=Path, default=Path("config/geo_sources.json"))
    geo_parser.add_argument("--output", type=Path, default=Path("artifacts/geo_events.json"))
    geo_parser.add_argument("--as-of", type=datetime.fromisoformat)
    session_parser = sub.add_parser("session-check")
    session_parser.add_argument("--as-of", type=datetime.fromisoformat)
    return parser


def main() -> int:
    _load_local_env()
    args = build_parser().parse_args()
    if args.command == "replay":
        return replay(args.config, args.input, args.output)
    if args.command == "preflight":
        return preflight(args.config)
    if args.command == "integration-preflight":
        return integration_preflight(args.services, tuple(args.require), args.runtime)
    if args.command == "sandbox-bootstrap":
        return sandbox_bootstrap(
            env_path=args.env_file,
            account_name=args.account_name,
            top_up=args.top_up,
            no_prompt=args.no_prompt,
        )
    if args.command == "broker-portfolio-snapshot":
        return broker_portfolio_snapshot(
            universe_path=args.universe,
            output_path=args.output,
            runtime_path=args.runtime,
            services_path=args.services,
        )
    if args.command == "environment-status":
        return environment_status(runtime_path=args.runtime, services_path=args.services)
    if args.command == "environment-set":
        return environment_set(runtime_path=args.runtime, environment=args.environment)
    if args.command == "runtime-render-systemd":
        return runtime_render_systemd(
            runtime_path=args.runtime, output_dir=args.output_dir
        )
    if args.command == "runtime-normalize":
        return runtime_normalize(runtime_path=args.runtime)
    if args.command == "sandbox-orders-set":
        return sandbox_orders_set(runtime_path=args.runtime, enabled=args.enabled)
    if args.command == "config-check":
        return config_check(root=args.root)
    if args.command == "shadow-decisions":
        return shadow_decisions(input_path=args.input)
    if args.command == "moex-snapshot":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return moex_snapshot(
            secid=args.secid,
            uid=args.uid,
            board=args.board,
            as_of=as_of,
            output_path=args.output,
            require_token=args.require_token,
        )
    if args.command == "algopack-flow":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return algopack_flow(
            secid=args.secid,
            futures_ticker=args.futures_ticker,
            as_of=as_of,
            output_path=args.output,
            outbox_path=args.outbox,
        )
    if args.command == "ownership-report":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return ownership_report(
            registry_path=args.registry, secid=args.secid, as_of=as_of, output_path=args.output
        )
    if args.command == "telegram-send":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return telegram_send(outbox_path=args.outbox, as_of=as_of, limit=args.limit)
    if args.command == "daily-trade-report":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return daily_trade_report(
            artifacts_dir=args.artifacts,
            report_date=args.date,
            timezone=args.timezone,
            output_path=args.output,
            outbox_path=args.outbox,
            as_of=as_of,
        )
    if args.command == "account-performance-report":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return account_performance_report(
            artifacts_dir=args.artifacts,
            start_date=args.start_date,
            end_date=args.end_date,
            timezone=args.timezone,
            universe_path=args.universe,
            runtime_path=args.runtime,
            services_path=args.services,
            output_path=args.output,
            outbox_path=args.outbox,
            weekly=args.weekly,
            as_of=as_of,
        )
    if args.command == "historical-backtest":
        if min(args.sandbox_weeks, args.reconciled_orders, args.unresolved_orders) < 0:
            print("FAIL: operational evidence values must be non-negative")
            return 2
        return historical_backtest(
            strategy_config_path=args.strategy_config,
            backtest_config_path=args.backtest_config,
            promotion_gates_path=args.promotion_gates,
            universe_path=args.universe,
            output_dir=args.output_dir,
            require_token=args.require_token,
            sandbox_weeks=args.sandbox_weeks,
            reconciled_orders=args.reconciled_orders,
            unresolved_orders=args.unresolved_orders,
        )
    if args.command == "outbox-health":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return outbox_health(
            outbox_path=args.outbox,
            as_of=as_of,
            max_pending_due=args.max_pending_due,
        )
    if args.command == "outbox-retry-dead":
        return outbox_retry_dead(outbox_path=args.outbox, as_of=datetime.now(UTC))
    if args.command == "hourly-shadow":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return hourly_shadow(
            config_path=args.config,
            universe_path=args.universe,
            portfolio_path=args.portfolio,
            geo_path=args.geo,
            output_path=args.output,
            as_of=as_of,
            require_token=args.require_token,
            outbox_path=args.outbox,
        )
    if args.command == "sandbox-execute":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return sandbox_execute(
            shadow_path=args.shadow,
            portfolio_path=args.portfolio,
            runtime_path=args.runtime,
            output_path=args.output,
            outbox_path=args.outbox,
            as_of=as_of,
        )
    if args.command == "geo-refresh":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return geo_refresh(sources_path=args.sources, output_path=args.output, as_of=as_of)
    if args.command == "session-check":
        as_of = args.as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            print("FAIL: --as-of must include a timezone offset")
            return 2
        return session_check(as_of)
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
