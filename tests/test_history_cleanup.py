import hashlib
import importlib.util
import io
import os
import tarfile
from pathlib import Path

import pytest


def test_cleanup_preserves_and_validates_newest_backups(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/ubuntu/cleanup-service-history.py"
    spec = importlib.util.spec_from_file_location("history_cleanup", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = []
    for index in range(3):
        path = tmp_path / f"moex-tinvest-{index}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo("example.txt")
            member.size = 4
            archive.addfile(member, io.BytesIO(b"test"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n")
        os.utime(path, (index + 1, index + 1))
        paths.append(path)
    protected, retired = module.verified_archives(tmp_path, "moex-tinvest-", 2)
    assert protected == paths[:0:-1]
    assert retired == paths[:1]
    assert all(path.exists() for path in paths)
    Path(str(paths[-1]) + ".sha256").write_text("invalid")
    with pytest.raises(ValueError, match="checksum"):
        module.verified_archives(tmp_path, "moex-tinvest-", 2)
    assert all(path.exists() for path in paths)
