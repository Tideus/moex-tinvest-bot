"""Explicit one-off migration for vivek. Does not change bot or MinIO data."""
from __future__ import annotations

import argparse
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SITE = Path("/etc/nginx/sites-available/obsidian-livesync")
STATIC = Path("/var/www/mdl-web/mdl-static")
SNIPPET = Path("/etc/nginx/snippets/mdl-static.conf")
AVAILABLE = Path("/etc/nginx/sites-available/breakwood-services")
PENDING = Path("/etc/nginx/sites-available/breakwood-services.tls.pending")
ENABLED = Path("/etc/nginx/sites-enabled/breakwood-services")
NEEDLE = "include /etc/nginx/snippets/mdl-static.conf;"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    original = SITE.read_text()
    if original.count(NEEDLE) != 1:
        raise ValueError("expected exactly one legacy static include; refusing migration")
    if any(path.exists() or path.is_symlink() for path in (AVAILABLE, ENABLED, PENDING)):
        raise ValueError("new site paths already exist; inspect instead of overwriting")
    if SITE.is_symlink() or not STATIC.is_dir() or STATIC.resolve() != STATIC:
        raise ValueError("unexpected site or static path")
    if STATIC.is_mount() or any(p.is_symlink() or p.is_mount() for p in STATIC.rglob("*")):
        raise ValueError("static tree has links/mounts; refusing recursive removal")
    http = (args.templates / "breakwood-services-http.conf").read_text()
    tls = (args.templates / "breakwood-services-tls.conf").read_text()
    print(f"Verified removal target: {STATIC}")
    if not args.apply:
        print("DRY RUN: nginx config backup, bootstrap sites, static removal")
        return
    backup = Path("/var/backups/nginx-breakwood") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(SITE, backup / SITE.name)
    shutil.copy2(SNIPPET, backup / SNIPPET.name)
    tombstone = ("location = /mdl-static { return 410; }\n"
                 "    location ^~ /mdl-static/ { return 410; }")
    try:
        SITE.write_text(original.replace(NEEDLE, tombstone))
        AVAILABLE.write_text(http)
        PENDING.write_text(tls)
        ENABLED.symlink_to(AVAILABLE)
        Path("/var/www/letsencrypt/.well-known/acme-challenge").mkdir(parents=True, exist_ok=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    except Exception:
        shutil.copy2(backup / SITE.name, SITE)
        for path in (ENABLED, AVAILABLE, PENDING):
            path.unlink(missing_ok=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        raise
    # Exact absolute target verified above. No MinIO paths or other web roots are removed.
    shutil.rmtree(STATIC)
    SNIPPET.unlink()
    if not list(STATIC.parent.iterdir()):
        STATIC.parent.rmdir()
    print(f"REMOVED: {STATIC}; no asset backup created. Config backup: {backup}")
    print("Bootstrap enabled; TLS pending DNS/certificate/admin ACL. Existing endpoints preserved.")


if __name__ == "__main__":
    main()
