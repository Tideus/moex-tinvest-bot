"""Separate root admission, user's file confirmation and exact durable stop-only recovery."""

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_protection_recovery import setup_case
from test_supervised_operator import ROOT

from moex_bot import supervised_operator as op
from moex_bot.live_execution import policy_digest
from moex_bot.prod_long import load_prod_long_policy


@pytest.mark.parametrize(
    "fault",
    [
        "legacy",
        "MOEX",
        "other_stop",
        "new_parent",
        "raw_preview",
        "wrong_action",
        "ledger",
        "bool_lots",
        "no_binding",
        "extra_field",
        "none",
    ],
)
def test_one_review_one_exact_pair_only(tmp_path, monkeypatch, fault):
    case, now = setup_case(tmp_path)
    plan = case["plan"]
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    expected = op.ReviewBinding.for_plan("BUY_WITH_STOP", plan, "ledger", "a" * 64)
    actual = expected
    review = dict(
        version=2,
        decision="APPROVED_SUPERVISED_EXPERIMENT",
        scope="ONE_LOT_TECHNICAL_TEST",
        strategy_profitability="NOT_ESTABLISHED",
        account_id=plan.account_id,
        release_sha256="b" * 64,
        policy_sha256=policy_digest(policy),
        reviewed_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        binding=asdict(expected),
        checks=dict.fromkeys(
            (
                "experimental_risk_acknowledged",
                "broker_readonly_preflight_passed",
                "protection_contract_tests_passed",
                "restore_drill_passed",
                "independent_code_review_passed",
                "supervised_session_active",
            ),
            True,
        ),
    )
    if fault == "legacy":
        review["version"] = 1
    elif fault == "MOEX":
        actual = op.ReviewBinding.for_plan(
            "BUY_WITH_STOP",
            replace(plan, instrument=replace(plan.instrument, secid="MOEX", uid=str(uuid4()))),
            "ledger",
            "a" * 64,
        )
    elif fault == "other_stop":
        actual = op.ReviewBinding.for_plan(
            "BUY_WITH_STOP",
            replace(plan, stop_trigger=plan.stop_trigger - plan.instrument.tick_size),
            "ledger",
            "a" * 64,
        )
    elif fault == "new_parent":
        actual = replace(expected, entry_request_id=str(uuid4()))
    elif fault == "raw_preview":
        actual = replace(expected, preview_sha256="e" * 64)
    elif fault == "wrong_action":
        actual = replace(expected, action="PROTECT_ONLY")
    elif fault == "ledger":
        actual = replace(expected, journal_uuid="different enrollment")
    elif fault == "bool_lots":
        review["binding"]["lots"] = True
    elif fault == "no_binding":
        del review["binding"]
    elif fault == "extra_field":
        review["binding"]["*"] = True
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review))
    monkeypatch.setattr(op, "assert_root_review_path", lambda p: None)
    if fault == "none":
        op.require_experiment_review(path, plan.account_id, policy, "b" * 64, now, binding=actual)
    else:
        with pytest.raises(ValueError, match="scope"):
            op.require_experiment_review(
                path, plan.account_id, policy, "b" * 64, now, binding=actual
            )


def test_invalid_binding(tmp_path):
    case, _ = setup_case(tmp_path)
    with pytest.raises(ValueError, match="invalid experiment"):
        op.ReviewBinding.for_plan("anything", case["plan"], "ledger", "a" * 64)


@pytest.mark.parametrize(
    "fault", ["none", "universe", "output", "bytes", "stage", "changed_file", "status_unknown"]
)
def test_recovery_cli_never_buys_or_cancels(tmp_path, monkeypatch, fault):
    case, now = setup_case(tmp_path)
    plan = case["plan"]
    settings = dict(
        journal=str(case["entry_path"]),
        protection_journal=str(case["protection_path"]),
        admission=str(tmp_path / "review.json"),
        enabled=True,
        stage="supervised_canary",
    )
    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    args = argparse.Namespace(
        action="protection-preview",
        settings=tmp_path / "settings.json",
        policy=tmp_path / "policy.json",
        universe=tmp_path / "universe.json",
        preview=None,
        output=tmp_path / "new.json",
        confirm_preview_sha256=None,
    )
    delegate = Mock()
    monkeypatch.setattr(op, "datetime", Mock(wraps=datetime))
    op.datetime.now.return_value = now
    monkeypatch.setattr(op, "NativeRecoveryParentBroker", lambda x: case["parent_broker"])
    monkeypatch.setattr(op, "NativeProtectionBroker", lambda *a: case["stop_broker"])
    monkeypatch.setattr(op, "load_experiment_settings", lambda f: settings)
    monkeypatch.setattr(op, "release_digest", lambda *a: case["release"])
    gate = Mock()
    monkeypatch.setattr(op, "require_experiment_review", gate)

    def run():
        return op.operate_recovery(
            args,
            settings,
            policy,
            case["release"],
            plan.account_id,
            "fake",
            delegate,
            {} if fault == "universe" else {plan.instrument.uid: plan.instrument},
        )

    if fault in {"output", "universe"}:
        args.output = None
        with pytest.raises(ValueError):
            run()
        return
    assert run() == 0 and case["stop_broker"].posts == 0
    args.action = "protection-apply"
    args.preview = args.output
    args.confirm_preview_sha256 = hashlib.sha256(args.output.read_bytes()).hexdigest()
    if fault == "bytes":
        args.confirm_preview_sha256 = "e" * 64
    if fault == "stage":
        settings["stage"] = "preparation_only"
    if fault == "changed_file":
        gate.side_effect = lambda *a, **kw: args.output.write_text("{}")
    if fault in {"bytes", "stage", "changed_file"}:
        with pytest.raises(ValueError):
            run()
        assert case["stop_broker"].posts == 0
    else:
        if fault == "status_unknown":
            monkeypatch.setattr(op, "apply_recovery", lambda **kw: "UNKNOWN")
        assert run() == (0 if fault == "none" else 2)
        assert gate.call_args.kwargs["binding"].action == "PROTECT_ONLY"
    delegate.submit.assert_not_called()
    delegate.cancel_pending.assert_not_called()
