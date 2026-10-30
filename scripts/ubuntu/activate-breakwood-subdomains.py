"""Explicit public HTTPS cutover, gated on native authentication; archives old configs."""
import argparse
import json
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

AVAILABLE = Path("/etc/nginx/sites-available")
ENABLED = Path("/etc/nginx/sites-enabled")


def replace_block(text: str, marker: str, replacement: str) -> str:
    if text.count(marker) != 1:
        raise ValueError("unexpected legacy location layout")
    start = text.index(marker)
    first = text.index("{", start)
    depth = 0
    for index in range(first, len(text)):
        depth += (text[index] == "{") - (text[index] == "}")
        if depth == 0:
            return text[:start] + replacement + text[index + 1:]
    raise ValueError("unclosed legacy block")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    args = parser.parse_args()
    gui = ET.parse("/var/lib/syncthing/.local/state/syncthing/config.xml").getroot().find("gui")
    if gui is None or not gui.findtext("user") or not gui.findtext("password"):
        raise ValueError("refusing public Syncthing without native login/password")
    if gui.findtext("insecureAdminAccess", "false").lower() == "true":
        raise ValueError("insecureAdminAccess is enabled")
    with sqlite3.connect("file:/opt/moex-monitoring/data/kuma.db?mode=ro", uri=True) as db:
        if db.execute("SELECT count(*) FROM user WHERE active=1").fetchone()[0] < 1:
            raise ValueError("refusing public Kuma before admin setup")
        disabled = db.execute("SELECT value FROM setting WHERE key='disableAuth'").fetchone()
        if disabled and json.loads(disabled[0]) is True:
            raise ValueError("Kuma authentication disabled")
    legacy = AVAILABLE / "obsidian-livesync"
    compat = AVAILABLE / "breakwood-compat.conf"
    active = AVAILABLE / "breakwood-services.conf"
    if compat.exists() or active.exists():
        raise ValueError("cutover paths already exist; inspect instead of overwriting")
    content = legacy.read_text()
    content = replace_block(content, "location = /syncthing {",
                            "location = /syncthing { return 308 https://sync.breakwood.ru/; }")
    content = replace_block(content, "location /syncthing/ {", """location /syncthing/ {
        rewrite ^/syncthing/(.*)$ https://sync.breakwood.ru/$1 permanent;
    }""")
    backup = Path("/var/backups/nginx-breakwood") / (
        "public-cutover-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(mode=0o750)
    old_targets = {path: path.readlink() for path in (
        ENABLED / "obsidian-livesync", ENABLED / "breakwood-services")}
    for path in (legacy, AVAILABLE / "breakwood-services",
                 AVAILABLE / "breakwood-services.tls.pending",
                 AVAILABLE / "obsidian-livesync.backup"):
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, backup / path.name)
    try:
        compat.write_text(content)
        active.write_text(args.template.read_text())
        (ENABLED / "obsidian-livesync").unlink()
        (ENABLED / "breakwood-compat").symlink_to(compat)
        (ENABLED / "breakwood-services").unlink()
        (ENABLED / "breakwood-services").symlink_to(active)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    except Exception:
        (ENABLED / "breakwood-compat").unlink(missing_ok=True)
        for path, target in old_targets.items():
            path.unlink(missing_ok=True)
            path.symlink_to(target)
        compat.unlink(missing_ok=True)
        active.unlink(missing_ok=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        raise
    # Only the four exact retired config files, never data or shared snippets.
    for name in ("obsidian-livesync", "breakwood-services", "breakwood-services.tls.pending",
                 "obsidian-livesync.backup"):
        path = AVAILABLE / name
        if path.is_file() and not path.is_symlink() and (backup / name).is_file():
            path.unlink()
    print(f"Public HTTPS cutover complete. Retired configs archived: {backup}")


if __name__ == "__main__":
    main()
