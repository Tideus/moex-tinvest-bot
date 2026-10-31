"""Production session/update locking; platform-independent native flock stubs."""

import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from moex_bot import prod_session


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "import",
        "parent_symlink",
        "parent_owner",
        "parent_mode",
        "file_type",
        "file_owner",
        "file_mode",
        "file_links",
        "inode",
        "locked",
        "body",
    ],
)
def test_production_session_fails_closed_and_always_releases_descriptor(fault):
    path = Path("/var/lib/moex-tinvest-control/prod-session.lock")
    directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0)
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o660, st_uid=0, st_nlink=1, st_dev=1, st_ino=2)
    opened = SimpleNamespace(st_dev=1, st_ino=3 if fault == "inode" else 2)
    if fault == "parent_symlink":
        directory.st_mode = stat.S_IFLNK | 0o777
    elif fault == "parent_owner":
        directory.st_uid = 1000
    elif fault == "parent_mode":
        directory.st_mode |= 0o002
    elif fault == "file_type":
        info.st_mode = stat.S_IFLNK | 0o660
    elif fault == "file_owner":
        info.st_uid = 1000
    elif fault == "file_mode":
        info.st_mode |= 0o004
    elif fault == "file_links":
        info.st_nlink = 2
    flock = Mock(side_effect=BlockingIOError() if fault == "locked" else None)
    module = SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4)
    platform = SimpleNamespace(
        open=Mock(return_value=123),
        close=Mock(),
        fstat=Mock(return_value=opened),
        O_RDWR=2,
        O_NOFOLLOW=256,
    )

    def lstat(value):
        return info if value == path else directory

    with (
        patch.object(prod_session, "LOCK_PATH", path),
        patch.object(prod_session, "os", platform),
        patch.object(Path, "lstat", lstat),
        patch.object(
            prod_session,
            "import_module",
            side_effect=ImportError() if fault == "import" else None,
            return_value=module,
        ),
    ):
        if fault is None:
            with prod_session.production_session():
                flock.assert_called_once_with(123, 6)
        elif fault == "body":
            with pytest.raises(RuntimeError), prod_session.production_session():
                raise RuntimeError("session error")
        else:
            with pytest.raises(ValueError), prod_session.production_session():
                raise AssertionError("must not enter")
    if fault in {None, "inode", "locked", "body"}:
        platform.close.assert_called_once_with(123)
    else:
        platform.open.assert_not_called()


def test_deployment_lock_precedes_backup_pull_and_app_replacement():
    root = Path(__file__).resolve().parents[1]
    update = (root / "scripts/ubuntu/update.sh").read_text()
    assert update.index("flock -n 9") < update.index('"${APP_DIR}/scripts/ubuntu/backup.sh"')
    assert update.index("flock -n 9") < update.index("pull --ff-only") < update.index("rsync -a")
    assert "exec 9<>" in update and "set -o noclobber" in update
    assert "chmod 0660" in update and '-L "${CONTROL_LOCK}"' in update
    install = (root / "scripts/ubuntu/install.sh").read_text()
    assert install.index("flock -n 9") < install.index("rsync -a")
    assert "root_path /var/lib/moex-tinvest-control" in install
    assert install.index('exec 9<>"${early_control_lock}"') < install.index("apt-get update")
    assert install.index('exec 9<>"${early_control_lock}"') < install.index("useradd --system")
    assert 'if [[ ! -e /proc/self/fd/9 || ! "${control_lock}" -ef /proc/self/fd/9 ]]' in install


@pytest.mark.skipif(sys.platform == "win32", reason="native Linux flock interoperability")
def test_native_linux_lock_excludes_shell_deployer(tmp_path, monkeypatch):
    path = tmp_path / "prod-session.lock"
    path.touch(mode=0o660)
    native_lstat = Path.lstat

    def trusted_lstat(value):
        info = native_lstat(value)
        fields = list(info)
        fields[0] = stat.S_IFREG | 0o660 if value == path else stat.S_IFDIR | 0o750
        fields[4] = 0
        return type(info)(fields)

    monkeypatch.setattr(prod_session, "LOCK_PATH", path)
    monkeypatch.setattr(Path, "lstat", trusted_lstat)
    with prod_session.production_session():
        assert subprocess.run(["flock", "-n", str(path), "true"], check=False).returncode == 1
    assert subprocess.run(["flock", "-n", str(path), "true"], check=False).returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="native existing-install early flock")
def test_existing_install_locks_before_any_mutating_command(tmp_path):
    import fcntl
    import os

    if os.getuid() != 0:
        pytest.skip("fixture validates actual root ownership")
    control = tmp_path / "control"
    control.mkdir(mode=0o750)
    lock = control / "prod-session.lock"
    lock.touch(mode=0o660)
    script = (Path(__file__).resolve().parents[1] / "scripts/ubuntu/install.sh").read_text(
        encoding="utf-8"
    )
    start = script.index("# Existing installations must exclude PROD")
    end = script.index('if [[ -z "${DESTDIR}" && "${INSTALL_PACKAGES}" -eq 1 ]]')
    guard = "set -Eeuo pipefail\nDESTDIR=''\n" + script[start:end].replace(
        "/var/lib/moex-tinvest-control", str(control)
    )
    with lock.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(
            ["bash", "-c", guard + "\nprintf MUTATION"], capture_output=True, text=True
        )
        assert blocked.returncode == 2 and "MUTATION" not in blocked.stdout
    free = subprocess.run(["bash", "-c", guard + "\nprintf READY"], capture_output=True, text=True)
    assert free.returncode == 0 and free.stdout == "READY"
