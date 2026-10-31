import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from moex_bot import storage_maintenance as storage

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "field,value",
    [
        (None, []),
        ("enabled", "yes"),
        ("warning_used_percent", 90),
        ("minimum_free_bytes", -1),
        ("log_retention_days", 0),
        ("artifact_retention_days", 0),
        ("backup_retention_days", 0),
        ("keep_min_backups", 0),
        ("keep_min_backups", 101),
        ("keep_min_backups", True),
    ],
)
def test_storage_policy_validation(tmp_path, field, value):
    raw = json.loads((ROOT / "config/maintenance.json").read_text())
    raw = value if field is None else raw | {field: value}
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        storage.load_storage_policy(path)


def test_storage_helpers_missing_directories(tmp_path):
    missing = tmp_path / "missing"
    assert storage.directory_bytes(missing) == 0
    assert storage._old_files(missing, datetime.now(UTC), 1) == []
    assert storage._old_backups(missing, datetime.now(UTC), 1, 1) == []
    assert storage.StorageUsage(0, 0, 0).used_percent == 0
    (tmp_path / "file").write_bytes(b"123")
    (tmp_path / "dir").mkdir()
    assert storage.directory_bytes(tmp_path) == 3
    assert storage.storage_usage(tmp_path).total > 0
    assert storage._old_files(tmp_path, datetime.now(UTC), 1) == []


def test_storage_rejects_naive_clock_and_ignores_disappeared_candidates(tmp_path, monkeypatch):
    policy = storage.load_storage_policy(ROOT / "config/maintenance.json")
    kwargs = dict(
        policy=policy,
        filesystem_path=tmp_path,
        artifacts_dir=tmp_path,
        logs_dir=tmp_path,
        backups_dir=tmp_path,
        apply=True,
        usage_reader=lambda p: storage.StorageUsage(1000, 999, 1),
    )
    with pytest.raises(ValueError, match="timezone"):
        storage.maintain_storage(**kwargs, now=datetime(2026, 1, 1))
    missing = tmp_path / "disappeared"
    monkeypatch.setattr(storage, "_old_files", lambda *a: [missing])
    monkeypatch.setattr(storage, "_old_backups", lambda *a: [])
    result = storage.maintain_storage(**kwargs, now=datetime.now(UTC))
    assert result.critical and result.deleted_files == 0
