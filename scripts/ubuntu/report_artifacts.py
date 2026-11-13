"""Include completed-day reports by session date, independently of file mtime."""

from __future__ import annotations

import argparse
import re
import shutil
from datetime import date
from pathlib import Path

REPORT_NAME = re.compile(
    r"(?:daily-performance|intraday-daily-performance|long-daily-performance|"
    r"prod-long-daily-control|long-close)-(\d{4}-\d{2}-\d{2})\.(?:json|txt)\Z"
)


def copy_dated_reports(source: Path, destination: Path, start: date, end: date) -> int:
    if start > end:
        raise ValueError("report start date must not be after end date")
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("report directories must not be symlinks")
    if source.resolve() == destination.resolve():
        raise ValueError("report source and destination must differ")
    if not source.exists():
        return 0
    selected = []
    for path in sorted(source.iterdir()):
        match = REPORT_NAME.fullmatch(path.name)
        if match is None or not start <= date.fromisoformat(match[1]) <= end:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("dated report must be a regular file")
        if (destination / path.name).is_symlink():
            raise ValueError("report destination must not be a symlink")
        selected.append(path)
    destination.mkdir(parents=True, exist_ok=True)
    for path in selected:
        shutil.copy2(path, destination / path.name)
    return len(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    count = copy_dated_reports(args.artifacts, args.destination, args.from_date, args.to_date)
    print(f"PASS: included {count} session-dated reports independently of mtime")


if __name__ == "__main__":
    main()
