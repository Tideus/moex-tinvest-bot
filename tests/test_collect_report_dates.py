import importlib.util
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ubuntu/report_artifacts.py"
spec = importlib.util.spec_from_file_location("report_artifacts", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
DAY = date(2026, 11, 12)


@pytest.mark.parametrize("prefix", [
    "daily-performance", "intraday-daily-performance", "long-daily-performance",
    "prod-long-daily-control", "long-close",
])
@pytest.mark.parametrize("suffix", ["json", "txt"])
def test_report_rebuilt_next_day_is_included_without_changing_source(tmp_path, prefix, suffix):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    path = source / f"{prefix}-{DAY}.{suffix}"
    path.write_text("immutable fixture", encoding="utf-8")
    changed = datetime(2026, 11, 13, 0, 19, tzinfo=UTC).timestamp()
    os.utime(path, (changed, changed))
    before = path.stat().st_mtime_ns
    assert module.copy_dated_reports(source, dest, DAY, DAY) == 1
    assert (dest / path.name).read_bytes() == path.read_bytes()
    assert path.stat().st_mtime_ns == before
    assert module.copy_dated_reports(source, dest, DAY, DAY) == 1


def test_date_boundaries_and_unrelated_files_are_not_copied(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    names = [f"long-daily-performance-2026-11-{d}.json" for d in (11, 12, 13, 14)]
    names += ["bot.env", "intraday-plan-20261112T155501Z.json", "misc-2026-11-12.json"]
    for name in names:
        (source / name).write_text("fixture")
    assert module.copy_dated_reports(source, dest, DAY, date(2026, 11, 13)) == 2
    assert sorted(p.name for p in dest.iterdir()) == names[1:3]
    with pytest.raises(ValueError, match="start date"):
        module.copy_dated_reports(source, dest, date(2026, 11, 13), DAY)
    with pytest.raises(ValueError, match="must differ"):
        module.copy_dated_reports(source, source, DAY, DAY)
    assert module.copy_dated_reports(tmp_path / "missing", dest, DAY, DAY) == 0


def test_symlink_is_not_followed(tmp_path, monkeypatch):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    path = source / f"long-close-{DAY}.json"
    path.write_text("fixture")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path or original(self))
    with pytest.raises(ValueError, match="regular file"):
        module.copy_dated_reports(source, dest, DAY, DAY)
    assert not dest.exists()


def test_cli_and_collect_report_integration(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    (source / f"long-close-{DAY}.json").write_text("fixture")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--artifacts", str(source), "--destination", str(dest),
         "--from-date", str(DAY), "--to-date", str(DAY)], capture_output=True, text=True,
    )
    assert result.returncode == 0 and "included 1" in result.stdout
    body = (ROOT / "scripts/ubuntu/moex-botctl.sh").read_text(encoding="utf-8")
    report = body.split("collect_report() (", 1)[1]
    assert '"${APP_DIR}/scripts/ubuntu/report_artifacts.py"' in report
    assert '--from-date "${from_date}" --to-date "${to_date}"' in report
    assert report.index("report_artifacts.py") < report.index('REPORT_ROOT="${work_dir}"')
