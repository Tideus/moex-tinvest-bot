"""Read-only, fail-closed observation of the one-shot IIS claim, never a retry worker."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import Instrument, OrderStatus
from .live_execution import registered_live_journal
from .live_protection import _verified_stop
from .long_auto_signal import Candidate
from .long_daily_report import report_day
from .notifications import SQLiteOutbox
from .prod_long_once import Claim, claim_binding, paired
from .prod_long_once_operator import FIXED, SETTINGS, ProductionTransport, load_settings, operate
from .service_config import PROD_REST
from .supervised_execution import SupervisedTInvestBroker, read_parent

PREFIX = PROD_REST + "/tinkoff.public.invest.api.contract.v1."
READS = frozenset({
    "UsersService/GetAccounts", "OperationsService/GetPositions",
    "OrdersService/GetOrders", "OrdersService/GetOrderState", "StopOrdersService/GetStopOrders",
})
KEY = "data:prod-long-protection"


def rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])  # protobuf JSON may omit an empty repeated field.
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("invalid repeated broker field")
    return value


def has_durable_attempt() -> bool:
    """Missing independent control must not hide a surviving SQLite entry/stop."""
    found = False
    for key, table in (("journal", "actions"), ("protection_journal", "protection")):
        path = Path(str(FIXED[key]))
        if path.is_symlink():
            raise ValueError("unsafe attempt journal")
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                found = bool(db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()) or found
    return found


class ReadOnlyTransport(ProductionTransport):
    def post(self, url: str, *, headers: Any, payload: Any, timeout_seconds: float) -> Any:
        if url not in {PREFIX + method for method in READS}:
            raise ValueError("observer cannot mutate broker state")
        return super().post(url, headers=headers, payload=payload, timeout_seconds=timeout_seconds)


def candidate_from_claim(raw: dict[str, Any]) -> Candidate:
    fields = dict(raw["candidate"])
    instrument = dict(fields.pop("instrument"))
    instrument["tick_size"] = Decimal(instrument["tick_size"])
    return Candidate(
        **(fields | {
            "instrument": Instrument(**instrument),
            "limit_price": Decimal(fields["limit_price"]),
            "stop_trigger": Decimal(fields["stop_trigger"]),
            "valid_until": datetime.fromisoformat(fields["valid_until"]),
        })
    )


def observe(settings_path: Path, now: datetime, *, transport: Any = None) -> dict[str, Any]:
    """Return only redacted state; a disabled grant never suppresses existing exposure."""
    if now.tzinfo is None:
        raise ValueError("aware observation time required")
    result: dict[str, Any] = {"observed_at": now.isoformat(), "environment": "prod"}
    directory = Path(str(FIXED["claim_directory"]))
    claimed = (directory / "claim.json").exists() or (directory / "claim.json").is_symlink()
    orphan = any((directory / (n + ".json")).exists() or
                 (directory / (n + ".json")).is_symlink()
                 for n in ("buy-dispatch", "stop-dispatch", "cancel-dispatch", "result"))
    if not claimed and has_durable_attempt():
        raise ValueError("control evidence lost but durable attempt survives")
    if not settings_path.exists() and not claimed and not orphan:
        return result | {"state": "NOT_ENROLLED", "check": "OK"}
    settings = load_settings(settings_path)
    if orphan and not claimed:
        raise ValueError("orphan dispatch fence")
    if not claimed:
        if settings["enabled"]:
            status = operate(argparse.Namespace(action="status", settings=settings_path))
            if status["state"] != "ARMED_WAITING":
                raise ValueError("entry admission not ready")
            return result | {"state": "WAITING_SIGNAL", "check": "OK"}
        return result | {"state": "DISABLED", "check": "OK"}
    raw = json.loads((directory / "claim.json").read_bytes())
    candidate = candidate_from_claim(raw)
    account = os.environ["T_INVEST_PROD_ACCOUNT_ID"].strip()
    if not account or candidate.account_id != account:
        raise ValueError("claim account mismatch")
    intent, plan = paired(candidate)
    journal = Path(settings["journal"])
    identity = registered_live_journal(journal, account)
    claim = Claim(directory, claim_binding(candidate, plan, identity, raw["release_sha256"]))
    claim.check()
    if not claim.exists("buy-dispatch"):
        return result | {"state": "CLAIMED_NO_DISPATCH", "check": "FAIL: PROD attempt needs review"}
    parent = read_parent(journal, plan)
    protection_path = Path(settings["protection_journal"])
    if protection_path.is_symlink():
        raise ValueError("unsafe protection journal")
    with closing(sqlite3.connect(protection_path.as_uri() + "?mode=ro", uri=True)) as db:
        persisted = db.execute(
            "SELECT state,stop_id,live_uuid FROM protection WHERE account=? "
            "AND request_id=? AND plan_hash=?", (account, plan.request_id, plan.digest),
        ).fetchall()
    if len(persisted) != 1 or persisted[0][2] != identity:
        raise ValueError("protection identity missing")
    token = os.environ["T_INVEST_PROD_TOKEN"]
    transport = transport or ReadOnlyTransport()

    def read(method: str, **payload: object) -> dict[str, Any]:
        if method not in READS:
            raise ValueError("read method required")
        response = transport.post(PREFIX + method, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
        }, payload=payload, timeout_seconds=10)
        if not isinstance(response, dict):
            raise ValueError("invalid broker response")
        return response

    accounts = rows(read("UsersService/GetAccounts"), "accounts")
    matching = [a for a in accounts if a["id"] == account]
    if len(matching) != 1 or matching[0]["type"] != "ACCOUNT_TYPE_TINKOFF_IIS":
        raise ValueError("IIS account unverified")
    current = SupervisedTInvestBroker(token, account, transport, {
        intent.instrument.uid: intent.instrument,
    }).lookup(parent)
    positions = read("OperationsService/GetPositions", accountId=account)
    if positions.get("limitsLoadingInProgress") is True:
        raise ValueError("positions still loading")
    holdings = [s for s in rows(positions, "securities")
                if s["instrumentUid"] == intent.instrument.uid]
    if len(holdings) > 1:
        raise ValueError("duplicate holding")
    units = Decimal(str(holdings[0]["balance"])) if holdings else Decimal(0)
    blocked = Decimal(str(holdings[0]["blocked"])) if holdings else Decimal(0)
    if not units.is_finite() or not blocked.is_finite():
        raise ValueError("nonfinite holding")
    stops = rows(read("StopOrdersService/GetStopOrders", accountId=account,
                      status="STOP_ORDER_STATUS_ACTIVE"), "stopOrders")
    own = [s for s in stops if s["instrumentUid"] == intent.instrument.uid]
    orders = rows(read("OrdersService/GetOrders", accountId=account), "orders")
    pending = [o for o in orders if o["instrumentUid"] == intent.instrument.uid]
    result.update(secid=intent.instrument.secid, units=str(units),
                  parent_status=current.status.value, filled_lots=current.filled_lots)
    if (current.filled_lots == 0 and current.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}
            and units == 0 and not own and not pending):
        return result | {"state": "NO_FILL", "check": "OK"}
    if current.status is OrderStatus.FILLED and current.filled_lots == 1:
        if units == 0 and not own and not pending:
            return result | {"state": "FLAT_EXIT_UNATTRIBUTED", "check": "OK"}
        if (units == intent.instrument.lot_size and blocked == 0 and not pending
                and not holdings[0].get("exchangeBlocked") and claim.exists("stop-dispatch")
                and len(own) == 1 and own[0]["stopOrderId"] == persisted[0][1]
                and _verified_stop(plan, own[0])):
            return result | {"state": "PROTECTED", "check": "OK",
                             "stop_trigger": str(plan.stop_trigger)}
    return result | {"state": "REQUIRES_REVIEW", "check": "FAIL: PROD position or stop unverified"}


def safe_observe(settings: Path, now: datetime) -> dict[str, Any]:
    try:
        return observe(settings, now)
    except Exception:
        # No broker exception, token, account/stop/order ID crosses the public boundary.
        return {"observed_at": now.isoformat(), "environment": "prod", "state": "UNKNOWN",
                "check": "FAIL: PROD evidence or read-only API unavailable"}


def render(result: dict[str, Any], day: date) -> str:
    names = {
        "NOT_ENROLLED": "Не подключён", "DISABLED": "Автовход выключен",
        "WAITING_SIGNAL": "Допуск действует; ожидается сигнал LONG",
        "CLAIMED_NO_DISPATCH": "Попытка зарезервирована; отправка BUY не зафиксирована",
        "NO_FILL": "Покупка не исполнилась; новая попытка запрещена",
        "FLAT_EXIT_UNATTRIBUTED": "Позиция закрыта; причина выхода не установлена",
        "PROTECTED": "Лот и активный защитный стоп подтверждены брокером",
        "REQUIRES_REVIEW": "Позиция/защита не подтверждены — нужна сверка",
        "UNKNOWN": "Нет достоверной сверки: журнал или read-only API недоступен",
    }
    lines = [f"📋 LONG · ИИС · контроль за {day}", names[result["state"]],
             "Срез: " + result["observed_at"]]
    if "secid" in result:
        lines += [f"Бумага: {result['secid']} · штук: {result['units']}",
                  f"BUY: {result['parent_status']} · исполнено лотов: {result['filled_lots']}"]
    if "stop_trigger" in result:
        lines.append("Защитный стоп: " + result["stop_trigger"] + " ₽")
    lines += ["P&L и просадка: н/д — этот отчёт проверяет исполнение и защиту, не доходность.",
              "Ручные бумаги ИИС и intraday не входят в результат LONG.",
              "Снимок текущий; не подменяет оценку портфеля на историческое закрытие."]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    parser.add_argument("--report-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--outbox", type=Path)
    args = parser.parse_args()
    now = datetime.now(UTC)
    day = report_day(now, args.report_date)  # Refuse an unfinished session before API calls.
    result = safe_observe(args.settings, now)
    body = render(result, day)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body + "\n", encoding="utf-8")
    args.output.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8",
    )
    if args.outbox and result["state"] != "NOT_ENROLLED":
        SQLiteOutbox(args.outbox).enqueue(kind="prod_once_daily", dedupe_key=f"prod-once:{day}",
                                         body=body, now=now)
    print("LONG IIS report: " + result["state"])
    return 0 if result["check"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
