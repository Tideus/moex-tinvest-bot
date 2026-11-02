from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_long_auto_signal import AT, CONFIG, decision  # noqa: F401 - fixture dependency
from test_supervised_operator import setup  # noqa: F401 - fixture dependency

from moex_bot import long_auto_operator as op
from moex_bot import supervised_operator
from moex_bot.live_execution import LiveHolding
from moex_bot.long_auto_signal import Candidate


def test_enrollment_marker_checked_before_any_action(tmp_path, monkeypatch):
    state, anchor, marker = (tmp_path / n for n in ("state", "claim", "marker"))
    for key, path in (("STATE", state), ("ANCHOR", anchor), ("MARKER", marker)):
        monkeypatch.setattr(op, key, path)
    marker.write_bytes(b"sandbox-long-once/v1\n")
    native = Path.stat

    def fake_stat(self, **kwargs):
        data = native(self, **kwargs)
        if self == marker:
            import os
            import stat

            fields = list(data)
            fields[0], fields[3], fields[4] = stat.S_IFREG | 0o644, 1, 0
            return os.stat_result(fields)
        return data

    monkeypatch.setattr(Path, "stat", fake_stat)
    op._scope(SimpleNamespace(state=state))
    with pytest.raises(ValueError):
        op._scope(SimpleNamespace(state=tmp_path / "alternative"))
    marker.write_bytes(b"wrong\n")
    with pytest.raises(ValueError):
        op._scope(SimpleNamespace(state=state))
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "")
    with pytest.raises(ValueError):
        op._working_account()


def test_cli_entrypoint_only_status_without_enrollment(monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["long_auto_operator", "status", "--state", "not-enrolled"])
    monkeypatch.delitem(sys.modules, "moex_bot.long_auto_operator", raising=False)
    with pytest.raises(SystemExit) as error:
        runpy.run_module("moex_bot.long_auto_operator", run_name="__main__")
    assert error.value.code == 2


@pytest.fixture
def layout(decision, tmp_path, monkeypatch):  # noqa: F811
    from contextlib import nullcontext

    base = tmp_path / "once"
    run = base / "run-20261102T070500Z.abcd1234"
    run.mkdir(parents=True)
    monkeypatch.setattr(op, "BASE", base)
    monkeypatch.setattr(op, "STATE", base / "sandbox.sqlite3")
    monkeypatch.setattr(op, "ANCHOR", tmp_path / "control" / "entry.claim")
    monkeypatch.setattr(op, "CONFIG", CONFIG)
    monkeypatch.setattr(op, "RUNTIME", CONFIG / "runtime.json")
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "test-long")
    monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "test-intraday")
    monkeypatch.delenv("T_INVEST_SANDBOX_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "offline-token")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "not-sandbox")
    monkeypatch.setattr(op, "datetime", Mock(now=lambda tz: AT))
    monkeypatch.setattr(op, "_scope", lambda args: None)
    monkeypatch.setattr(op, "writer_lock", nullcontext)
    for key in ("portfolio", "plan"):
        decision[key].rename(run / (key + ".json"))
        decision[key] = run / (key + ".json")
    return [
        item
        for flag, value in (
            ("plan", decision["plan"]),
            ("portfolio", decision["portfolio"]),
            ("strategy-config", decision["strategy_config"]),
            ("universe", decision["universe"]),
            ("policy", decision["policy"]),
        )
        for item in ("--" + flag, str(value))
    ]


def test_canonical_real_plan_cli_no_implicit_init_and_no_prod(layout, monkeypatch):
    assert op.main(["sandbox-cycle", *layout]) == 2
    assert op.main(["init-sandbox"]) == 0
    assert op.main(["init-sandbox"]) == 2
    assert op.main(["status", "--waiting-only"]) == 0
    runner = Mock(return_value="QUARANTINED")
    fake = Mock()
    monkeypatch.setattr(op, "run_sandbox_candidate", runner)
    monkeypatch.setattr(op, "SandboxLongBroker", fake)
    # Real signal/CLI, test-only broker. Enable the production-identical sandbox gate explicitly.
    real_runtime = op.load_runtime_config(op.RUNTIME)
    monkeypatch.setattr(
        op, "load_runtime_config", lambda _: replace(real_runtime, sandbox_orders_enabled=False)
    )
    assert op.main(["sandbox-cycle", *layout]) == 2
    fake.assert_not_called()
    monkeypatch.setattr(
        op, "load_runtime_config", lambda _: replace(real_runtime, sandbox_orders_enabled=True)
    )
    assert op.main(["sandbox-cycle", *layout]) == 2  # spy did not persist protection
    c = runner.call_args.args[0]
    assert c.lots == 1 and c.account_id == "test-long" and c.instrument.secid == "SBER"
    assert fake.call_args.args == ("offline-token", "test-long")
    fake.call_args.kwargs["entry_gate"]()
    monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "test-long")
    with pytest.raises(ValueError):
        fake.call_args.kwargs["entry_gate"]()


