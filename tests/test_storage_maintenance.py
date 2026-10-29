import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from moex_bot.storage_maintenance import (
    StoragePolicy,
    StorageUsage,
    maintain_storage,
)

NOW = datetime(2026, 10, 30, 3, 40, tzinfo=UTC)
POLICY = StoragePolicy(True, 75, 80, 90, 100, 14, 90, 90, 2)


def _old(path: Path, *, days: int = 120) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def test_cleanup_removes_only_expired_files_and_preserves_databases(tmp_path: Path) -> None:
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    backups = tmp_path / "backups"
    database = state / "data" / "notifications.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_text("active", encoding="utf-8")
    old_artifact = state / "artifacts" / "old.json"
    old_log = logs / "old.log"
    _old(old_artifact)
    _old(old_log)
    for index in range(4):
        archive = backups / f"moex-tinvest-20260{index + 1}01T000000Z.tar.gz"
        _old(archive, days=120 - index)
        _old(archive.with_suffix(archive.suffix + ".sha256"), days=120 - index)

    usages = iter((StorageUsage(1000, 850, 150), StorageUsage(1000, 700, 300)))
    result = maintain_storage(
        policy=POLICY,
        filesystem_path=tmp_path,
        artifacts_dir=state / "artifacts",
        logs_dir=logs,
        backups_dir=backups,
        apply=True,
        now=NOW,
        usage_reader=lambda _path: next(usages),
    )

    assert result.cleanup_triggered
    assert not result.critical
    assert not old_artifact.exists()
    assert not old_log.exists()
    assert database.exists()
    assert len(list(backups.glob("*.tar.gz"))) == 2
    assert len(list(backups.glob("*.sha256"))) == 2


def test_cleanup_does_nothing_below_threshold(tmp_path: Path) -> None:
    old_log = tmp_path / "logs" / "old.log"
    _old(old_log)
    result = maintain_storage(
        policy=POLICY,
        filesystem_path=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        apply=True,
        now=NOW,
        usage_reader=lambda _path: StorageUsage(1000, 500, 500),
    )
    assert not result.cleanup_triggered
    assert old_log.exists()


def test_dry_run_never_deletes_candidates(tmp_path: Path) -> None:
    old_log = tmp_path / "logs" / "old.log"
    _old(old_log)
    result = maintain_storage(
        policy=POLICY,
        filesystem_path=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        apply=False,
        now=NOW,
        usage_reader=lambda _path: StorageUsage(1000, 850, 150),
    )
    assert result.cleanup_triggered
    assert result.deleted_files == 0
    assert old_log.exists()
