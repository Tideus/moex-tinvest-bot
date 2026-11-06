"""Working LONG sandbox one-shot; a separate read-only IIS signal preview. No PROD submit."""

from __future__ import annotations

import argparse
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from .account_profiles import AccountPurpose, load_account_registry
from .long_auto_control import writer_lock
from .long_auto_once import OnceLedger, init, run_sandbox_candidate
from .long_auto_sandbox import SandboxLongBroker
from .long_auto_signal import select_candidate
from .long_sandbox_retry import authorize, require_empty, resolve
from .runtime_config import load_runtime_config

BASE = Path("/var/lib/moex-tinvest-bot/long-once")
STATE = BASE / "sandbox.sqlite3"
ANCHOR = Path("/var/lib/moex-tinvest-control/long-once-sandbox/entry.claim")
CONFIG = Path("/opt/moex-tinvest-bot/config")
MARKER = Path("/etc/moex-tinvest-bot/sandbox-long-once.owner")
RUNTIME = Path("/etc/moex-tinvest-bot/runtime.json")
TERMINAL = {"FILLED_PROTECTED", "ENTRY_NOT_FILLED"}


def _working_account() -> str:
    account = os.getenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "").strip()
    other = os.getenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "").strip()
    if not account or not other or account in {other, os.getenv("T_INVEST_PROD_ACCOUNT_ID", "")}:
        raise ValueError("explicit separate working sandbox account bindings required")
    return account


def _scope(args: argparse.Namespace) -> None:
    if (
        args.state != STATE
        or STATE.resolve() != STATE
        or ANCHOR.resolve() != ANCHOR
        or MARKER.resolve() != MARKER
        or MARKER.read_bytes() != b"sandbox-long-once/v1\n"
        or MARKER.stat().st_uid != 0
        or MARKER.stat().st_mode & 0o022
        or not stat.S_ISREG(MARKER.stat().st_mode)
        or MARKER.stat().st_nlink != 1
    ):
        raise ValueError("sandbox enrollment or canonical ledger path invalid")


def _entry_gate(args: argparse.Namespace, account: str) -> None:
    _scope(args)
    runtime = load_runtime_config(RUNTIME)
    registry = load_account_registry(CONFIG / "accounts.json")
    bindings = registry.sandbox_bindings(os.environ)
    profile = registry.by_id("long")
    if (
        runtime.environment.value != "sandbox"
        or not runtime.sandbox_orders_enabled
        or _working_account() != account
        or bindings[AccountPurpose.LONG] != account
        or profile.account_id_env != "T_INVEST_SANDBOX_LONG_ACCOUNT_ID"
        or not profile.order_execution_enabled
        or profile.purpose.value != "long"
        or [(s.strategy_id, s.config_path) for s in profile.strategies if s.enabled]
        != [("daily_long_momentum_v1", "config/shadow.json")]
    ):
        raise ValueError("sandbox long account, strategy or runtime disallows entry")


