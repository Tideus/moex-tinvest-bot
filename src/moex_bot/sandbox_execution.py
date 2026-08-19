from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from .runtime_config import RuntimeConfig
from .service_config import TInvestEnvironment


class SandboxOrderPort(Protocol):
    account_id: str

    def submit(self, intent: OrderIntent) -> OrderRecord: ...


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    run_id: str
    submitted: tuple[OrderRecord, ...]
    stopped_reason: str | None


def execute_shadow_plan(
    *,
    shadow_path: Path,
    portfolio_path: Path,
    output_path: Path,
    runtime: RuntimeConfig,
    adapter: SandboxOrderPort,
    as_of: datetime,
) -> SandboxExecutionResult:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if runtime.environment is not TInvestEnvironment.SANDBOX:
        raise ValueError("sandbox execution is forbidden outside sandbox environment")
    if not runtime.sandbox_orders_enabled:
        raise ValueError("sandbox order submission is disabled")
    raw = _object(shadow_path)
    portfolio = _object(portfolio_path)
    if portfolio.get("source") != "t_invest_sandbox":
        raise ValueError("execution requires a t_invest_sandbox portfolio snapshot")
    if str(portfolio.get("account_id", "")) != adapter.account_id:
        raise ValueError("portfolio snapshot account does not match execution account")
    open_orders = portfolio.get("open_orders", 0)
    if isinstance(open_orders, bool) or not isinstance(open_orders, (int, str)):
        raise ValueError("portfolio snapshot has invalid open_orders")
    if int(open_orders) != 0:
        raise ValueError("active sandbox orders require reconciliation before execution")
    quality = raw.get("quality")
    if not isinstance(quality, Mapping) or quality.get("passed") is not True:
        raise ValueError("only a quality-passed shadow plan can be executed")
    run_id = str(raw.get("run_id", ""))
    prefix = next(
        (item for item in ("shadow-", "intraday-") if run_id.startswith(item)), None
    )
    if prefix is None:
        raise ValueError("shadow artifact has an invalid run id")
    run_at = datetime.fromisoformat(run_id.removeprefix(prefix))
    if run_at.tzinfo is None or as_of - run_at > timedelta(minutes=30):
        raise ValueError("shadow execution plan is stale")
    if run_at - as_of > timedelta(minutes=1):
        raise ValueError("shadow execution plan is from the future")
    records_raw = raw.get("orders")
    if not isinstance(records_raw, list):
        raise ValueError("shadow artifact has no order list")
    submitted: list[OrderRecord] = []
    stopped_reason: str | None = None
    raw_positions = portfolio.get("positions", {})
    if not isinstance(raw_positions, Mapping):
        raise ValueError("portfolio snapshot has invalid positions")
    projected_lots = {
        str(secid): int(value.get("lots", 0) if isinstance(value, Mapping) else value)
        for secid, value in raw_positions.items()
    }
    for item in records_raw[: runtime.sandbox_max_orders_per_cycle]:
        intent = _intent(item)
        current_lots = projected_lots.get(intent.instrument.secid, 0)
        resulting_lots = current_lots + (
            intent.lots if intent.side is Side.BUY else -intent.lots
        )
        opens_short = resulting_lots < min(current_lots, 0)
        if opens_short != intent.confirm_margin_trade:
            raise ValueError("margin confirmation does not match projected short exposure")
        if opens_short and not intent.instrument.short_enabled:
            raise ValueError("short order instrument is not verified as short-enabled")
        record = adapter.submit(intent)
        submitted.append(record)
        projected_lots[intent.instrument.secid] = resulting_lots
        if record.status in {OrderStatus.UNKNOWN, OrderStatus.REJECTED}:
            stopped_reason = f"submission stopped after {record.status.value} order"
            break
    result = SandboxExecutionResult(run_id, tuple(submitted), stopped_reason)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    return result


def render_sandbox_execution_report(result: SandboxExecutionResult) -> str:
    lines = [
        "🧪 T‑INVEST SANDBOX · ИСПОЛНЕНИЕ",
        f"План: {result.run_id}",
        f"Отправлено заявок: {len(result.submitted)}",
    ]
    for record in result.submitted:
        intent = record.intent
        lines.append(
            f"• {intent.side.value.upper()} {intent.instrument.secid}: "
            f"{intent.lots} лот. · {intent.notional} ₽ · {record.status.value}"
        )
    if result.stopped_reason:
        lines.append(f"⛔ {result.stopped_reason}")
    lines.append("Это виртуальный счёт T‑Invest; реальные деньги не используются.")
    return "\n".join(lines)


def _object(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object: {path}")
    return raw


def _intent(record: object) -> OrderIntent:
    if not isinstance(record, Mapping) or not isinstance(record.get("intent"), Mapping):
        raise ValueError("invalid shadow order record")
    if record.get("status") != "validated":
        raise ValueError("only validated shadow orders can be submitted")
    raw = record["intent"]
    instrument_raw = raw.get("instrument")
    if not isinstance(instrument_raw, Mapping):
        raise ValueError("shadow order has no instrument")
    instrument = Instrument(
        secid=str(instrument_raw["secid"]),
        uid=str(instrument_raw["uid"]),
        board=str(instrument_raw["board"]),
        lot_size=int(instrument_raw["lot_size"]),
        tick_size=Decimal(str(instrument_raw["tick_size"])),
        currency=str(instrument_raw.get("currency", "RUB")),
        issuer_id=str(instrument_raw.get("issuer_id", "unknown")),
        sector=str(instrument_raw.get("sector", "unknown")),
        risk_cluster=str(instrument_raw.get("risk_cluster", "unknown")),
        asset_class=str(instrument_raw.get("asset_class", "share")),
        short_enabled=bool(instrument_raw.get("short_enabled", False)),
    )
    if instrument.asset_class != "share":
        raise ValueError("current sandbox execution gate supports shares only")
    return OrderIntent(
        order_request_id=str(raw["order_request_id"]),
        instrument=instrument,
        side=Side(str(raw["side"])),
        lots=int(raw["lots"]),
        limit_price=Decimal(str(raw["limit_price"])),
        notional=Decimal(str(raw["notional"])),
        rationale=str(raw.get("rationale", "")),
        confirm_margin_trade=bool(raw.get("confirm_margin_trade", False)),
        order_type=str(raw.get("order_type", "limit")),
    )
