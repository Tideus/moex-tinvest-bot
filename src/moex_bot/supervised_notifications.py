"""Best-effort durable experiment reporting, isolated from trading decisions."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

from .notifications import SQLiteOutbox, TelegramBotApiSender, deliver_pending

TRADE_OUTBOX = Path("/var/lib/moex-tinvest-bot/data/notifications.sqlite3")
ALERT_OUTBOX = Path("/var/lib/moex-observer/relay.sqlite3")


def notify_experiment(
    body: str, *, dedupe_key: str, critical: bool, now: datetime
) -> dict[str, int | bool]:
    """Queue first; critical events are delivered by the independent relay worker.

    Counts concern this delivery attempt, not broker execution or proof of receipt.
    No exception text, token or full account identifier is logged or returned.
    """
    result: dict[str, int | bool] = {"queued": False, "sent": 0, "failed": 0}
    try:
        if not body or not dedupe_key or now.tzinfo is None:
            raise ValueError("valid event body/key/aware timestamp required")
        safe = body
        for name in ("T_INVEST_PROD_TOKEN", "T_INVEST_SANDBOX_TOKEN", "TELEGRAM_BOT_TOKEN",
                     "T_INVEST_PROD_ACCOUNT_ID", "T_INVEST_SANDBOX_ACCOUNT_ID", "MOEX_APIKEY"):
            secret = os.getenv(name, "")
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
        safe = safe[:4096]
        outbox = SQLiteOutbox(ALERT_OUTBOX if critical else TRADE_OUTBOX)
        key = "prod-experiment:" + hashlib.sha256(dedupe_key.encode()).hexdigest()
        result["queued"] = outbox.enqueue(
            kind="monitoring" if critical else "prod_experiment", dedupe_key=key,
            body=safe, now=now,
        )
        if critical:
            return result  # Relay owns alert credentials/routing/retries; no direct Telegram here.
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            return result  # Durable pending event remains for the usual outbox worker.
        sent, failed = deliver_pending(
            outbox, TelegramBotApiSender(token), chat_id=chat, now=now,
            alert_chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip() or None,
        )
        result.update(sent=sent, failed=failed)
    except Exception:
        result["failed"] = 1  # Includes storage failures; never raise back into execution.
    return result
