"""Secret config files are private from first creation and atomically replaced."""

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.fixture(params=["kuma-exporter.py", "install-kuma-adapters.py"])
def writer(request):
    path = Path(__file__).resolve().parents[1] / "scripts/ubuntu" / request.param
    spec = importlib.util.spec_from_file_location("private_writer_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "grp": SimpleNamespace(),
            "dotenv": SimpleNamespace(dotenv_values=Mock()),
        },
    ):
        spec.loader.exec_module(module)
    return module


def trusted_parents(monkeypatch, *, insecure=False):
    original = Path.lstat

    def lstat(path):
        result = original(path)
        if stat.S_ISDIR(result.st_mode):
            fields = list(result)
            fields[0] = stat.S_IFDIR | (0o777 if insecure else 0o755)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)


def test_private_atomic_write(writer, tmp_path, monkeypatch):
    trusted_parents(monkeypatch)
    target = tmp_path / "secret"
    target.write_text("old")
    native_open = os.open
    opened = []

    def opening(path, flags, mode=0o777):
        opened.append((path, flags, mode))
        if Path(path) == target.parent:
            return 123456
        return native_open(path, flags, mode)

    with (
        patch.object(writer.os, "open", opening),
        patch.object(writer.os, "fchown", Mock(), create=True) as owner,
        patch.object(writer.os, "fchmod", Mock(), create=True) as permissions,
        patch.object(writer.os, "fsync", Mock()),
        patch.object(writer.os, "close", Mock()),
    ):
        writer.private_config_write(target, "new-secret", 123)
    assert target.read_text() == "new-secret"
    assert opened[0][1] & os.O_EXCL and opened[0][2] == 0o600
    owner.assert_called_once()
    assert permissions.call_args.args[1] == 0o640
    assert not list(tmp_path.glob(".*.tmp"))


def test_insecure_parent_blocks_before_secret_creation(writer, tmp_path, monkeypatch):
    trusted_parents(monkeypatch, insecure=True)
    with pytest.raises(ValueError, match="root-owned"):
        writer.private_config_write(tmp_path / "secret", "never written", 123)
    assert not list(tmp_path.iterdir())


def test_write_failure_preserves_previous_secret_and_removes_temporary(
    writer, tmp_path, monkeypatch
):
    trusted_parents(monkeypatch)
    target = tmp_path / "secret"
    target.write_text("old")
    with (
        patch.object(writer.os, "fsync", side_effect=OSError("disk failure")),
        pytest.raises(OSError),
    ):
        writer.private_config_write(target, "new", 123)
    assert target.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secret"]


def test_nonregular_target_rejected(writer, tmp_path, monkeypatch):
    trusted_parents(monkeypatch)
    target = tmp_path / "directory"
    target.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        writer.private_config_write(target, "secret", 123)
