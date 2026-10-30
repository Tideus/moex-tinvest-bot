import importlib.util
import sys
from pathlib import Path

import pytest


def load_migration():
    script = Path(__file__).resolve().parents[1] / "scripts/ubuntu/prepare-breakwood-nginx.py"
    spec = importlib.util.spec_from_file_location("nginx_migration", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nginx_migration_dry_run_does_not_delete(tmp_path, monkeypatch):
    module = load_migration()
    site = tmp_path / "legacy"
    site.write_text(module.NEEDLE)
    static = tmp_path / "mdl-static"
    static.mkdir()
    payload = static / "keep.bin"
    payload.write_bytes(b"data")
    monkeypatch.setattr(module, "SITE", site)
    monkeypatch.setattr(module, "STATIC", static)
    for key in ("AVAILABLE", "PENDING", "ENABLED"):
        monkeypatch.setattr(module, key, tmp_path / key)
    for name in ("breakwood-services-http.conf", "breakwood-services-tls.conf"):
        (tmp_path / name).write_text("server {}")
    monkeypatch.setattr(sys, "argv", ["migration", "--templates", str(tmp_path)])
    module.main()
    assert payload.read_bytes() == b"data"
    assert site.read_text() == module.NEEDLE


def test_nginx_migration_rejects_unexpected_config(tmp_path, monkeypatch):
    module = load_migration()
    site = tmp_path / "legacy"
    site.write_text("another config")
    monkeypatch.setattr(module, "SITE", site)
    monkeypatch.setattr(sys, "argv", ["migration", "--templates", str(tmp_path), "--apply"])
    with pytest.raises(ValueError, match="exactly one"):
        module.main()
