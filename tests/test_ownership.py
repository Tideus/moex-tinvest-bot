import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from moex_bot.ownership import load_ownership_disclosures, render_ownership_report


def test_ownership_requires_dated_direct_source(tmp_path: Path) -> None:
    path = tmp_path / "owners.json"
    path.write_text(
        json.dumps(
            [
                {
                    "secid": "SBER",
                    "holder_name": "Example Fund",
                    "holder_type": "fund",
                    "stake_percent": "5.1",
                    "report_date": "2026-06-30",
                    "published_at": "2026-07-20T12:00:00+03:00",
                    "source_url": "https://example.org/disclosure.pdf",
                    "source_kind": "fund_report",
                    "verified": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    items = load_ownership_disclosures(path, as_of=datetime(2026, 8, 14, tzinfo=UTC), secid="SBER")
    assert "5.1%" in render_ownership_report(items, secid="SBER")


def test_ownership_rejects_future_publication(tmp_path: Path) -> None:
    path = tmp_path / "owners.json"
    payload = [{
        "secid": "SBER", "holder_name": "x", "holder_type": "fund",
        "stake_percent": None, "report_date": "2026-06-30",
        "published_at": "2027-01-01T00:00:00+03:00",
        "source_url": "https://example.org/x", "source_kind": "fund_report",
        "verified": True,
    }]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="timestamp"):
        load_ownership_disclosures(path, as_of=datetime(2026, 8, 14, tzinfo=UTC))
