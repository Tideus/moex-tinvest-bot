"""Verify private HTTPS delivery without printing authentication or account data."""

import base64
import gzip
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

root = "https://breakwood.ru/journal/"
credentials = dict(
    line.split(": ", 1)
    for line in Path("/etc/moex-trader-journal/access.txt").read_text().splitlines()
)
auth = base64.b64encode((credentials["Username"] + ":" + credentials["Password"]).encode()).decode()


def get(path, authenticated=False, method="GET", compressed=False):
    headers = {"Authorization": "Basic " + auth} if authenticated else {}
    if compressed:
        headers["Accept-Encoding"] = "gzip"
    request = Request(root + path, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, b"", error.headers


for path in ("", "data.json", "journal.js", "journal-model.js", "journal-loader.js"):
    status, _, _ = get(path)
    assert status == 401, (path, status)
    print("PASS: unauthenticated request denied:", path or "index")
for path in ("", "journal.js", "journal-model.js", "journal-loader.js", "journal.css", "data.json"):
    status, data, headers = get(path, True)
    assert status == 200 and data, (path, status)
    assert "no-store" in headers["Cache-Control"]
    expected_type = (
        "text/html"
        if not path
        else {"js": "application/javascript", "css": "text/css", "json": "application/json"}[
            path.rsplit(".", 1)[-1]
        ]
    )
    assert headers.get_content_type() == expected_type, path
    assert headers["X-Content-Type-Options"] == "nosniff"
    print("PASS: authenticated HTTPS:", path or "index")
    if path == "data.json":
        report = json.loads(data)
        assert not report["import"]["errors"]
        assert len(report["streams"]) >= 2
        for stream in report["streams"]:
            assert stream["environment"] == "sandbox"
            assert not stream["warnings"], stream["strategy"]
            assert all("summary" in period for period in stream["periods"].values())
            assert all("net_return_pct" in trade for trade in stream["trades"])
            print(
                "DATA:",
                stream["strategy"],
                "operations",
                len(stream["operations"]),
                "closures",
                len(stream["trades"]),
                "observed",
                stream["observed_at"],
            )
started = time.monotonic()
status, compressed, headers = get("data.json", True, compressed=True)
assert status == 200 and headers.get("Content-Encoding") == "gzip"
assert "accept-encoding" in headers.get("Vary", "").lower()
decoded = gzip.decompress(compressed)
assert json.loads(decoded)["schema"] == "trader-journal-v1"
assert len(compressed) < len(decoded)
print(
    "PASS: complete gzip JSON",
    len(compressed),
    "/",
    len(decoded),
    "bytes in",
    round(time.monotonic() - started, 3),
    "seconds",
)
for path in ("journal.sqlite3", "bot.env", "receipts/", "config.json", "htpasswd"):
    status, _, _ = get(path, True)
    assert status in {403, 404}, (path, status)
    print("PASS: private file unavailable:", path)
assert get("", True, "POST")[0] == 403
print("PASS: write method forbidden")
for endpoint in ("catalog", "health", "summary", "trades", "signals"):
    assert get("api/" + endpoint)[0] == 401
assert get("api/catalog", True, "POST")[0] == 403
assert get("api/sql", True)[0] == 404
assert get("api/health", True)[0] == 200
status, raw, _ = get("api/catalog", True)
assert status == 200
catalog = json.loads(raw)
assert catalog["schema"] == "trader-journal-api-v1"
print("PASS: authenticated API catalog", len(raw), "bytes")
for stream in catalog["streams"]:
    assert not {"signals", "operations", "trades", "equity"} & set(stream)
    params = {
        "stream": stream["id"],
        "period": stream["dates"][-1],
        "generation": catalog["generation"],
    }
    sizes = {}
    for endpoint in ("summary", "trades", "signals"):
        status, body, _ = get("api/" + endpoint + "?" + urlencode(params), True)
        assert status == 200
        data = json.loads(body)
        assert data["generation"] == catalog["generation"]
        sizes[endpoint] = len(body)
        if endpoint != "summary":
            assert len(data["items"]) <= 50
    assert get("api/trades?" + urlencode(params | {"limit": "101"}), True)[0] == 400
    assert get("api/trades?" + urlencode(params | {"generation": "old"}), True)[0] == 409
    print("PASS: API summary/pages/limits/snapshot binding:", stream["strategy"], sizes)
