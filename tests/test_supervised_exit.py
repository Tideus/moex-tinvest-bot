"""CLI exit preparation and confirmation use fake broker and notification ports."""

import argparse
import hashlib
from datetime import datetime
from unittest.mock import Mock

import pytest
from test_live_execution import RELEASE
from test_take_profit import case  # noqa: F401 -- shared durable fake broker fixture

from moex_bot import supervised_operator as m


@pytest.fixture
def operator(case, tmp_path, monkeypatch):  # noqa: F811 -- pytest imported fixture injection
    kw, _, now = case
    args = argparse.Namespace(action="exit-preview", settings=tmp_path / "settings.json",
                              policy=tmp_path / "policy.json", universe=tmp_path / "universe.json",
                              output=tmp_path / "exit.json", preview=None,
                              confirm_preview_sha256=None)
    settings = dict(journal=str(kw["entry_path"]), protection_journal=str(kw["journal"].path),
                    admission=str(tmp_path / "review.json"), enabled=True,
                    stage="supervised_canary")
    monkeypatch.setattr(m, "datetime", Mock(wraps=datetime))
    m.datetime.now.return_value = now
    monkeypatch.setattr(m, "NativeExitBroker", lambda *args: kw["stops"])
    monkeypatch.setattr(m, "UrlLibJsonTransport", Mock())
    monkeypatch.setattr(m, "AuditJsonTransport", Mock())
    monkeypatch.setattr(m, "notify_experiment", Mock())
    monkeypatch.setattr(m, "require_experiment_review", Mock())
    monkeypatch.setattr(m, "release_digest", lambda *args: RELEASE)
    monkeypatch.setattr(m, "load_experiment_settings", lambda path: settings)
    instruments = {kw["plan"].instrument.uid: kw["plan"].instrument}
    call = dict(args=args, settings=settings, policy=kw["policy"], release=RELEASE,
                account="real-iis", token="fake", broker=kw["broker"], instruments=instruments)
    return call, kw


def confirmed(operator):
    call, kw = operator
    assert m.operate_exit(**call) == 0
    args = call["args"]
    args.action = "exit-execute"
    args.preview = args.output
    args.confirm_preview_sha256 = hashlib.sha256(args.preview.read_bytes()).hexdigest()
    return call, kw


def test_exit_preview_and_execution_are_paired(operator):
    call, kw = confirmed(operator)
    assert not kw["broker"].calls
    assert m.operate_exit(**call) == 0
    assert len(kw["broker"].calls) == 1
    m.notify_experiment.assert_called_once()
    assert m.require_experiment_review.call_count >= 3


@pytest.mark.parametrize("fault", ["universe", "output_none", "output_collision", "digest",
                                   "preview_none", "disabled", "stage", "release",
                                   "changed_file", "changed_settings"])
def test_exit_operator_rejects_changed_authority(operator, monkeypatch, fault):
    call, kw = operator
    if fault in {"universe", "output_none", "output_collision"}:
        if fault == "universe":
            call["instruments"].clear()
        elif fault == "output_none":
            call["args"].output = None
        else:
            call["args"].output = call["args"].settings
    else:
        confirmed(operator)
        if fault == "digest":
            call["args"].confirm_preview_sha256 = "bad"
        elif fault == "preview_none":
            call["args"].preview = None
        elif fault in {"disabled", "stage"}:
            call["settings"]["enabled" if fault == "disabled" else "stage"] = (
                False if fault == "disabled" else "preparation_only"
            )
        elif fault == "release":
            monkeypatch.setattr(m, "release_digest", lambda *args: "b" * 64)
        elif fault == "changed_settings":
            monkeypatch.setattr(m, "load_experiment_settings", lambda path: {})
        else:
            def run(**kwargs):
                call["args"].preview.write_text("{}")
                kwargs["gate"]()

            monkeypatch.setattr(m, "run_exit", run)
    with pytest.raises(ValueError):
        m.operate_exit(**call)
    assert not kw["broker"].calls
    kw["stops"].cancel.assert_not_called()


def test_exit_routes_through_operator_setup_and_lock(operator, monkeypatch):
    call, _ = operator
    args = call["args"]
    # Exercise the common dispatch without invoking any real account endpoint.
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "fake")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "real-iis")
    monkeypatch.setattr(m, "experiment_policy", lambda *args: call["policy"])
    monkeypatch.setattr(m, "load_universe", lambda path: [])
    monkeypatch.setattr(m, "SupervisedTInvestBroker", Mock())
    dispatch = Mock(return_value=0)
    monkeypatch.setattr(m, "operate_exit", dispatch)
    assert m.operate(args) == 0
    dispatch.assert_called_once()
