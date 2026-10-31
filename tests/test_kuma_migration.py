import importlib.util
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def migration():
    script = Path(__file__).resolve().parents[1] / "scripts/ubuntu/migrate-kuma-monitors.py"
    spec = importlib.util.spec_from_file_location("kuma_migration", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["Syncthing GUI", "MOEX / Syncthing GUI"])
def database(tmp_path, request):
    path = tmp_path / "kuma.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE monitor(
            id INTEGER PRIMARY KEY, name TEXT, type TEXT, parent INTEGER, active INTEGER,
            user_id INTEGER, interval INTEGER, description TEXT, push_token TEXT,
            maxretries INTEGER, retry_interval INTEGER);
            CREATE TABLE monitor_notification(id INTEGER PRIMARY KEY,
                monitor_id INTEGER, notification_id INTEGER);""")
        db.executemany(
            "INSERT INTO monitor VALUES(?,?,?,NULL,1,1,60,NULL,?,1,60)",
            [
                (1, "MOEX / API integrations", "push", "PRIVATE"),
                (2, "MOEX / Resources", "push", "PRIVATE2"),
                (3, request.param, "http", None),
                (4, "MOEX / MinIO S3", "http", None),
            ],
        )
        db.execute("INSERT INTO monitor_notification VALUES(1,1,2)")
    return path


def test_offline_migration_preserves_and_is_idempotent(migration, database, tmp_path, monkeypatch):
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *a, **k: "false\n")
    before = database.read_bytes()
    assert len(migration.migrate(database, tmp_path / "dry.db")) == 16
    assert database.read_bytes() == before
    assert not (tmp_path / "dry.db").exists()
    actions = migration.migrate(database, tmp_path / "backup.db", apply=True)
    assert len(actions) == 16
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM monitor").fetchone()[0] == 16
        assert db.execute("SELECT push_token FROM monitor WHERE id=1").fetchone()[0] == "PRIVATE"
        assert db.execute("SELECT count(*) FROM monitor_notification").fetchone()[0] == 10
        assert db.execute("SELECT parent FROM monitor WHERE id=3").fetchone()[0] is not None
        assert db.execute("SELECT name FROM monitor WHERE id=3").fetchone()[0] == (
            "Infrastructure / Syncthing GUI"
        )
    assert migration.migrate(database, tmp_path / "again.db", apply=True) == []
    with sqlite3.connect(tmp_path / "backup.db") as db:
        assert db.execute("SELECT count(*) FROM monitor").fetchone()[0] == 4


def test_running_kuma_and_existing_backup_block(migration, database, tmp_path, monkeypatch):
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *a, **k: "true")
    with pytest.raises(ValueError, match="stopped"):
        migration.migrate(database, tmp_path / "backup.db", apply=True)
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *a, **k: "false")
    with pytest.raises(ValueError, match="distinct"):
        migration.migrate(database, database, apply=True)


def test_unsupported_schema_no_mutation(migration, tmp_path):
    path = tmp_path / "bad.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE monitor(id INTEGER)")
    with pytest.raises(ValueError, match="schema"):
        migration.migrate(path, tmp_path / "backup.db")


def test_distinct_s3_endpoints_and_unrelated_sync_names_are_preserved(
    migration, database, tmp_path, monkeypatch
):
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *a, **k: "false")
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO monitor(id,name,type) VALUES(100,'MinIO S3 console','http')")
        db.execute("INSERT INTO monitor(id,name,type) VALUES(101,'Async job endpoint','http')")
    migration.migrate(database, tmp_path / "backup.db", apply=True)
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT name FROM monitor WHERE id=100").fetchone()[0] == "MinIO S3 console"
        )
        assert (
            db.execute("SELECT name FROM monitor WHERE id=101").fetchone()[0]
            == "Async job endpoint"
        )
        names = [r[0] for r in db.execute("SELECT name FROM monitor")]
        assert len(names) == len(set(names))
    assert migration.migrate(database, tmp_path / "again.db", apply=True) == []


def test_alias_collision_is_rejected_before_backup_or_mutation(
    migration, database, tmp_path, monkeypatch
):
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *a, **k: "false")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO monitor(id,name,type) VALUES(100,'Infrastructure / MinIO S3','http')"
        )
    before = database.read_bytes()
    backup = tmp_path / "backup.db"
    with pytest.raises(ValueError, match="collide"):
        migration.migrate(database, backup, apply=True)
    assert database.read_bytes() == before
    assert not backup.exists()
