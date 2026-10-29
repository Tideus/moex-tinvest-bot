from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    enabled: bool
    warning_used_percent: int
    cleanup_used_percent: int
    critical_used_percent: int
    minimum_free_bytes: int
    log_retention_days: int
    artifact_retention_days: int
    backup_retention_days: int
    keep_min_backups: int


@dataclass(frozen=True, slots=True)
class StorageUsage:
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> int:
        return 0 if self.total == 0 else round(self.used * 100 / self.total)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    before: StorageUsage
    after: StorageUsage
    cleanup_triggered: bool
    deleted_files: int
    deleted_bytes: int
    critical: bool


def load_storage_policy(path: Path) -> StoragePolicy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("maintenance config must be a JSON object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("maintenance.enabled must be a boolean")
    policy = StoragePolicy(
        enabled=enabled,
        warning_used_percent=_integer(raw, "warning_used_percent"),
        cleanup_used_percent=_integer(raw, "cleanup_used_percent"),
        critical_used_percent=_integer(raw, "critical_used_percent"),
        minimum_free_bytes=_integer(raw, "minimum_free_bytes"),
        log_retention_days=_integer(raw, "log_retention_days"),
        artifact_retention_days=_integer(raw, "artifact_retention_days"),
        backup_retention_days=_integer(raw, "backup_retention_days"),
        keep_min_backups=_integer(raw, "keep_min_backups"),
    )
    if not (
        1
        <= policy.warning_used_percent
        < policy.cleanup_used_percent
        < policy.critical_used_percent
        <= 99
    ):
        raise ValueError("storage thresholds must satisfy warning < cleanup < critical <= 99")
    if policy.minimum_free_bytes < 0:
        raise ValueError("minimum_free_bytes must be non-negative")
    for name in ("log_retention_days", "artifact_retention_days", "backup_retention_days"):
        if getattr(policy, name) < 1:
            raise ValueError(f"{name} must be positive")
    if not 1 <= policy.keep_min_backups <= 100:
        raise ValueError("keep_min_backups must be between 1 and 100")
    return policy


def storage_usage(path: Path) -> StorageUsage:
    value = shutil.disk_usage(path)
    return StorageUsage(value.total, value.used, value.free)


def directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def maintain_storage(
    *,
    policy: StoragePolicy,
    filesystem_path: Path,
    artifacts_dir: Path,
    logs_dir: Path,
    backups_dir: Path,
    apply: bool,
    now: datetime,
    usage_reader: Callable[[Path], StorageUsage] = storage_usage,
) -> MaintenanceResult:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    before = usage_reader(filesystem_path)
    triggered = policy.enabled and (
        before.used_percent >= policy.cleanup_used_percent
        or before.free <= policy.minimum_free_bytes
    )
    candidates: list[Path] = []
    if triggered:
        candidates.extend(_old_files(logs_dir, now, policy.log_retention_days))
        candidates.extend(_old_files(artifacts_dir, now, policy.artifact_retention_days))
        candidates.extend(
            _old_backups(
                backups_dir,
                now,
                policy.backup_retention_days,
                policy.keep_min_backups,
            )
        )
    unique = sorted(set(candidates))
    deleted_files = 0
    deleted_bytes = 0
    if apply:
        for path in unique:
            if not path.is_file():
                continue
            deleted_bytes += path.stat().st_size
            path.unlink()
            deleted_files += 1
    after = usage_reader(filesystem_path) if apply else before
    critical = (
        after.used_percent >= policy.critical_used_percent
        or after.free <= policy.minimum_free_bytes
    )
    return MaintenanceResult(before, after, triggered, deleted_files, deleted_bytes, critical)


def _old_files(root: Path, now: datetime, retention_days: int) -> list[Path]:
    if not root.exists():
        return []
    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    result: list[Path] = []
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file() or not path.resolve().is_relative_to(resolved_root):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if modified < cutoff:
            result.append(path)
    return result


def _old_backups(root: Path, now: datetime, retention_days: int, keep: int) -> list[Path]:
    if not root.exists():
        return []
    archives = sorted(
        root.glob("moex-*.tar.gz"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    protected = set(archives[:keep])
    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    result: list[Path] = []
    for archive in archives:
        modified = datetime.fromtimestamp(archive.stat().st_mtime, tz=UTC)
        if archive in protected or modified >= cutoff:
            continue
        result.append(archive)
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        if checksum.is_file():
            result.append(checksum)
    return result


def _integer(raw: dict[str, object], name: str) -> int:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"maintenance.{name} must be an integer")
    return value
