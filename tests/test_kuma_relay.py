import importlib.util
import json
import threading
from datetime import UTC, datetime
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from moex_bot.notifications import SQLiteOutbox


def test_private_relay_deduplicates_and_does_not_forward_raw_secrets(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/ubuntu/kuma-telegram-relay.py"
    spec = importlib.util.spec_from_file_location("kuma_relay_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUTBOX = SQLiteOutbox(tmp_path / "relay.db")
    monkeypatch.setenv("KUMA_ALLOWED_IP", "127.0.0.1")
    server = HTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/kuma"
        data = json.dumps({"monitor": {"id": 1, "name": "Resources"},
                           "heartbeat": {"id": 5, "status": 0},
                           "msg": "SECRET_TOKEN https://sensitive-url"}).encode()
        for _ in range(2):
            with urlopen(Request(url, data=data), timeout=3) as response:
                assert response.status == 202
        messages = module.OUTBOX.claim(now=datetime.now(UTC))
        assert len(messages) == 1
        assert "Resources" in messages[0].body
        assert "SERVER" in messages[0].body
        assert "RAM" in messages[0].body
        assert "SECRET" not in messages[0].body
        monkeypatch.setenv("KUMA_ALLOWED_IP", "192.0.2.100")
        with pytest.raises(HTTPError) as rejected:
            urlopen(Request(url, data=data), timeout=3)
        assert rejected.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
