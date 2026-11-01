"""Supervised operator boundaries use private files and fake broker ports only."""

import argparse
import hashlib
import json
import os
import runpy
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_live_execution import _setup

from moex_bot import supervised_operator as module
from moex_bot.live_execution import policy_digest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    monkeypatch.setattr(module, "notify_experiment", Mock())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    _, kw, _ = _setup(tmp_path)
    settings = json.loads((ROOT / "config/prod_experiment.json").read_text())
    settings.update(
        journal=str(kw["journal_path"]),
        protection_journal=str(tmp_path / "protection.sqlite3"),
        admission=str(tmp_path / "review.json"),
    )
    monkeypatch.setattr(module, "load_experiment_settings", lambda path: settings)
    monkeypatch.setattr(module, "release_digest", lambda *args: "release")
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "fake")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "real-iis")
    broker = Mock()
    broker._post.return_value = {
        "instrument": {"minPriceIncrement": {"units": "0", "nano": 10000000}}
    }
    broker.capture.side_effect = lambda intent: replace(
        kw["broker"].state, instrument=intent.instrument
    )
    monkeypatch.setattr(module, "SupervisedTInvestBroker", lambda *args: broker)
    monkeypatch.setattr(module, "NativeProtectionBroker", lambda *args: Mock())
    monkeypatch.setattr(module, "UrlLibJsonTransport", lambda: Mock())
    args = argparse.Namespace(
        action="preview",
        settings=tmp_path / "settings.json",
        policy=ROOT / "config/prod_long.json",
        universe=ROOT / "config/universe.json",
        preview=None,
        confirm_preview_sha256=None,
        ticker="SBER",
        limit_price="100",
        stop_trigger="95",
        stop_limit=None,
        kind="stop_loss_market",
        output=tmp_path / "preview.json",
    )
    return args, settings, broker


def preview(setup):
    args, _, _ = setup
    assert module.operate(args) == 0
    raw = json.loads(args.output.read_text())
    args.preview = args.output
    args.confirm_preview_sha256 = hashlib.sha256(args.preview.read_bytes()).hexdigest()
    return raw


def test_take_profit_is_bound_to_entry_preview(setup):
    args, settings, _ = setup
    args.take_profit = "102"
    raw = preview(setup)
    _, plan = module.decode_preview(raw, "real-iis", "release",
                                     module.experiment_policy(args.policy, settings),
                                     datetime.now(UTC))
    assert str(plan.take_profit) == "102"
    assert raw["protection"]["take_profit"] == "102"


