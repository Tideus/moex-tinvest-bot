"""One-time read-only acceptance on a candidate, not a production installation."""

import json
import os
from pathlib import Path

from moex_bot.journal_import import atomic, collect, import_artifacts
from moex_bot.trader_journal import Journal, build_report, encoded

base = Path(os.environ["JOURNAL_PROBE_DIR"])
config = json.loads(Path("deploy/trader-journal/config.example.json").read_bytes())
config.update(
    database=str(base / "journal.sqlite3"),
    collector_dir=str(base / "receipts"),
    output=str(base / "public"),
)
collect(config)
journal = Journal(Path(config["database"]))
status = import_artifacts(config, journal)
report = build_report(journal.latest())
report["import"] = status
atomic(base / "public/data.json", encoded(report))
print("ERRORS", status["errors"][:15], "COUNT", len(status["errors"]))
for stream in report["streams"]:
    print(
        stream["strategy"],
        stream["environment"],
        "operations",
        len(stream["operations"]),
        "closures",
        len(stream["trades"]),
        "warnings",
        stream["warnings"],
    )
