"""Forward shadow only. No execution adapter or legacy runner is called."""

import argparse
import hashlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from moex_bot import cli
from moex_bot.account_profiles import AccountPurpose, load_account_registry
from moex_bot.integrations import tinvest_sandbox as sb

MSK = ZoneInfo("Europe/Moscow")
SESSIONS = (date(2026, 11, 2), date(2026, 11, 3))
CONFIG = Path("config")
READS = frozenset(
    sb.SANDBOX_BASE_URL + method
    for method in (
        sb.GET_SANDBOX_PORTFOLIO_PATH,
        sb.GET_SANDBOX_POSITIONS_PATH,
        sb.GET_SANDBOX_ORDERS_PATH,
        sb.GET_SANDBOX_OPERATIONS_BY_CURSOR_PATH,
    )
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class SandboxReads:
    def __init__(self, account):
        self.account = account
        self.methods = []

    def post(self, url, *, headers, payload, timeout_seconds):
        if url not in READS or payload.get("accountId") != self.account:
            raise PermissionError("PR6 permits only four reads on its assigned sandbox account")
        self.methods.append(url.rsplit("/", 1)[1])
        req = Request(url, headers=dict(headers), data=json.dumps(payload).encode(), method="POST")
        with build_opener(NoRedirect()).open(req, timeout=timeout_seconds) as response:
            return json.load(response)


def save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def observe(root, profile, warmup=False):
    now = datetime.now(UTC)
    local = now.astimezone(MSK)
    if local.date() not in SESSIONS:
        raise ValueError("outside approved sessions")
    if not warmup and not 10 <= local.hour < 19:
        raise ValueError("outside strategy window")
    part = root / ("precheck" if warmup else local.date().isoformat()) / profile
    part.mkdir(parents=True, exist_ok=True)
    tag = now.strftime("%Y%m%dT%H%M%S%fZ")
    status = {"profile": profile, "started_at": now, "warmup": warmup, "stage": "bindings"}
    audit = part / f"cycle-{tag}.json"
    try:
        key = load_account_registry(CONFIG / "accounts.json").sandbox_bindings(os.environ)[
            AccountPurpose(profile)
        ]
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        identity = root / f"{profile}.binding"
        try:
            with identity.open("x") as stream:
                stream.write(fingerprint)
        except FileExistsError:
            if identity.read_text() != fingerprint:
                raise ValueError("account binding changed during experiment") from None
        transport = SandboxReads(key)
        status["reads"] = transport.methods
        status["stage"] = "sandbox-snapshot"
        token = os.environ.pop("T_INVEST_SANDBOX_TOKEN")
        service = sb.TInvestSandboxAccountService(token, transport=transport)
        instruments = cli._verified_instruments(cli.load_universe(CONFIG / "universe.json"))
        snap = service.broker_snapshot(key, instruments)
        ops = service.operations(
            key,
            instruments,
            from_time=datetime.combine(local.date(), time.min, MSK),
            to_time=datetime.now(UTC),
        )
        payload = snap.as_portfolio_payload(
            daily_turnover=sum((op.gross for op in ops if op.side in {"BUY", "SELL"}), Decimal(0))
        )
        payload.update(
            account_id=f"shadow-only:{profile}",
            observed_at=datetime.now(UTC),
            snapshot_started_at=now,
            executed_operations=[asdict(op) for op in ops],
        )
        portfolio = part / f"portfolio-{tag}.json"
        save(portfolio, payload)
        status.update(
            equity=payload["reported_equity"],
            cash=payload["cash"],
            positions=len(payload["positions"]),
            open_orders=payload["open_orders"],
        )
        kwargs = dict(
            universe_path=CONFIG / "universe.json", portfolio_path=portfolio, outbox_path=None
        )
        if profile == "long":
            status["stage"] = "geo"
            geo = part / f"geo-{tag}.json"
            status["geo_code"] = cli.geo_refresh(
                sources_path=CONFIG / "geo_sources.json", output_path=geo, as_of=datetime.now(UTC)
            )
            plan = part / f"long-plan-{tag}.json"
            status["stage"] = "long-planner"
            code = cli.hourly_shadow(
                **kwargs,
                config_path=CONFIG / "shadow.json",
                geo_path=geo,
                require_token=True,
                output_path=plan,
                as_of=datetime.now(UTC),
            )
        else:
            plan = part / f"intraday-plan-{tag}.json"
            status["stage"] = "intraday-planner"
            code = cli.intraday_plan(
                **kwargs,
                config_path=CONFIG / "intraday.json",
                state_path=part / "intraday.sqlite3",
                output_path=plan,
                as_of=datetime.now(UTC),
            )
        status.update(
            stage="complete" if plan.exists() else status["stage"],
            code=code,
            plan=plan.name if plan.exists() else None,
        )
        failure_path = plan.with_suffix(".error.json")
        if code != 0 and failure_path.exists():
            status["failure"] = json.loads(failure_path.read_text(encoding="utf-8"))
            print(json.dumps(status["failure"], ensure_ascii=False), file=sys.stderr)
        return code
    except Exception as exc:
        status.update(code=2, error=type(exc).__name__, http_status=getattr(exc, "code", None))
        return 2
    finally:
        status["finished_at"] = datetime.now(UTC)
        save(audit, status)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=("long", "intraday"))
    parser.add_argument("--precheck", action="store_true")
    args = parser.parse_args()
    # Library messages may contain provider details. Only sanitized cycle evidence leaves the job.
    with redirect_stdout(io.StringIO()):
        result = observe(Path(os.environ["STATE_DIRECTORY"]), args.profile, args.precheck)
    print(f"PR6 {args.profile}: result={result}; sandbox reads only")
    raise SystemExit(result)