@pytest.mark.parametrize("platform,kind", [("nt", "stop_loss_market"), ("posix", "stop_limit")])
def test_preview_is_private_read_only_and_exact_paired_content(setup, monkeypatch, platform, kind):
    args, _, broker = setup
    args.kind = kind
    args.stop_limit = "94" if kind == "stop_limit" else None
    monkeypatch.setattr(module, "os", SimpleNamespace(name=platform, getenv=os.getenv))
    raw = preview(setup)
    policy = module.experiment_policy(args.policy, setup[1])
    intent, protection = module.decode_preview(
        raw, "real-iis", "release", policy, datetime.now(UTC)
    )
    assert intent.lots == 1 and protection.lots == 1
    assert raw["order_submitted"] is False
    broker.submit.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "missing_credentials",
        "unknown_ticker",
        "not_allowed",
        "protected",
        "excess_notional",
        "account",
        "account_type",
        "missing_output",
        "output_suffix",
        "config_output",
        "new_ledger",
    ],
)
def test_preview_boundary_guards(setup, monkeypatch, fault):
    args, settings, broker = setup
    if fault == "missing_credentials":
        monkeypatch.delenv("T_INVEST_PROD_TOKEN")
    elif fault == "unknown_ticker":
        args.ticker = "UNKNOWN"
    elif fault == "not_allowed":
        args.ticker = "LKOH"
    elif fault == "protected":
        policy = module.experiment_policy(args.policy, settings)
        monkeypatch.setattr(
            module,
            "experiment_policy",
            lambda *args: replace(policy, protected_secids=policy.protected_secids | {"SBER"}),
        )
    elif fault == "excess_notional":
        args.limit_price = "100000"
    elif fault in {"account", "account_type"}:
        original = broker.capture.side_effect
        broker.capture.side_effect = lambda intent: replace(
            original(intent), **{"account_id" if fault == "account" else "account_type": "wrong"}
        )
    elif fault == "missing_output":
        args.output = None
    elif fault == "output_suffix":
        args.output = args.output.with_suffix(".txt")
    elif fault == "config_output":
        args.output = args.settings
    elif fault == "new_ledger":
        settings["journal"] = str(args.output.with_suffix(".sqlite3"))
        raw = preview(setup)
        assert raw["journal_uuid"] is None
        return
    with pytest.raises(ValueError):
        module.operate(args)
    broker.submit.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing_preview",
        "digest",
        "disabled",
        "wrong_ledger",
        "kill",
        "release",
        "preview_changed",
        "not_filled",
    ],
)
def test_enroll_and_execute_require_complete_hash_and_recheck_authority(setup, monkeypatch, fault):
    args, settings, broker = setup
    preview(setup)
    args.action = "enroll"
    enroll = Mock(return_value="local-ledger")
    monkeypatch.setattr(module, "enroll_live_journal", enroll)
    assert module.operate(args) == 0
    enroll.assert_called_once()
    args.action = "execute"
    if fault != "disabled":
        settings.update(enabled=True, stage="supervised_canary")
    review = Mock()
    monkeypatch.setattr(module, "require_experiment_review", review)
    if fault == "missing_preview":
        args.preview = None
    elif fault == "digest":
        args.confirm_preview_sha256 = "wrong"
    elif fault == "wrong_ledger":
        monkeypatch.setattr(module, "registered_live_journal", lambda *args: "wrong")

    def runner(**kwargs):
        if fault == "kill":
            settings["enabled"] = False
        elif fault == "release":
            monkeypatch.setattr(module, "release_digest", lambda *args: "changed")
        elif fault == "preview_changed":
            args.preview.write_text("{}")
        kwargs["approval_gate"]()
        kwargs["entry_approval_gate"]()
        return {
            "state": "ENTRY_NOT_FILLED" if fault == "not_filled" else "FILLED_PROTECTED",
            "filled_lots": 0 if fault == "not_filled" else 1,
            "protection": "PLANNED_REVIEW_REQUIRED" if fault == "not_filled" else "ACTIVE",
        }

    monkeypatch.setattr(module, "run_supervised_entry", runner)
    if fault in {"none", "not_filled"}:
        assert module.operate(args) == (0 if fault == "none" else 2)
        assert review.call_count == 3
        assert review.call_args.kwargs["min_remaining_seconds"] == 180
    else:
        with pytest.raises(ValueError):
            module.operate(args)
    broker.submit.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", 2),
        ("account_id", "other"),
        ("release_sha256", "other"),
        ("policy_sha256", "other"),
        ("cancel_unfilled_after_seconds", 60),
        ("created_at", "2026-10-30T10:00:00"),
        ("expires_at", "2026-10-30T10:00:00"),
    ],
)
def test_preview_identity_and_time_guards(setup, field, value):
    args, settings, _ = setup
    raw = preview(setup)
    raw[field] = value
    with pytest.raises(ValueError):
        module.decode_preview(
            raw,
            "real-iis",
            "release",
            module.experiment_policy(args.policy, settings),
            datetime.now(UTC),
        )


