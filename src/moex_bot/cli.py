from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

from .adapters import DryRunExecutionAdapter, JsonlAuditLog
from .config import load_config
from .domain import GeoEvent, Instrument, MarketObservation, PortfolioSnapshot, Position
from .env_file import upsert_env_value
from .geo_feed import refresh_geo_feed
from .harness import TradingHarness
from .integrations.algopack_flow import AlgoPackFlowAdapter
from .integrations.moexalgo_data import MoexAlgoReadOnlyAdapter
from .integrations.tinvest_sandbox import (
    MAX_SANDBOX_PAY_IN_RUB,
    TInvestSandboxAccountService,
)
from .notifications import SQLiteOutbox, TelegramBotApiSender, deliver_pending
from .ownership import load_ownership_disclosures, render_ownership_report
from .reporting import render_flow_report, render_shadow_report
from .runtime_config import (
    load_runtime_config,
    render_systemd_timer_overrides,
    set_runtime_environment,
)
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
    print("Live execution is not available in this scaffold.")
    return 0


def integration_preflight(
    services_path: Path = Path("config/services.json"),
    required: tuple[str, ...] = (),
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
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: service configuration: {exc}")
        return 2
    print("Integration preflight (secret values are never printed):")
    print(f"- T-Invest prod gRPC: {services.t_invest.prod_grpc}")
    print(f"- T-Invest sandbox gRPC: {services.t_invest.sandbox_grpc}")
    for name, present in checks.items():
        print(f"- {name}: {'present' if present else 'missing'}")
    if checks["T_INVEST_SANDBOX_ACCOUNT_ID"] and not checks["T_INVEST_SANDBOX_TOKEN"]:
        print("FAIL: sandbox account id is unusable without a sandbox token")
        return 2
    if checks["T_INVEST_SANDBOX_TOKEN"] and not checks["T_INVEST_SANDBOX_ACCOUNT_ID"]:
        print("INFO: run sandbox-bootstrap to create or restore the sandbox account id")
    if checks["T_INVEST_PROD_TOKEN"] != checks["T_INVEST_PROD_ACCOUNT_ID"]:
        print("FAIL: prod token and account id must be configured together")
        return 2
    if checks["TELEGRAM_BOT_TOKEN"] != checks["TELEGRAM_CHAT_ID"]:
        print("FAIL: Telegram token and chat id must be configured together")
        return 2
    if checks["T_INVEST_PROD_TOKEN"]:
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
    print("live orders: DISABLED")
    print(
        "shadow schedule: "
        f"{config.schedule.shadow_on_calendar} {config.schedule.timezone}"
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


def outbox_health(*, outbox_path: Path, as_of: datetime, max_pending_due: int) -> int:
    if max_pending_due < 0:
        print("FAIL: max_pending_due must be non-negative")
        return 2
    health = SQLiteOutbox(outbox_path).health(now=as_of)
    print(f"outbox pending_due={health['pending_due']} dead={health['dead']}")
    if health["dead"] or health["pending_due"] > max_pending_due:
        print("FAIL: Telegram outbox is unhealthy")
        return 2
    print("PASS: Telegram outbox is healthy")
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
    outbox_parser = sub.add_parser("outbox-health")
    outbox_parser.add_argument("--outbox", type=Path, default=Path("data/notifications.sqlite3"))
    outbox_parser.add_argument("--as-of", type=datetime.fromisoformat)
    outbox_parser.add_argument("--max-pending-due", type=int, default=20)
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
        return integration_preflight(args.services, tuple(args.require))
    if args.command == "sandbox-bootstrap":
        return sandbox_bootstrap(
            env_path=args.env_file,
            account_name=args.account_name,
            top_up=args.top_up,
            no_prompt=args.no_prompt,
        )
    if args.command == "environment-status":
        return environment_status(runtime_path=args.runtime, services_path=args.services)
    if args.command == "environment-set":
        return environment_set(runtime_path=args.runtime, environment=args.environment)
    if args.command == "runtime-render-systemd":
        return runtime_render_systemd(
            runtime_path=args.runtime, output_dir=args.output_dir
        )
    if args.command == "config-check":
        return config_check(root=args.root)
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
