"""Verify private HTTPS delivery without printing authentication or account data."""

import base64
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = "https://breakwood.ru/journal/"
credentials = dict(
    line.split(": ", 1)
    for line in Path("/etc/moex-trader-journal/access.txt").read_text().splitlines()
)
auth = base64.b64encode((credentials["Username"] + ":" + credentials["Password"]).encode()).decode()


def get(path, authenticated=False, method="GET"):
    headers = {"Authorization": "Basic " + auth} if authenticated else {}
    request = Request(root + path, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, b"", error.headers


for path in ("", "data.json", "journal.js", "journal-model.js"):
    status, _, _ = get(path)
    assert status == 401, (path, status)
    print("PASS: unauthenticated request denied:", path or "index")
for path in ("", "journal.js", "journal-model.js", "journal.css", "data.json"):
    status, data, headers = get(path, True)
    assert status == 200 and data, (path, status)
    assert "no-store" in headers["Cache-Control"]
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
for path in ("journal.sqlite3", "bot.env", "receipts/", "config.json", "htpasswd"):
    status, _, _ = get(path, True)
    assert status in {403, 404}, (path, status)
    print("PASS: private file unavailable:", path)
assert get("", True, "POST")[0] == 403
print("PASS: write method forbidden")