@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "long_expiry",
        "now_naive",
        "sell",
        "lots",
        "copied_plan",
        "logical",
        "request",
        "price",
        "not_allowed",
        "protected",
        "excess",
    ],
)
def test_preview_pair_instrument_scope_is_immutable(setup, fault):
    args, settings, _ = setup
    raw = preview(setup)
    policy = module.experiment_policy(args.policy, settings)
    now = datetime.now(UTC)
    if fault == "expired":
        now += timedelta(minutes=10)
    elif fault == "long_expiry":
        raw["expires_at"] = (now + timedelta(hours=1)).isoformat()
    elif fault == "now_naive":
        now = now.replace(tzinfo=None)
    elif fault == "sell":
        raw["intent"]["side"] = "sell"
    elif fault == "lots":
        raw["intent"]["lots"] = 2
    elif fault == "copied_plan":
        raw["protection"]["instrument"]["uid"] = "other"
    elif fault == "logical":
        raw["protection"]["entry_logical_id"] = str(uuid4())
    elif fault == "request":
        raw["protection"]["entry_request_id"] = str(uuid4())
    elif fault == "price":
        raw["protection"]["entry_price"] = "101"
    elif fault == "not_allowed":
        policy = replace(policy, allowed_secids=frozenset())
    elif fault == "protected":
        policy = replace(policy, protected_secids=policy.protected_secids | {"SBER"})
    else:
        raw["intent"]["notional"] = "10001"
    with pytest.raises(ValueError):
        module.decode_preview(raw, "real-iis", "release", policy, now)


@pytest.mark.parametrize(
    "field,value",
    [("uid", "copied-uid"), ("issuer_id", "other"), ("sector", "other"), ("risk_cluster", "other")],
)
def test_copied_risk_metadata_is_rejected_even_with_new_confirmed_hash(setup, field, value):
    args, settings, broker = setup
    raw = preview(setup)
    raw["intent"]["instrument"][field] = value
    raw["protection"]["instrument"][field] = value
    args.preview.write_text(json.dumps(raw))
    args.confirm_preview_sha256 = hashlib.sha256(args.preview.read_bytes()).hexdigest()
    args.action = "execute"
    settings.update(enabled=True, stage="supervised_canary")
    with pytest.raises(ValueError, match="trusted instrument universe"):
        module.operate(args)
    broker.submit.assert_not_called()


@pytest.mark.parametrize("budget,accepted", [("5.5", True), ("5.49", False)])
def test_planned_stop_distance_includes_transaction_cost_allowance(setup, budget, accepted):
    args, settings, _ = setup
    raw = preview(setup)
    intent, protection = module.decode_preview(
        raw,
        "real-iis",
        "release",
        module.experiment_policy(args.policy, settings),
        datetime.now(UTC),
    )
    constrained = {**settings, "max_daily_loss_rub": budget}
    if accepted:
        module.require_planned_risk(intent, protection, constrained)
    else:
        with pytest.raises(ValueError, match="loss budget"):
            module.require_planned_risk(intent, protection, constrained)


def test_preview_rejects_stop_distance_that_exceeds_budget_before_capture(setup):
    args, settings, broker = setup
    args.stop_trigger = "1"
    settings["max_daily_loss_rub"] = "50"
    with pytest.raises(ValueError, match="loss budget"):
        module.operate(args)
    broker.capture.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "version",
        "decision",
        "scope",
        "profitability",
        "account",
        "release",
        "policy",
        "review_naive",
        "expiry_naive",
        "now_naive",
        "expired",
        "long",
        "checks",
        "flag",
        "headroom",
    ],
)
def test_root_review_is_scoped_to_experiment_not_profitability(setup, monkeypatch, fault):
    args, settings, _ = setup
    policy = module.experiment_policy(args.policy, settings)
    now = datetime.now(UTC)
    raw = {
        "version": 1,
        "decision": "APPROVED_SUPERVISED_EXPERIMENT",
        "scope": "ONE_LOT_TECHNICAL_TEST",
        "strategy_profitability": "NOT_ESTABLISHED",
        "account_id": "real-iis",
        "release_sha256": "release",
        "policy_sha256": policy_digest(policy),
        "reviewed_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "checks": {
            key: True
            for key in (
                "experimental_risk_acknowledged",
                "broker_readonly_preflight_passed",
                "protection_contract_tests_passed",
                "restore_drill_passed",
                "independent_code_review_passed",
                "supervised_session_active",
            )
        },
    }
    field = {
        "version": "version",
        "decision": "decision",
        "scope": "scope",
        "profitability": "strategy_profitability",
        "account": "account_id",
        "release": "release_sha256",
        "policy": "policy_sha256",
    }.get(fault)
    if field:
        raw[field] = "wrong"
    elif fault == "review_naive":
        raw["reviewed_at"] = now.replace(tzinfo=None).isoformat()
    elif fault == "expiry_naive":
        raw["expires_at"] = now.replace(tzinfo=None).isoformat()
    elif fault == "now_naive":
        now = now.replace(tzinfo=None)
    elif fault == "expired":
        raw["expires_at"] = now.isoformat()
    elif fault == "long":
        raw["expires_at"] = (now + timedelta(hours=2)).isoformat()
    elif fault == "checks":
        raw["checks"] = []
    elif fault == "flag":
        raw["checks"]["experimental_risk_acknowledged"] = 1
    elif fault == "headroom":
        raw["expires_at"] = (now + timedelta(seconds=179)).isoformat()
    path = Path(settings["admission"])
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(module, "assert_root_review_path", lambda path: None)
    if fault == "none":
        module.require_experiment_review(path, "real-iis", policy, "release", now)
    else:
        with pytest.raises(ValueError):
            module.require_experiment_review(
                path,
                "real-iis",
                policy,
                "release",
                now,
                min_remaining_seconds=180 if fault == "headroom" else 0,
            )