def test_cli_refuses_untrusted_sources_stale_or_crossed_scope(layout, monkeypatch):
    assert op.main(["init-sandbox"]) == 0
    fake = Mock(side_effect=AssertionError)
    monkeypatch.setattr(op, "SandboxLongBroker", fake)
    bad = list(layout)
    bad[1] = str(Path(bad[1]).with_name("other.json"))
    assert op.main(["sandbox-cycle", *bad]) == 2
    monkeypatch.setattr(op, "datetime", Mock(now=lambda tz: AT + timedelta(hours=1)))
    assert op.main(["sandbox-cycle", *layout]) == 2
    monkeypatch.setattr(op, "select_candidate", lambda **_: None)
    assert op.main(["sandbox-cycle", *layout]) == 0
    fake.assert_not_called()


def test_separate_status_reconcile_recovery_and_prod_preview(layout, monkeypatch, tmp_path):
    from moex_bot import long_auto_once as once

    state = Mock()
    monkeypatch.setattr(op, "OnceLedger", lambda *a, **kw: state)
    for current, rc in (("FILLED_PROTECTED", 3), ("ENTRY_NOT_FILLED", 3), ("QUARANTINED", 2)):
        state.status.return_value = current
        assert op.main(["status", "--waiting-only"]) == rc
    monkeypatch.setattr(op, "SandboxLongBroker", Mock())
    monkeypatch.setattr(
        once, "reconcile_sandbox_once", lambda *a, **k: dict(broker_status="filled", filled_lots=1)
    )
    assert op.main(["reconcile"]) == 0
    monkeypatch.setattr(once, "load_saved_candidate", Mock())
    recover = Mock(return_value="FILLED_PROTECTED")
    monkeypatch.setattr(once, "recover_sandbox_protection", recover)
    assert op.main(["recover-stop"]) == 0
    recover.return_value = "QUARANTINED"
    assert op.main(["recover-stop"]) == 2
    preview = [
        "prod-preview",
        *layout,
        "--output",
        str(tmp_path / "out.json"),
        "--settings",
        str(CONFIG / "prod_experiment.json"),
    ]
    assert op.main(preview) == 2  # sandbox strategy cannot become an IIS signal
    preview[preview.index("--strategy-config") + 1] = str(CONFIG / "prod_shadow.json")
    monkeypatch.setattr(
        op,
        "select_candidate",
        lambda **kw: SimpleNamespace(
            instrument=SimpleNamespace(secid="SBER"), limit_price=100, stop_trigger=98
        ),
    )
    paired = Mock(return_value=0)
    monkeypatch.setattr(supervised_operator, "operate", paired)
    assert op.main(preview) == 0
    assert paired.call_args.args[0].action == "preview"


@pytest.mark.parametrize("case", ["ok", "manual", "fee", "expired"])
def test_iis_signal_preview_preserves_fresh_limit_and_protection(setup, case):  # noqa: F811
    args, _, broker = setup
    raw_capture = broker.capture.side_effect
    # Obtain canonical broker instrument without writing a preview or calling a broker.
    from moex_bot.domain import Instrument
    from moex_bot.shadow import load_universe

    u = next(u for u in load_universe(args.universe) if u.secid == "SBER")
    i = Instrument(
        u.secid,
        u.t_invest_uid,
        u.board,
        u.lot_size_verified,
        Decimal(".01"),
        issuer_id=u.issuer_id,
        sector=u.sector,
        risk_cluster=u.risk_cluster,
        short_enabled=u.short_enabled_verified,
    )
    expiry = datetime.now(UTC) + timedelta(seconds=-1 if case == "expired" else 60)
    signal = Candidate(
        "real-iis", "a" * 64, i, Decimal(100), Decimal(98), str(uuid4()), str(uuid4()), expiry
    )
    args.signal = signal
    args.stop_trigger = "98"
    broker.capture.side_effect = lambda o: replace(
        raw_capture(o),
        estimated_total=Decimal("10000.01") if case == "fee" else Decimal("100.10"),
        positions={i.uid: LiveHolding(i, 1, 1, Decimal(100))} if case == "manual" else {},
    )
    if case == "ok":
        import json

        assert supervised_operator.operate(args) == 0
        p = json.loads(args.output.read_bytes())
        assert not p["order_submitted"] and p["long_decision_sha256"] == signal.decision_sha256
        assert p["intent"]["order_request_id"] == signal.order_request_id
        assert p["expires_at"] == expiry.isoformat() and p["protection"]["stop_trigger"] == "98"
    else:
        with pytest.raises(ValueError):
            supervised_operator.operate(args)
        assert not args.output.exists()
    broker.submit.assert_not_called()
