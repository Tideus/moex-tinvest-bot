"""Root pre-step: read pinned deployment files, publish PASS/FAIL only. No broker or secrets."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_bot.admission_diagnostics import failure

BASE = Path('/var/lib/moex-long-readiness-20261108')
APP = Path('/opt/moex-tinvest-bot')


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    result: dict[str, Any] = {'checked_at': datetime.now(UTC).isoformat(), 'passed': False}
    try:
        raw = (BASE / 'review.json').read_bytes()
        review = json.loads(raw)
        result['review_sha256'] = digest(raw)
        manifest = (APP / 'BUILD_MANIFEST.sha256').read_bytes()
        if digest(manifest) != review['build_manifest_sha256']:
            raise ValueError('manifest changed')
        for line in manifest.decode().splitlines():
            sha, name = line.split(maxsplit=1)
            target = APP / name.strip().removeprefix('./')
            if (not target.resolve().is_relative_to(APP) or target.is_symlink()
                    or not target.is_file() or target.stat().st_size > 2_000_000
                    or digest(target.read_bytes()) != sha):
                raise ValueError('installed file changed')
        result['passed'] = True
    except Exception as exc:
        result.update(failure(exc, 'integrity_inputs'))
    output = BASE / 'integrity' / 'status.json'
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(result, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print('DEPLOYMENT_INTEGRITY_' + ('PASS' if result['passed'] else 'FAIL'))
    return 0  # FAIL is evidence for the unprivileged reporter, not a suppressed report.


if __name__ == '__main__':
    raise SystemExit(main())