def test_malformed_preview_root_is_rejected_before_decode(setup):
    args, _, broker = setup
    preview(setup)
    args.preview.write_text("[]")
    args.confirm_preview_sha256 = hashlib.sha256(args.preview.read_bytes()).hexdigest()
    args.action = "enroll"
    with pytest.raises(ValueError, match="JSON object"):
        module.operate(args)
    broker.submit.assert_not_called()


def test_preview_is_parsed_from_same_confirmed_bytes_not_second_file_read(setup, monkeypatch):
    args, _, _ = setup
    preview(setup)
    args.action = "enroll"
    original = Path.read_text

    def changed_read(path, *pos, **kw):
        return "[]" if path == args.preview else original(path, *pos, **kw)

    monkeypatch.setattr(Path, "read_text", changed_read)
    monkeypatch.setattr(module, "enroll_live_journal", lambda *args, **kwargs: "local-id")
    assert module.operate(args) == 0


@pytest.mark.parametrize("action", ["preview", "enroll", "execute"])
@pytest.mark.parametrize("failure", [False, True])
def test_main_locks_only_mutable_sessions_and_alerts_execute_failure(
    setup,
    monkeypatch,
    capsys,
    action,
    failure,
):
    args, _, _ = setup
    entered = []

    @contextmanager
    def session():
        entered.append(True)
        yield

    def operate(parsed):
        if failure:
            raise KeyboardInterrupt("PRIVATE_ERROR")
        return 0

    monkeypatch.setattr(module, "production_session", session)
    monkeypatch.setattr(module, "operate", operate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "operator",
            action,
            "--settings",
            str(args.settings),
            "--policy",
            str(args.policy),
            "--universe",
            str(args.universe),
        ],
    )
    assert module.main() == (2 if failure else 0)
    assert bool(entered) is (action != "preview")
    text = capsys.readouterr().out
    assert "PRIVATE_ERROR" not in text
    assert ("CRITICAL_CHECK_ACCOUNT" in text) is (action == "execute" and failure)
    assert module.notify_experiment.call_count == int(action == "execute" and failure)


@pytest.mark.parametrize("failure", [False, True])
def test_main_masks_private_broker_exception_text(setup, monkeypatch, capsys, failure):
    args, _, _ = setup
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "operator",
            "preview",
            "--settings",
            str(args.settings),
            "--policy",
            str(args.policy),
            "--universe",
            str(args.universe),
        ],
    )

    def operate(parsed):
        if failure:
            raise ValueError("SECRET_TOKEN")
        return 0

    monkeypatch.setattr(module, "operate", operate)
    assert module.main() == (2 if failure else 0)
    assert "SECRET_TOKEN" not in capsys.readouterr().out


def test_module_entrypoint_does_not_trade_on_invalid_settings(setup, monkeypatch):
    args, _, _ = setup
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "operator",
            "preview",
            "--settings",
            str(args.settings),
            "--policy",
            str(args.policy),
            "--universe",
            str(args.universe),
        ],
    )
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as result:
        runpy.run_module("moex_bot.supervised_operator", run_name="__main__")
    assert result.value.code == 2
