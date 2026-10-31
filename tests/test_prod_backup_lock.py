"""Backups exclude manual PROD sessions without deadlocking an inherited update FD."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_backup_checks_lock_before_creating_any_archive():
    script = (ROOT / "scripts/ubuntu/backup.sh").read_text(encoding="utf-8")
    assert script.index("flock -n 8") < script.index('mkdir -p "${BACKUP_DIR}"')
    assert '"${control_lock}" -ef /proc/self/fd/9' in script
    assert "flock -n 9" in script and "exec 8<>" in script
    assert "MOEX_BOT_LOCK_HELD" not in script
    assert "-L" in script and "stat -c %h" in script and "stat -c %u" in script


@pytest.mark.skipif(sys.platform == "win32", reason="native Linux bash/flock integration")
def test_backup_busy_and_inherited_updater_descriptor(tmp_path):
    import fcntl

    if os.getuid() != 0:
        pytest.skip("fixture validates real root ownership, run Linux deployment tests as root")
    control = tmp_path / "control"
    control.mkdir(mode=0o750)
    lock = control / "prod-session.lock"
    lock.touch(mode=0o660)
    script = (ROOT / "scripts/ubuntu/backup.sh").read_text(encoding="utf-8")
    # Run the exact lock guard against isolated fixture paths, never production files.
    guard = script.split('mkdir -p "${BACKUP_DIR}"', 1)[0]
    guard = guard.replace("/var/lib/moex-tinvest-control", str(control))
    with lock.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(
            ["bash", "-c", guard + "\nprintf entered"], capture_output=True, text=True
        )
        assert blocked.returncode == 2 and "entered" not in blocked.stdout
    passed = subprocess.run(
        ["bash", "-c", guard + "\nprintf entered"], capture_output=True, text=True
    )
    assert passed.returncode == 0 and passed.stdout == "entered"
    inherited = subprocess.run(
        [
            "bash",
            "-c",
            'exec 9<>"'
            + str(lock)
            + '"; flock -n 9; '
            + "bash -c "
            + shlex.quote(guard + "\nprintf inherited"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inherited.returncode == 0 and inherited.stdout == "inherited"
