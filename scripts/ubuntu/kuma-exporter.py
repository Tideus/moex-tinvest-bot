"""Pilot Push adapter: read-only probes, then local Kuma Push. Never places orders."""
import argparse
import grp
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

NAMES = ("Observer heartbeat", "Resources", "Timers and services",
         "API integrations", "Trading data")
CONFIG = Path("/etc/moex-observer/kuma-push.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provision", action="store_true")
    args = parser.parse_args()
    if args.provision:
        with sqlite3.connect("file:/opt/moex-monitoring/data/kuma.db?mode=ro", uri=True) as db:
            tokens = dict(db.execute("SELECT name,push_token FROM monitor "
                                     "WHERE type='push' AND active=1"))
        selected = {name: tokens["MOEX / " + name] for name in NAMES}
        CONFIG.parent.mkdir(mode=0o750, exist_ok=True)
        CONFIG.write_text(json.dumps(selected))
        CONFIG.chmod(0o640)
        os.chown(CONFIG, 0, grp.getgrnam("moexbot").gr_gid)
        os.chown(CONFIG.parent, 0, grp.getgrnam("moexbot").gr_gid)
        print("Provisioned five Push destinations; secrets not displayed")
        return 0
    tokens = json.loads(CONFIG.read_text())
    started = datetime.now(UTC)
    completed = subprocess.run(
        [sys.executable, "-m", "moex_bot.monitoring", "--collect-only"],
        capture_output=True, timeout=240,
    )
    if completed.returncode not in (0, 2):
        print("FAIL: collector did not complete")
        return 2
    snapshot = json.loads(Path(
        "/var/lib/moex-tinvest-bot/artifacts/monitoring-latest.json").read_text())
    if datetime.fromisoformat(snapshot["observed_at"]) < started:
        print("FAIL: collector snapshot is not from this run")
        return 2
    checks = snapshot["checks"]
    groups = {
        "Resources": ("disk:", "inodes:", "memory:", "collector:resources"),
        "Timers and services": ("timer:", "service:", "collector:services"),
        "API integrations": ("api:", "collector:integrations"),
        "Trading data": ("data:", "quality:", "backup:", "telegram:", "intraday:",
                         "collector:artifacts", "monitor:collector"),
        "Observer heartbeat": (),
    }
    for name, prefixes in groups.items():
        selected = {key: value for key, value in checks.items() if key.startswith(prefixes)}
        problems = [f"{key}: {value}" for key, value in selected.items() if value != "OK"]
        if name != "Observer heartbeat" and not selected:
            problems.append("Missing expected metrics")
        query = urlencode({"status": "down" if problems else "up",
                           "msg": "; ".join(problems)[:800] or "OK"})
        try:
            with urlopen("http://127.0.0.1:3001/api/push/" + tokens[name] + "?" + query,
                         timeout=10) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise ValueError("Push rejected")
        except Exception:
            print(f"FAIL: Push {name}; inspect Kuma (URL omitted)")
            return 2
        print(f"PUSH {name}: {'DOWN' if problems else 'UP'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
