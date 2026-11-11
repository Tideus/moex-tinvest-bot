"""Loopback-only nginx regression with synthetic data and disposable credentials."""

import base64
import gzip
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from moex_bot.journal_readmodel import publish


def main():
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="journal-http-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        public = root / "public"
        public.mkdir(mode=0o755)
        for name in (
            "index.html",
            "journal-model.js",
            "journal-loader.js",
            "journal.js",
            "journal.css",
        ):
            (public / name).write_bytes((source / name).read_bytes())
        payload = json.dumps(
            {
                "schema": "trader-journal-v1",
                "streams": [],
                "synthetic_rows": ["test-only signal"] * 20000,
            }
        ).encode()
        (public / "data.json").write_bytes(payload)
        from datetime import UTC, datetime

        synthetic = {
            "schema": "trader-journal-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "streams": [
                {
                    "id": "test:demo",
                    "strategy": "test",
                    "environment": "sandbox",
                    "dates": ["2026-11-11"],
                    "periods": {"2026-11": {}},
                    "positions": {},
                    "warnings": [],
                    "observed_at": None,
                    "trades": [],
                    "operations": [],
                    "orders": [],
                    "equity": [],
                    "signals": [
                        {
                            "at": "2026-11-11T08:00:00Z",
                            "symbol": str(i),
                            "side": "buy",
                            "status": "entry_rejected",
                            "reasons": ["cost_edge"],
                        }
                        for i in range(257)
                    ],
                }
            ],
        }
        model = root / "model.sqlite3"
        generation = publish(synthetic, model)
        api = subprocess.Popen(
            [
                os.environ["JOURNAL_TEST_GUNICORN"],
                "--no-control-socket",
                "--bind",
                "unix:" + str(root / "api.sock"),
                "--umask",
                "0000",
                "--workers",
                "1",
                "moex_bot.journal_api:application",
            ],
            env={**os.environ, "JOURNAL_READMODEL": str(model)},
        )
        password_hash = (
            subprocess.check_output(
                ["openssl", "passwd", "-6", "-stdin"], input=b"test-only-password\n"
            )
            .decode()
            .strip()
        )
        (root / "htpasswd").write_text("tester:" + password_hash + "\n")
        snippet = (
            (source / "nginx.conf")
            .read_text()
            .replace("/etc/moex-trader-journal/htpasswd", str(root / "htpasswd"))
            .replace("/var/lib/moex-trader-journal/public/", str(public) + "/")
            .replace("/run/moex-trader-journal-api/api.sock", str(root / "api.sock"))
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        configuration = root / "nginx.conf"
        configuration.write_text(
            f"pid {root}/nginx.pid; error_log {root}/error.log;\n"
            "events { worker_connections 64; }\n"
            "http { include /etc/nginx/mime.types; access_log off; "
            f"server {{ listen 127.0.0.1:{port}; {snippet} }} }}"
        )
        process = subprocess.Popen(
            ["nginx", "-p", str(root), "-c", str(configuration), "-g", "daemon off;"]
        )
        auth = "Basic " + base64.b64encode(b"tester:test-only-password").decode()

        def get(path, authenticated=True, compressed=False, method="GET"):
            headers = {"Authorization": auth} if authenticated else {}
            if compressed:
                headers["Accept-Encoding"] = "gzip"
            try:
                with urlopen(
                    Request(
                        f"http://127.0.0.1:{port}/journal/" + path, headers=headers, method=method
                    ),
                    timeout=5,
                ) as result:
                    return result.status, result.read(), result.headers
            except HTTPError as error:
                return error.code, b"", error.headers

        try:
            for _ in range(100):
                try:
                    get("")
                    break
                except URLError:
                    if process.poll() is not None:
                        raise RuntimeError("Temporary nginx failed to start") from None
                    time.sleep(0.05)
            for name in ("", "data.json", "journal-loader.js"):
                assert get(name, authenticated=False, compressed=True)[0] == 401
            for _ in range(100):
                if get("api/health")[0] == 200:
                    break
                time.sleep(0.05)
            assert get("api/health")[0] == 200
            assert get("api/catalog", authenticated=False)[0] == 401
            assert get("api/catalog", method="POST")[0] == 403
            catalog = json.loads(get("api/catalog")[1])
            assert catalog["generation"] == generation and "signals" not in catalog["streams"][0]
            params = {
                "stream": "test:demo",
                "period": "2026-11",
                "generation": generation,
                "limit": "100",
            }
            code, packed, headers = get("api/signals?" + urlencode(params), compressed=True)
            assert code == 200 and headers["Content-Encoding"] == "gzip"
            page = json.loads(gzip.decompress(packed))
            assert len(page["items"]) == 100 and page["total"] == 257 and page["next_cursor"]
            params["cursor"] = page["next_cursor"]
            second = json.loads(get("api/signals?" + urlencode(params))[1])
            assert second["items"][0]["symbol"] == "100"
            params["generation"] = "old"
            assert get("api/signals?" + urlencode(params))[0] == 409
            assert get("api/sql")[0] == 404
            print("PASS: real Gunicorn + nginx: auth, GET-only API, gzip, pages, snapshot conflict")
            for name in (
                "data.json",
                "journal.js",
                "journal-model.js",
                "journal-loader.js",
                "journal.css",
            ):
                status, plain, _ = get(name)
                code, packed, headers = get(name, compressed=True)
                assert status == code == 200
                assert headers.get("Content-Encoding") == "gzip", name
                assert gzip.decompress(packed) == plain
                assert len(packed) < len(plain)
                assert "accept-encoding" in headers.get("Vary", "").lower()
                assert "no-store" in headers["Cache-Control"]
                assert headers["X-Content-Type-Options"] == "nosniff"
                print("PASS: authenticated complete gzip", name, len(packed), "/", len(plain))
            assert get("data.json")[1] == payload
            for path in ("config.json", "htpasswd", "journal.sqlite3", "bot.env"):
                assert get(path)[0] in {403, 404}
            assert get("", method="POST")[0] == 403
            print("PASS: private access preserved, writes forbidden; synthetic data only")
        finally:
            process.terminate()
            process.wait(timeout=10)
            api.terminate()
            api.wait(timeout=10)


if __name__ == "__main__":
    main()
