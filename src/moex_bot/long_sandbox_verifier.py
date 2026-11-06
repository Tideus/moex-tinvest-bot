"""Durable first-signal readback and Telegram report. No trading/recovery methods."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import OrderIntent, OrderRecord, OrderStatus, Side
from .long_auto_control import writer_lock
from .long_auto_operator import ANCHOR, STATE, _scope, _working_account
from .long_auto_sandbox import SandboxLongBroker, SandboxOnlyTransport, _rows
from .long_sandbox_retry import resolve
from .monitoring_long_once import ReadOnlyLedger
from .notifications import SQLiteOutbox, TelegramBotApiSender, deliver_pending
from .prod_once_observer import candidate_from_claim
from .sandbox_execution import decode_validated_intent
from .scheduler import MOSCOW
from .service_config import SANDBOX_REST

OUTPUT = Path("/var/lib/moex-tinvest-bot/artifacts/long-sandbox-verification")
OUTBOX = Path("/var/lib/moex-tinvest-bot/data/notifications.sqlite3")
PREFIX = SANDBOX_REST + "/tinkoff.public.invest.api.contract.v1.SandboxService/"
READS = {"GetSandboxAccounts", "GetSandboxOrderState", "GetSandboxPositions",
         "GetSandboxOrders", "GetSandboxStopOrders"}


class ReadOnlySandboxTransport(SandboxOnlyTransport):
    def post(self, url: str, *, headers: Any, payload: Any, timeout_seconds: float) -> Any:
        if url not in {PREFIX + name for name in READS}:
            raise ValueError("verifier permits sandbox reads only")
        return super().post(url, headers=headers, payload=payload, timeout_seconds=timeout_seconds)


def verify(state: Path, anchor: Path, account: str, broker: SandboxLongBroker,
           now: datetime) -> dict[str, Any] | None:
    """Caller holds shared LONG writer lock; SQLite opened read-only, never repaired."""
    if broker.account_id != account or broker.environment != "sandbox" or now.tzinfo is None:
        raise ValueError("exact sandbox account and aware time required")
    binding = resolve(state, anchor, account)
    anchor = binding.anchor
    ledger = ReadOnlyLedger(binding.state, account, anchor=anchor)
    status = ledger.status()
    if status == "WAITING":
        return None  # No signal: no API traffic and no repeated Telegram message.
    with closing(ledger.connect()) as db:
        row = db.execute("SELECT candidate,record,detail FROM attempt WHERE singleton=1").fetchone()
    if not row:
        raise ValueError("claimed ledger without candidate")
    candidate = candidate_from_claim({"candidate": json.loads(row[0])})
    if candidate.account_id != account or candidate.lots != 1:
        raise ValueError("foreign/changed candidate")
    expected = OrderIntent(candidate.order_request_id, candidate.instrument, Side.BUY, 1,
                           candidate.limit_price,
                           candidate.limit_price * candidate.instrument.lot_size,
                           "sandbox one-shot long genuine signal")
    parent = OrderRecord(expected, OrderStatus.UNKNOWN)
    if row[1]:
        raw = json.loads(row[1])
        intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
        if (intent.instrument != expected.instrument or intent.side is not Side.BUY
                or intent.order_request_id != expected.order_request_id or intent.lots != 1
                or intent.limit_price != expected.limit_price):
            raise ValueError("durable parent mismatch")
        parent = OrderRecord(intent, OrderStatus(raw["status"]), raw["filled_lots"],
                             raw["broker_order_id"])
    accounts = _rows(broker._post("SandboxService/GetSandboxAccounts", {}), "accounts")
    if len([a for a in accounts if a.get("id") == account
            and a.get("status") == "ACCOUNT_STATUS_OPEN"]) != 1:
        raise ValueError("working sandbox account not open")
    actual = broker.lookup(parent)
    position = broker._sandbox("GetSandboxPositions")
    if position.get("limitsLoadingInProgress") is True:
        raise ValueError("position still loading")
    holdings = [h for h in _rows(position, "securities")
                if h.get("instrumentUid") == candidate.instrument.uid]
    orders = _rows(broker._sandbox("GetSandboxOrders"), "orders")
    stops = _rows(broker._sandbox("GetSandboxStopOrders", {
        "status": "STOP_ORDER_STATUS_ACTIVE"}), "stopOrders")
    result = {
        "version": 1, "environment": "sandbox", "observed_at": now.isoformat(),
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "decision_sha256": candidate.decision_sha256, "secid": candidate.instrument.secid,
        "lots": 1, "lot_size": candidate.instrument.lot_size,
        "limit_price": str(candidate.limit_price), "stop_trigger": str(candidate.stop_trigger),
        "ledger_state": status, "broker_status": actual.status.value,
        "attempt": 2 if binding.admitted_at is not None else 1,
        "filled_lots": actual.filled_lots, "buy_verified": False, "stop_verified": False,
        "state": "REQUIRES_REVIEW", "read_only": True,
        "ledger_candidate_sha256": hashlib.sha256(row[0].encode()).hexdigest(),
        "parent_id_sha256": hashlib.sha256(str(actual.broker_order_id).encode()).hexdigest(),
    }
    related_orders = [o for o in orders if o.get("instrumentUid") == candidate.instrument.uid]
    related_stops = [s for s in stops if s.get("instrumentUid") == candidate.instrument.uid]
    if any(not o.get("instrumentUid") for o in orders + stops):
        raise ValueError("ambiguous active orders")
    if (status == "ENTRY_NOT_FILLED" and actual.filled_lots == 0
            and actual.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}
            and not holdings and not related_orders and not related_stops):
        result["state"] = "ENTRY_NOT_FILLED"
        return result
    if actual.status is OrderStatus.FILLED and actual.filled_lots == 1:
        result["buy_verified"] = True
        if (status == "FILLED_PROTECTED" and not holdings
                and not related_orders and not related_stops):
            result["state"] = "POSITION_CLOSED"
            return result  # Not proof of a stop fill or an earlier verified active stop.
        if (status == "FILLED_PROTECTED" and parent == actual and len(holdings) == 1
                and str(holdings[0].get("balance")) == str(candidate.instrument.lot_size)
                and str(holdings[0].get("blocked")) == "0"
                and not holdings[0].get("exchangeBlocked") and not related_orders):
            response = json.loads(row[2])
            if not anchor.with_suffix(anchor.suffix + ".stop-dispatch").is_file():
                raise ValueError("missing stop dispatch fence")
            verified = broker.verify_protection(candidate, response)
            if verified.get("verified") is True:
                result.update(state="FILLED_PROTECTED", stop_verified=True)
                result["stop_id_sha256"] = hashlib.sha256(
                    verified["stopOrderId"].encode()).hexdigest()
    return result


def render(result: dict[str, Any]) -> str:
    state = result["state"]
    heading = {"FILLED_PROTECTED": "✅ Покупка и стоп подтверждены",
               "ENTRY_NOT_FILLED": "ℹ️ BUY не исполнен; новая попытка запрещена",
               "REQUIRES_REVIEW": "🚨 Покупка/защита требуют сверки",
               "POSITION_CLOSED": "ℹ️ Позиции больше нет; причина закрытия не установлена",
               "UNAVAILABLE": "🚨 Не удалось проверить LONG: журнал или sandbox API недоступен"}
    observed = datetime.fromisoformat(result["observed_at"]).astimezone(MOSCOW)
    lines = [f"🧪 LONG SANDBOX · попытка {result.get('attempt', 1)}", heading[state],
             "Сверка: " + observed.strftime("%d.%m.%Y · %H:%M МСК")]
    if "secid" in result:
        lines += [f"{result['secid']} · 1 лот = {result['lot_size']} шт.",
                  f"Лимит BUY: {result['limit_price']} ₽ · исполнено: {result['filled_lots']} лот.",
                  "Покупка брокером: " + ("подтверждена" if result["buy_verified"] else "нет"),
                  "Защитный стоп: " + (f"активен · {result['stop_trigger']} ₽"
                                         if result["stop_verified"] else "НЕ подтверждён")]
    lines.append("Проверка read-only. PROD не включён; это не разрешение торговать на ИИС.")
    return "\n".join(lines)


def publish(result: dict[str, Any], output: Path, outbox: Path, now: datetime) -> None:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    facts = {k: v for k, v in result.items() if k != "observed_at"}
    identity = json.dumps(facts, sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()
    target = output / (digest + ".json")
    # Immutable first readback per outcome, not a mutable latest success overwritten by failure.
    if not target.exists():
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=output)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                if os.name == "nt":
                    os.rename(temporary, target)  # Windows rename refuses an existing destination.
                else:
                    os.link(temporary, target)  # POSIX atomic publish without overwriting.
        finally:
            temporary.unlink(missing_ok=True)
        if os.name != "nt":
            fd = os.open(output, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    stored = json.loads(target.read_bytes())
    if {k: v for k, v in stored.items() if k != "observed_at"} != facts:
        raise ValueError("existing verification receipt mismatches; never silently replace")
    SQLiteOutbox(outbox).enqueue(kind="long_sandbox_verification",
                                dedupe_key="long-sandbox-verification:" + digest,
                                body=render(result), now=now)


def publish_planner_failures(root: Path, outbox: Path, now: datetime) -> None:
    """The read-only verifier owns notification writes, not the isolated trading writer."""
    for path in root.glob("run-*/plan.error.json"):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            receipt = json.loads(path.read_bytes())
            at = datetime.fromisoformat(receipt["as_of"])
            if receipt["environment"] != "SANDBOX" or not 0 <= (now - at).total_seconds() <= 86400:
                continue
            failure = receipt["failure"]
            host = failure["host"]
            stage = failure["stage"]
            category = failure["category"]
            symbol = failure["secid"]
            attempts = failure["attempts"]
            if (host not in {"apim.moex.com", "iss.moex.com", "unknown"}
                    or stage not in {"ticker", "metadata", "candles", "historical_candles"}
                    or category not in {"dns", "tls", "connection", "http"}
                    or not isinstance(symbol, str) or not symbol.isascii()
                    or not symbol.isalnum() or len(symbol) > 20
                    or type(attempts) is not int or not 1 <= attempts <= 3):
                continue
            body = (
                "🚨 LONG · SANDBOX · ошибка рыночных данных\n"
                f"Время: {at.astimezone(MOSCOW):%d.%m.%Y %H:%M:%S МСК}\n"
                f"API: MOEX market data · {host} (не T-Invest sandbox)\n"
                f"Этап: {stage} · бумага: {symbol}\n"
                f"Причина: {category} · попыток: {attempts}\n"
                "План не построен: это не отсутствие сигнала. Проверьте API/DNS/TLS.\n"
                "Детали — plan.error.json в каталоге цикла. Заявки не повторять."
            )
            SQLiteOutbox(outbox).enqueue(
                kind="health_failure", dedupe_key="long-planner-failure:" + at.isoformat(),
                body=body, now=now,
            )
        except (OSError, ValueError, KeyError, TypeError):
            print("WARN: invalid or unavailable LONG planner error receipt; inspect journal")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    now = datetime.now(UTC)
    result = None
    try:
        _scope(argparse.Namespace(state=STATE))
        account = _working_account()
        try:
            binding = resolve(STATE, ANCHOR, account)
            waiting = ReadOnlyLedger(
                binding.state, account, anchor=binding.anchor).status() == "WAITING"
        except Exception:
            waiting = False  # Recheck under lock before treating inconsistent evidence as failure.
        if not waiting:
            # Never compete with the one scheduled entry window merely to poll WAITING.
            # OnSuccess/OnFailure handles the transition that happens after this fast read.
            with writer_lock():
                result = verify(STATE, ANCHOR, account, SandboxLongBroker(
                    os.environ["T_INVEST_SANDBOX_TOKEN"], account,
                    transport=ReadOnlySandboxTransport()), now)
    except Exception as exc:
        if isinstance(exc.__cause__, BlockingIOError):
            print("WAITING: LONG writer still active; no broker retry or notification")
            return 0
        result = {"version": 1, "state": "UNAVAILABLE", "observed_at": now.isoformat(),
                  "environment": "sandbox", "read_only": True}
    if result is not None:
        publish(result, OUTPUT, OUTBOX, now)
    publish_planner_failures(STATE.parent, OUTBOX, now)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat:
        # Durable outbox is the receipt; an unavailable Telegram API is retried on next cycle.
        deliver_pending(SQLiteOutbox(OUTBOX), TelegramBotApiSender(token), chat_id=chat, now=now,
                        alert_chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID") or None)
    print("LONG sandbox verification: " + (str(result["state"]) if result else "WAITING_SIGNAL"))
    return 2 if result and result["state"] in {"UNAVAILABLE", "REQUIRES_REVIEW"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
