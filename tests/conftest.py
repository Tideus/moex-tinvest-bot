import pytest

from moex_bot import monitoring_long_once


@pytest.fixture
def legacy_monitor_layout(tmp_path, monkeypatch):
    """A legacy installation; never discover ownership evidence on the developer's host."""
    monkeypatch.setattr(monitoring_long_once, "MARKER", tmp_path / "etc/owner")
    monkeypatch.setattr(monitoring_long_once, "ANCHOR", tmp_path / "control/claim")
