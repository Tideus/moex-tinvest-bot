import copy
import hashlib
import io
import json
import tarfile
from datetime import date

import pytest
from accounting_supplement import archive_supplement, bridge

from moex_bot.trader_journal import OPERATIONS_SNAPSHOT, digest


@pytest.fixture
def accounting():
    first = dict(account_binding=hashlib.sha256(b"test-account").hexdigest(),
                 source="t_invest_sandbox", observed_at="2026-11-16T20:55:00Z",
                 reported_equity="300000", report_date="2026-11-16")
    last = dict(first, observed_at="2026-11-17T20:55:00Z", reported_equity="300095",
                report_date="2026-11-17")
    receipt = dict(account_id="test-account", source="t_invest_sandbox",
                   journal_coverage_from="2026-11-16T00:00:00Z",
                   journal_coverage_to="2026-11-18T00:00:00Z",
                   observed_at="2026-11-18T00:01:00Z",
                   executed_operations=[dict(operation_id="deposit",
                                             occurred_at="2026-11-17T10:00:00Z",
                                             external_cashflow="100")],
                   journal_operations_snapshot=dict(protocol=OPERATIONS_SNAPSHOT, pages=1,
                                                    count=1, ids_sha256=digest(["deposit"])))
    return first, last, receipt


def test_equity_bridge_external_flow_and_no_double_fee(accounting):
    frozen = copy.deepcopy(accounting)
    result = bridge(*accounting)
    assert result["pnl"] == "-5"
    assert result["external_cashflow"] == "100"
    assert result["is_full_session_return"] is False
    assert result["execution_allowed"] is False
    assert result["historical_reports_unchanged"] is True
    assert "test-account" not in json.dumps(result)
    assert accounting == frozen


@pytest.mark.parametrize("change", ["missing", "account", "source", "coverage", "proof", "ids",
                                   "duplicate", "nan", "flow", "naive", "reversed", "outside"])
def test_missing_or_bad_evidence_is_null_not_zero(accounting, change):
    first, last, receipt = accounting
    if change == "missing":
        first = None
    elif change == "account":
        receipt["account_id"] = "other"
    elif change == "source":
        last["source"] = "t_invest_prod"
    elif change == "coverage":
        receipt["journal_coverage_to"] = "2026-11-17T10:00:00Z"
    elif change == "proof":
        receipt.pop("journal_operations_snapshot")
    elif change == "ids":
        receipt["journal_operations_snapshot"]["ids_sha256"] = "incorrect"
    elif change == "duplicate":
        receipt["executed_operations"] *= 2
    elif change == "nan":
        last["reported_equity"] = "NaN"
    elif change == "flow":
        receipt["executed_operations"][0].pop("external_cashflow")
    elif change == "naive":
        last["observed_at"] = "2026-11-17T20:55:00"
    elif change == "reversed":
        last["observed_at"] = first["observed_at"]
    else:
        receipt["executed_operations"][0]["occurred_at"] = "2026-11-19T00:00:00Z"
    result = bridge(first, last, receipt)
    assert result["status"] == "UNAVAILABLE" and result["pnl"] is None


@pytest.mark.parametrize("at,pnl", [("2026-11-16T20:55:00Z", "95"),
                                   ("2026-11-17T20:55:00Z", "-5"),
                                   ("2026-11-17T20:55:01Z", "95")])
def test_exact_operation_boundaries_and_future_no_effect(accounting, at, pnl):
    accounting[2]["executed_operations"][0]["occurred_at"] = at
    assert bridge(*accounting)["pnl"] == pnl


def archive_file(tmp_path, accounting, *, duplicate=False):
    path = tmp_path / "private.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for value in [*accounting[:2], *([accounting[1]] if duplicate else [])]:
            raw = json.dumps(value).encode()
            member = tarfile.TarInfo(
                "./state/artifacts/long-close-" + value["report_date"] + ".json")
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return path


def test_archive_retains_missing_original_and_missing_later_baseline(tmp_path, accounting):
    path = archive_file(tmp_path, accounting)
    before = path.read_bytes()
    result = archive_supplement(path, accounting[2], date(2026, 11, 17), date(2026, 11, 18))
    assert result["rows"][0]["pnl"] == "-5"
    assert result["rows"][0]["original_pnl"] is None
    assert result["rows"][1]["pnl"] is None
    assert path.read_bytes() == before


def test_duplicate_members_rejected(tmp_path, accounting):
    with pytest.raises(ValueError, match="duplicate_archive"):
        archive_supplement(archive_file(tmp_path, accounting, duplicate=True), accounting[2],
                           date(2026, 11, 17), date(2026, 11, 17))


def test_wrong_close_timestamp_rejected(tmp_path, accounting):
    accounting[1]["observed_at"] = "2026-11-17T10:00:00Z"
    with pytest.raises(ValueError, match="wrong_close_date"):
        archive_supplement(archive_file(tmp_path, accounting), accounting[2],
                           date(2026, 11, 17), date(2026, 11, 17))