def operate(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    sandbox = args.action != "prod-preview"
    if sandbox:
        _scope(args)
        account = _working_account()
        if args.action == "init-sandbox":
            init(STATE, account, anchor=ANCHOR)
            print("WAITING: working long sandbox enrolled; no broker request")
            return 0
        if args.action == "admit-second-sandbox":
            from .long_sandbox_verifier import ReadOnlySandboxTransport

            _entry_gate(args, account)
            binding = authorize(STATE, ANCHOR, account, SandboxLongBroker(
                os.environ["T_INVEST_SANDBOX_TOKEN"], account,
                transport=ReadOnlySandboxTransport()), now)
            print("ADMITTED: sandbox LONG attempt 2; new signal only; 1 lot <=10000 RUB; "
                  "original ledger preserved; no broker mutation; PROD unchanged")
            print("Active attempt:",
                  OnceLedger(binding.state, account, anchor=binding.anchor).status())
            return 0
        binding = resolve(STATE, ANCHOR, account)
        status = OnceLedger(binding.state, account, anchor=binding.anchor).status()
        print("SANDBOX LONG one-shot:", status)
        if args.action == "reconcile":
            from .long_auto_once import reconcile_sandbox_once

            observed = reconcile_sandbox_once(
                binding.state,
                account,
                SandboxLongBroker(os.environ["T_INVEST_SANDBOX_TOKEN"], account),
                anchor=binding.anchor,
            )
            print(
                "READ_ONLY: correlated parent",
                observed["broker_status"],
                "filled lots:",
                observed["filled_lots"],
                "ledger:",
                status,
            )
            return 0
        if args.action == "recover-stop":
            from .long_auto_once import load_saved_candidate, recover_sandbox_protection

            saved = load_saved_candidate(binding.state, account, anchor=binding.anchor)
            broker = SandboxLongBroker(os.environ["T_INVEST_SANDBOX_TOKEN"], account)
            result = recover_sandbox_protection(saved, binding.state, broker, anchor=binding.anchor)
            print(result)
            return 0 if result == "FILLED_PROTECTED" else 2
        if status not in TERMINAL | {"WAITING"}:
            print("CRITICAL: inspect sandbox order and protection; no repeat entry")
            return 2
        if args.action == "status" or status in TERMINAL:
            return 3 if getattr(args, "waiting_only", False) and status in TERMINAL else 0
        if (
            args.plan.resolve() != args.plan
            or args.portfolio.resolve() != args.portfolio
            or args.plan.parent.parent != BASE
            or not re.fullmatch(r"run-[0-9]{8}T[0-9]{6}Z\.[A-Za-z0-9]{8}", args.plan.parent.name)
            or args.plan.name != "plan.json"
            or args.portfolio != args.plan.with_name("portfolio.json")
            or args.universe != CONFIG / "universe.json"
            or args.policy != CONFIG / "prod_long.json"
            or args.strategy_config != CONFIG / "shadow.json"
        ):
            raise ValueError("only genuine same-cycle long plan and deployed policies accepted")
    else:
        account = os.environ["T_INVEST_PROD_ACCOUNT_ID"].strip()
        if args.strategy_config.resolve() != (CONFIG / "prod_shadow.json").resolve():
            raise ValueError("production observer strategy required")

    candidate = select_candidate(
        plan=args.plan,
        portfolio=args.portfolio,
        universe=args.universe,
        policy=args.policy,
        strategy_config=args.strategy_config,
        account_id=account,
        now=now,
        environment="sandbox" if sandbox else "prod-preview",
    )
    if candidate is None:
        print("WAITING: no fresh eligible risk-approved LONG BUY; no broker action")
        return 0
    if not sandbox:
        from .supervised_operator import operate as paired_preview

        return paired_preview(
            argparse.Namespace(
                action="preview",
                ticker=candidate.instrument.secid,
                settings=args.settings,
                policy=args.policy,
                universe=args.universe,
                output=args.output,
                kind="stop_loss_market",
                limit_price=str(candidate.limit_price),
                stop_trigger=str(candidate.stop_trigger),
                stop_limit=None,
                signal=candidate,
            )
        )
    _entry_gate(args, account)
    binding.check_candidate(candidate, now)

    def entry_gate() -> None:
        _entry_gate(args, account)
        current = resolve(STATE, ANCHOR, account)
        if current != binding:
            raise ValueError("sandbox attempt binding changed; entry forbidden")
        current.check_candidate(candidate, datetime.now(UTC))
        if current.admitted_at is not None:
            require_empty(broker, account)

    broker = SandboxLongBroker(
        os.environ["T_INVEST_SANDBOX_TOKEN"], account, entry_gate=entry_gate
    )
    result = run_sandbox_candidate(candidate, binding.state, broker, anchor=binding.anchor)
    status = OnceLedger(binding.state, account, anchor=binding.anchor).status()
    critical = status not in TERMINAL
    print(
        f"SANDBOX LONG {candidate.instrument.secid} · 1 lot · limit {candidate.limit_price}"
        f" · native stop {candidate.stop_trigger} · {result} · ledger={status}"
    )
    return 2 if critical else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in (
        "init-sandbox",
        "admit-second-sandbox",
        "status",
        "reconcile",
        "recover-stop",
        "sandbox-cycle",
        "prod-preview",
    ):
        sub = commands.add_parser(name)
        if name != "prod-preview":
            sub.add_argument("--state", type=Path, default=STATE)
        if name == "status":
            sub.add_argument("--waiting-only", action="store_true")
        if name in {"sandbox-cycle", "prod-preview"}:
            for flag in ("plan", "portfolio", "strategy-config", "policy", "universe"):
                sub.add_argument("--" + flag, type=Path, required=True)
        if name == "prod-preview":
            sub.add_argument("--settings", type=Path, required=True)
            sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in {"init-sandbox", "admit-second-sandbox", "sandbox-cycle", "recover-stop"}:
            with writer_lock():
                return operate(args)
        return operate(args)
    except Exception as exc:
        # A broker error/URL, account ID or token must never reach journald.
        print(f"BLOCKED: {args.action} ({type(exc).__name__}); inspect private ledger; no retry")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
