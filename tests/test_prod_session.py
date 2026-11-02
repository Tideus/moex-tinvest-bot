"""Production session/update locking; platform-independent native flock stubs."""

import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from moex_bot import long_auto_control, prod_session


@pytest.mark.parametrize(
    "fault",
    [
        "inherited",
        "direct",
        "unrelated",
        "body",
        "import",
        "missing-parent",
        "symlink",
        "nondirectory",
        "uid",
        "mode",
        "dup-error",
        "inherited-stat-error",
        "missing-directory-flag",
        "missing-nofollow",
        "open-error",
        "opened-stat-error",
        "replaced",
        "busy",
        "race",
    ],
)
def test_long_writer_fd_capability_ownership_and_cleanup(monkeypatch, fault):
    import errno
    import os

    control = Path("/var/lib/moex-tinvest-control")
    info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0, st_dev=1, st_ino=44)
    different = SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0, st_dev=1, st_ino=99)
    leaf_reads = 0

    def lstat(p):
        nonlocal leaf_reads
        if p == control.parent:
            if fault == "missing-parent":
                raise FileNotFoundError()
            mode = {
                "symlink": stat.S_IFLNK | 0o777,
                "nondirectory": stat.S_IFREG | 0o600,
                "mode": stat.S_IFDIR | 0o770,
            }.get(fault, info.st_mode)
            return SimpleNamespace(st_mode=mode, st_uid=1000 if fault == "uid" else 0)
        if p == control:
            leaf_reads += 1
            if fault == "race" and leaf_reads > 1:
                return different
        return info

    inherited = fault in ("inherited", "unrelated", "inherited-stat-error")

    def duplicate(fd):
        assert fd == 7
        if inherited:
            return 20
        raise OSError(errno.ENFILE if fault == "dup-error" else errno.EBADF, "fixture")

    def fstat(fd):
        if fd == 20:
            if fault == "inherited-stat-error":
                raise OSError("inherited fstat")
            return different if fault == "unrelated" else info
        assert fd == 21
        if fault == "opened-stat-error":
            raise OSError("new fstat")
        return different if fault == "replaced" else info

    platform = SimpleNamespace(
        dup=duplicate,
        fstat=fstat,
        path=os.path,
        O_RDONLY=0,
        O_DIRECTORY=0 if fault == "missing-directory-flag" else 128,
        O_NOFOLLOW=0 if fault == "missing-nofollow" else 256,
        open=Mock(return_value=21, side_effect=OSError() if fault == "open-error" else None),
        close=Mock(),
    )
    lock = Mock(side_effect=BlockingIOError() if fault == "busy" else None)
    kernel = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lock)
    monkeypatch.setattr(long_auto_control, "CONTROL_DIR", control)
    monkeypatch.setattr(long_auto_control, "os", platform)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(
        long_auto_control,
        "import_module",
        Mock(return_value=kernel, side_effect=ImportError() if fault == "import" else None),
    )
    if fault in {"direct", "inherited", "unrelated"}:
        with long_auto_control.writer_lock():
            lock.assert_called_once_with(20 if fault == "inherited" else 21, 6)
    elif fault == "body":
        with pytest.raises(RuntimeError), long_auto_control.writer_lock():
            raise RuntimeError()
    else:
        with pytest.raises((ValueError, OSError)), long_auto_control.writer_lock():
            pytest.fail("unsafe lock entered")
    closed = [call.args[0] for call in platform.close.call_args_list]
    expected = {"inherited": [20], "unrelated": [20, 21], "inherited-stat-error": [20]}
    needs_close = fault in {"direct", "body", "opened-stat-error", "replaced", "busy", "race"}
    assert closed == expected.get(fault, [21] if needs_close else [])
    assert lock.call_count <= 1  # no premature explicit LOCK_UN on the parent's description


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
@pytest.mark.parametrize("busy", ["production", "long", "neither"])
def test_existing_install_locks_before_any_mutating_command(tmp_path, busy):
    import fcntl
    import os

    # Run the real installer guard AND its sourced helper under an isolated
    # filesystem prefix. Only hardcoded root path literals are mapped; both
    # shell flock calls compete with actual independently-held kernel locks.
    fake_root = tmp_path / "root"
    control = fake_root / "var/lib/moex-tinvest-control"
    control.mkdir(mode=0o750, parents=True)
    lock = control / "prod-session.lock"
    lock.touch(mode=0o660)
    original = Path(__file__).resolve().parents[1] / "scripts/ubuntu"
    project = tmp_path / "checkout"
    helper = project / "scripts/ubuntu/lock-long-writer.sh"
    helper.parent.mkdir(parents=True)

    def isolate(text):
        text = text.replace(
            "in / /var /var/lib ",
            "in " + " ".join(shlex.quote(str(fake_root / p)) for p in ("", "var", "var/lib")) + " ",
        )
        return text.replace("=/var/lib/moex-tinvest-control", "=" + shlex.quote(str(control)))

    helper.write_text(isolate((original / helper.name).read_text(encoding="utf-8")))
    script = (original / "install.sh").read_text(encoding="utf-8")
    start = script.index("# Existing installations must exclude PROD")
    end = script.index('if [[ -z "${DESTDIR}" && "${INSTALL_PACKAGES}" -eq 1 ]]')
    guard = "set -Eeuo pipefail\nDESTDIR=''\nPROJECT_DIR=" + shlex.quote(str(project)) + "\n"
    if os.getuid() != 0:
        # Unprivileged CI: translate *only* fixture owner's UID to simulated
        # container-root. Type, links, permissions and all locks remain native.
        guard += """
stat() {
  local result
  result="$(command stat "$@")" || return 1
  if [[ "$2" == %u && "$result" == "$UID" && "$3" == "$FIXTURE_ROOT"* ]]; then
    printf '0\\n'
  else
    printf '%s\\n' "$result"
  fi
}
"""
    guard += isolate(script[start:end]) + "\nprintf MUTATION"
    fd = os.open(lock if busy == "production" else control, os.O_RDONLY)
    try:
        if busy != "neither":
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["bash", "-c", guard],
            capture_output=True,
            text=True,
            env={**os.environ, "FIXTURE_ROOT": str(fake_root)},
            timeout=10,
        )
    finally:
        os.close(fd)
    if busy == "neither":
        assert result.returncode == 0 and result.stdout == "MUTATION", result.stderr
    else:
        message = "production session active" if busy == "production" else "sandbox long writer"
        assert result.returncode == 2 and not result.stdout and message in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Linux descriptor inheritance and flock")
