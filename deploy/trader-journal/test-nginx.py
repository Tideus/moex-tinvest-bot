"""Loopback-only nginx regression with synthetic data and disposable credentials."""

import base64
import gzip
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


if __name__ == "__main__":
    main()
