import json
from datetime import datetime, timedelta
from decimal import Decimal

import pr6_report
import pytest
from pr6_observe import MSK, READS, NoRedirect, SandboxReads
from pr6_report import move, report


@pytest.mark.parametrize(
    "url,account",
    [
        (next(iter(READS)).replace("sandbox-invest-", "invest-"), "A"),
        (next(iter(READS)).rsplit("/", 1)[0] + "/CancelSandboxOrder", "A"),
        (next(iter(READS)).rsplit("/", 1)[0] + "/PostSandboxOrder", "A"),
        (next(iter(READS)), "B"),
    ],
)
def test_shadow_transport_rejects_other_account_write_and_prod(url, account):
    t = SandboxReads("A")
    with pytest.raises(PermissionError):
        t.post(url, headers={}, payload={"accountId": account}, timeout_seconds=1)
    assert not t.methods
    assert NoRedirect().redirect_request(None) is None


def test_mark_requires_two_traded_minutes_strictly_after_decision():
    at = datetime(2026, 11, 2, 10, 5, 15, tzinfo=MSK)
    start = at.replace(minute=6, second=0)
    end = start + timedelta(hours=1)
    prices = {
        at: (Decimal(1), Decimal(1)),
        start: (Decimal(100), Decimal(99)),
        end: (Decimal(102), Decimal(101)),
    }
    assert move(prices, at, 60, end)["status"] == "ожидается"
    assert move(prices, at, 60, end + timedelta(minutes=1))["pct"] == 2
    assert "pct" not in move({}, at, 60, end + timedelta(minutes=1))


def test_separate_empty_reports_do_not_invent_returns(tmp_path):
    now = datetime(2026, 11, 2, 8, 42, tzinfo=MSK)
    for name in ("long", "intraday"):
        report(tmp_path, name, now)
        body = (tmp_path / f"{name}-report.md").read_text(encoding="utf-8")
        assert f"# PR6 · {name}" in body and "Нет плана" in body
        assert "не P&L" in body


@pytest.mark.parametrize("profile", ["long", "intraday"])
def test_one_completed_plan_links_to_only_its_own_forward_returns(tmp_path, monkeypatch, profile):
    t = datetime(2026, 11, 2, 10, 5, 20, tzinfo=MSK)
    part = tmp_path / "2026-11-02" / profile
    part.mkdir(parents=True)
    (part / "cycle-1.json").write_text(
        json.dumps({"plan": "plan.json", "code": 0, "finished_at": t.isoformat()})
    )
    it = {
        "instrument": {"secid": "SBER"},
        "side": "buy",
        "lots": 1,
        "limit_price": "100",
        "order_request_id": "test",
    }
    (part / "plan.json").write_text(
        json.dumps(
            {
                "run_id": "case",
                "quality": {"passed": True},
                "orders": [{"intent": it}],
                "targets": [{"secid": "SBER"}],
                "rejected": [],
                "geo": {"level": "normal"},
                "rebalance_allowed": True,
                "analysis_input": {"decision_ready_at": t.isoformat()},
            }
        )
    )
    monkeypatch.setattr(
        pr6_report,
        "candles",
        lambda *a: {
            t.replace(minute=6, second=0): (Decimal(100), Decimal(100)),
            t.replace(hour=11, minute=6, second=0): (Decimal(101), Decimal(101)),
        },
    )
    monkeypatch.setattr(
        pr6_report,
        "analyze",
        lambda *a, **kw: {
            "counts": {"planned": 1},
            "rejection_reasons": {},
            "summary": {},
            "observations": [
                {
                    "run_id": "case",
                    "order_request_id": "test",
                    "horizons": {"15": {"net_bps": Decimal(8)}},
                }
            ],
        },
    )
    report(tmp_path, profile, t + timedelta(hours=2))
    body = (tmp_path / f"{profile}-report.md").read_text(encoding="utf-8")
    assert "SBER" in body and ("+8.0" if profile == "intraday" else "+1.00%") in body
