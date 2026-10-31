"""Private Docker-bridge webhook to durable Telegram outbox; no broker access."""
import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

from moex_bot.monitoring_labels import monitor_description, safe_failure_details
from moex_bot.notifications import SQLiteOutbox, TelegramBotApiSender, deliver_pending

OUTBOX: SQLiteOutbox


def worker():
    sender = TelegramBotApiSender(os.environ["TELEGRAM_BOT_TOKEN"])
    while True:
        try:
            sent, failed = deliver_pending(OUTBOX, sender,
                                          chat_id=os.environ["TELEGRAM_ALERT_CHAT_ID"],
                                          now=datetime.now(UTC), limit=20)
            if sent or failed:
                print(f"Relay sent={sent} failed={failed}", flush=True)
        except Exception:
            print("FAIL: relay worker; event remains queued", flush=True)
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log request bodies or URLs.

    def do_POST(self):
        self.connection.settimeout(10)
        if self.path != "/kuma" or self.client_address[0] != os.environ["KUMA_ALLOWED_IP"]:
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("invalid size")
            payload = json.loads(self.rfile.read(length))
            monitor = payload.get("monitor") or {}
            heartbeat = payload.get("heartbeat") or {}
            status = heartbeat.get("status")
            # Never forward raw msg/URL: these can include Push tokens or response data.
            title = str(monitor.get("name", "Проверка канала Kuma"))[:160]
            title = " ".join(title.split())
            details = safe_failure_details(title, Path(
                "/var/lib/moex-tinvest-bot/artifacts/monitoring-latest.json"), datetime.now(UTC)
            ) if status == 0 and title.startswith(("MOEX /", "MOEX Trading /", "Server /")) else ""
            label = {0: "🚨 DOWN", 1: "✅ UP"}.get(status, "🧪 TEST")
            identity = [monitor.get("id"), heartbeat.get("id"), heartbeat.get("time"), status]
            if not heartbeat:
                identity.append(str(uuid4()))
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            OUTBOX.enqueue(kind="monitoring", dedupe_key="kuma:" + key,
                           body=f"{label} · UPTIME KUMA\n{title}\n"
                                f"{monitor_description(title)}\n{details}".rstrip(),
                           now=datetime.now(UTC))
        except Exception:
            self.send_error(503)
            return
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


if __name__ == "__main__":
    OUTBOX = SQLiteOutbox(Path("/var/lib/moex-observer/relay.sqlite3"))
    threading.Thread(target=worker, daemon=True).start()
    server = HTTPServer((os.environ["KUMA_BIND_IP"], 9088), Handler)
    server.timeout = 10
    server.serve_forever()
