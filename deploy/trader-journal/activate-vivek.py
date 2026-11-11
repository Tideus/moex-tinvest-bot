"""Site-specific reviewed activation; no trading services or configuration writes."""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit("Root required")
site = Path("/etc/nginx/sites-enabled/breakwood-compat").resolve(strict=True)
text = site.read_text()
anchor = "    server_name breakwood.ru;\n"
include = "    include /etc/nginx/snippets/moex-trader-journal.conf;\n"
if text.count(anchor) != 1 or "listen 443 ssl;" not in text:
    raise SystemExit("Unexpected nginx config; manual review required")
backup = Path("/var/backups/moex-trader-journal") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
backup.mkdir(parents=True, mode=0o700)
os.chmod(backup.parent, 0o700)
shutil.copy2(site, backup / "breakwood-compat")
if include not in text:
    site.write_text(text.replace(anchor, anchor + "\n" + include))
try:
    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True)
except subprocess.CalledProcessError:
    shutil.copy2(backup / "breakwood-compat", site)
    raise SystemExit("nginx activation failed; original config restored") from None
subprocess.run(["systemctl", "start", "moex-trader-journal.service"], check=True)
subprocess.run(["systemctl", "enable", "--now", "moex-trader-journal.timer"], check=True)
print("Journal activated; nginx backup:", backup)
