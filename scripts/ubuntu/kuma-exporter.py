"""Pilot Push adapter: read-only probes, then local Kuma Push. Never places orders."""
import argparse
import grp
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

from moex_bot.monitoring_labels import API_LABELS

NAMES = ("Observer heartbeat", "Resources", "Timers and services",
         "API integrations", "Trading data")
CONFIG = Path("/etc/moex-observer/kuma-push.json")


def private_config_write(path, content, group):
    """No world-readable creation window, symlink following or partial secret file."""
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("private config requires root-owned non-writable parent directories")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("private config target must be a regular file, not a symlink")
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), 0, group)
            os.fchmod(stream.fileno(), 0o640)
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provision", action="store_true")
    args = parser.parse_args()
    if args.provision:
        with sqlite3.connect("file:/opt/moex-monitoring/data/kuma.db?mode=ro", uri=True) as db:
            tokens = dict(db.execute("SELECT name,push_token FROM monitor "
                                     "WHERE type='push' AND active=1"))
        aliases = {
            "Observer heartbeat": "MOEX Trading / Observer heartbeat",
            "Resources": "Server / VM disk, inodes and available RAM",
            "Timers and services": "MOEX Trading / Trading timers and services",
            "API integrations": "MOEX Trading / All API sources (aggregate)",
            "Trading data": "MOEX Trading / Data freshness, calendar and report queue",
        }
        selected = {name: tokens.get(aliases[name]) or tokens["MOEX / " + name]
                    for name in NAMES}
        # Dedicated monitors expose the failing API/environment without secrets.
        for key in API_LABELS:
            matches = [value for name, value in tokens.items()
                       if name == "MOEX / " + key or name.endswith("[" + key + "]")]
            if len(matches) > 1:
                raise ValueError("duplicate API monitor destinations")
            if matches:
                selected[key] = matches[0]
        CONFIG.parent.mkdir(mode=0o750, exist_ok=True)
        if CONFIG.parent.is_symlink():
            raise ValueError("private config directory must not be a symlink")
        group = grp.getgrnam("moexbot").gr_gid
        os.chown(CONFIG.parent, 0, group)
        private_config_write(CONFIG, json.dumps(selected), group)
        print(f"Provisioned {len(selected)} Push destinations; secrets not displayed")
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
    for key in API_LABELS:
        if key in tokens:
            groups[key] = (key,)
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
