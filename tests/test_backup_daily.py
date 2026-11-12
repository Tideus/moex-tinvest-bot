import importlib.util
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from moex_bot.storage_maintenance import _old_backups

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "snapshot_state", ROOT / "scripts/ubuntu/snapshot_state.py")
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def test_online_wal_snapshot_includes_committed_rows_without_copying_wal(tmp_path):
    source, target = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    path = source / "state.sqlite3"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE orders(value TEXT)")
        writer.execute("INSERT INTO orders VALUES ('original')")
        writer.commit()
        (source / "artifact.json").write_text('{"unchanged":true}')
        snapshot.snapshot(source, target)
        with sqlite3.connect(target / path.name) as copy:
            assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert copy.execute("SELECT * FROM orders").fetchall() == [("original",)]
        assert writer.execute("SELECT * FROM orders").fetchall() == [("original",)]
    assert not list(target.glob("*-wal"))
    assert not list(target.glob("*-shm"))
    assert (target / "artifact.json").read_bytes() == (source / "artifact.json").read_bytes()


@pytest.mark.parametrize("mode", ["corrupt", "orphan", "target_inside", "target_exists"])
def test_snapshot_fails_closed(tmp_path, mode):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "copy"
    if mode == "corrupt":
        (source / "broken.sqlite3").write_bytes(b"not SQLite")
    elif mode == "orphan":
        (source / "missing.db-wal").write_bytes(b"not WAL")
    elif mode == "target_inside":
        target = source / "copy"
    else:
        target.mkdir()
    with pytest.raises((ValueError, sqlite3.Error)):
        snapshot.snapshot(source, target)


@pytest.mark.skipif(os.name == "nt", reason="Linux symlink safety")
def test_snapshot_refuses_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "secret").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="linked"):
        snapshot.snapshot(source, tmp_path / "copy")


@pytest.mark.skipif(os.name == "nt", reason="Linux SQLite sidecar permissions")
def test_quiescent_wal_requires_sidecar_access_but_does_not_change_database(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("permission regression must run as an unprivileged user")
    source = tmp_path / "state"
    source.mkdir()
    database = source / "state.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE positions(quantity INTEGER)")
        db.execute("INSERT INTO positions VALUES (17)")
        db.commit()
    assert not database.with_name(database.name + "-wal").exists()
    original = database.read_bytes()
    database.chmod(0o444)
    source.chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError,
                           match="unable to open database file|readonly database"):
            snapshot.snapshot(source, tmp_path / "denied")
    finally:
        source.chmod(0o755)
    snapshot.snapshot(source, tmp_path / "allowed")
    assert database.read_bytes() == original
    with closing(sqlite3.connect(tmp_path / "allowed/state.sqlite3")) as db:
        assert db.execute("SELECT quantity FROM positions").fetchall() == [(17,)]
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_daily_maintenance_requires_backup_first():
    unit = (ROOT / "deploy/ubuntu/moex-tinvest-maintenance.service").read_text()
    assert "Requires=moex-tinvest-backup.service" in unit
    assert "After=moex-tinvest-backup.service" in unit
    backup = (ROOT / "deploy/ubuntu/moex-tinvest-backup.service").read_text()
    assert "ReadOnlyPaths=/var/lib/moex-tinvest-bot" not in backup
    assert "ReadWritePaths=/var/lib/moex-tinvest-bot /var/backups" in backup
    assert 'path.as_uri() + "?mode=ro"' in (
        ROOT / "scripts/ubuntu/snapshot_state.py").read_text()
    assert "EnvironmentFile" not in backup
    script = (ROOT / "scripts/ubuntu/backup.sh").read_text()
    assert script.index("snapshot_state.py") < script.index('mv -- "${stage_dir}/state.tar.gz"')
    assert (script.index('mv -- "${stage_dir}/state.tar.gz"')
            < script.index('mv -- "${stage_dir}/checksum"'))
    assert "moex-tinvest-backup.service" in (ROOT / "scripts/ubuntu/install.sh").read_text()


def test_retention_keeps_full_recovery_sets_not_only_three_sidecars(tmp_path):
    now = datetime.now(UTC)
    sets = []
    for day in range(1, 5):
        members = []
        for suffix in ('', '.prod-control', '.sandbox-control'):
            path = tmp_path / f'moex-tinvest-2026010{day}T000000Z{suffix}.tar.gz'
            path.write_bytes(b'backup')
            if not suffix:
                path.with_name(path.name + '.sha256').write_text('fixture')
            at = (now - timedelta(days=150-day)).timestamp()
            os.utime(path, (at, at))
            members.append(path)
        sets.append(members)
    removals = _old_backups(tmp_path, now, 90, 3)
    assert set(removals) == set(sets[0]) | {sets[0][0].with_name(sets[0][0].name + '.sha256')}
    assert all(p not in removals for members in sets[1:] for p in members)
