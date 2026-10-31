"""Installer validates checkout, explicit units and legacy bundles before writes."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).resolve().parents[1] / "scripts/ubuntu/install-kuma-adapters.py"
    spec = importlib.util.spec_from_file_location("installer_paths_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules, {"grp": SimpleNamespace(), "dotenv": SimpleNamespace(dotenv_values=Mock())}
    ):
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("layout", ["checkout", "explicit", "legacy"])
def test_all_supported_input_layouts(installer, tmp_path, layout):
    source = tmp_path / "project/scripts/ubuntu"
    units = (
        tmp_path / "custom"
        if layout == "explicit"
        else tmp_path / "project/deploy/ubuntu"
        if layout == "checkout"
        else source
    )
    for directory, names in ((source, installer.SCRIPT_FILES), (units, installer.UNIT_FILES)):
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_text("test input")
    assert installer.installation_sources(source, units if layout == "explicit" else None) == (
        source.resolve(),
        units.resolve(),
    )


@pytest.mark.parametrize(
    "missing",
    [
        "kuma-exporter.py",
        "kuma-telegram-relay.py",
        "moex-observer.service",
        "moex-observer.timer",
        "moex-kuma-relay.service",
    ],
)
def test_cli_missing_input_does_not_mutate_config_or_systemd(installer, tmp_path, missing):
    source = tmp_path / "source"
    source.mkdir()
    for name in (*installer.SCRIPT_FILES, *installer.UNIT_FILES):
        if name != missing:
            (source / name).write_text("test")
    native = installer.os
    platform = SimpleNamespace(**{name: getattr(native, name) for name in dir(native)})
    platform.geteuid = lambda: 0
    platform.chown = Mock()
    with (
        patch.object(installer, "os", platform),
        patch.object(installer, "private_config_write") as write,
        patch.object(installer.shutil, "copyfile") as copy,
        patch.object(installer.subprocess, "run") as run,
        patch.object(sys, "argv", ["installer", "--source", str(source)]),
        pytest.raises(ValueError, match=missing),
    ):
        installer.main()
    write.assert_not_called()
    copy.assert_not_called()
    run.assert_not_called()
    platform.chown.assert_not_called()