@pytest.mark.parametrize("case", ["busy", "unrelated-fd-busy", "direct", "inherited"])
def test_operator_writer_lock_native(tmp_path, case):
    """Use a child process so mapping FD7 can never clobber pytest's descriptors."""
    import os

    root = Path(__file__).resolve().parents[1]
    directory = tmp_path / "control"
    directory.mkdir(mode=0o750)
    code = r"""
import os, sys, subprocess, stat
from pathlib import Path
from moex_bot import long_auto_control as mod
mod.CONTROL_DIR = directory = Path(sys.argv[1])
case = sys.argv[2]
# Model only owner/mode of the tmp fixture's parents. Device/inode, directory
# open, FD inheritance, flock contention, and close are real kernel operations.
native = Path.lstat
def trusted(path):
    result = native(path)
    fields = list(result)
    fields[4] = 0
    if path != directory:
        fields[0] = stat.S_IFDIR | 0o755
    return type(result)(fields)
Path.lstat = trusted
def available():
    return subprocess.run(["flock", "-n", str(directory), "true"]).returncode == 0
if case in ("busy", "unrelated-fd-busy"):
    try:
        with mod.writer_lock():
            raise AssertionError("must refuse competing backup")
    except ValueError as exc:
        assert "already active" in str(exc), str(exc)
else:
    with mod.writer_lock():
        assert not available()
    assert available() == (case == "direct")
"""
    cmd = [sys.executable, "-c", code, str(directory), case]
    if case == "busy":
        cmd = ["flock", "-xn", str(directory), *cmd]
    elif case == "unrelated-fd-busy":
        cmd = [
            "flock",
            "-xn",
            str(directory),
            "bash",
            "-c",
            'exec 7<"$1"; shift; exec "$@"',
            "test",
            str(tmp_path),
            *cmd,
        ]
    elif case == "inherited":
        cmd = [
            "bash",
            "-c",
            'exec 7<"$1"; flock -xn 7 || exit 2; shift; exec "$@"',
            "test",
            str(directory),
            *cmd,
        ]
    run = subprocess.run(
        cmd,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="native coordinated tar backup")
def test_ordinary_backup_rejects_long_writer_and_preserves_ledger(tmp_path):
    import fcntl
    import os
    import tarfile

    root = Path(__file__).resolve().parents[1]
    fake_root = tmp_path / "root"
    control = fake_root / "var/lib/moex-tinvest-control"
    control.mkdir(mode=0o750, parents=True)
    (control / "prod-session.lock").touch(mode=0o660)
    anchor = control / "long-once-sandbox/entry.claim"
    anchor.parent.mkdir()
    anchor.write_text("independent witness")
    state, backups = tmp_path / "state", tmp_path / "backups"
    ledger = state / "long-once/sandbox.sqlite3"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"test ledger content")
    script = (root / "scripts/ubuntu/backup.sh").read_text()
    for path in ("in / /var /var/lib ", "/etc/moex-tinvest-bot", "/var/lib/moex-tinvest-control"):
        replacement = (
            "in " + " ".join(shlex.quote(str(fake_root / p)) for p in ("", "var", "var/lib")) + " "
            if path.startswith("in ")
            else str(fake_root / path[1:])
        )
        script = script.replace(path, replacement)
    if os.getuid() != 0:
        script = script.replace(
            "umask 077",
            """umask 077
stat() {
  local value
  value="$(command stat "$@")" || return 1
  if [[ "$2" == %u && "$value" == "$UID" && "$3" == "$FIXTURE_ROOT"* ]]; then
    printf '0\\n'
  else printf '%s\\n' "$value"; fi
}""",
        )
    env = {
        **os.environ,
        "FIXTURE_ROOT": str(fake_root),
        "MOEX_BOT_STATE_DIR": str(state),
        "MOEX_BOT_BACKUP_DIR": str(backups),
    }
    fd = os.open(control, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=10
        )
        assert busy.returncode == 2 and "sandbox long writer" in busy.stderr
        assert not backups.exists()
    finally:
        os.close(fd)
    cmd = [
        "bash",
        "-c",
        'exec 7<"$1"; flock -xn 7 || exit 2; bash -c "$2"',
        "test",
        str(control),
        script,
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)
    assert done.returncode == 0, done.stderr
    with tarfile.open(Path(done.stdout.strip())) as archive:
        assert archive.extractfile("./long-once/sandbox.sqlite3").read() == ledger.read_bytes()
        assert all("entry.claim" not in name for name in archive.getnames())
