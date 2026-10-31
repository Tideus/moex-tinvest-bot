"""Additional malformed-input and provenance cases at the live CLI boundary."""

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_operator import NOW, _plan
from test_tinvest_live import Transport, _broker, _intent

from moex_bot import live_operator as module
from moex_bot.domain import OrderRecord, OrderStatus
from moex_bot.live_execution import intent_digest
from moex_bot.live_operator import load_live_settings, read_live_plan
from moex_bot.prod_long import load_prod_long_policy

ROOT = Path(__file__).resolve().parents[1]


def test_root_review_checks_every_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "review.json"
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda node: SimpleNamespace(
            st_uid=0, st_mode=(stat.S_IFREG | 0o640) if node == path else (stat.S_IFDIR | 0o755)
        ),
    )
    monkeypatch.setattr(Path, "is_symlink", lambda node: False)
    module.assert_root_review_path(path)


@pytest.mark.parametrize("path", [Path("relative.json"), Path("/etc/../tmp/review.json")])
def test_review_path_must_not_traverse(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="absolute"):
        module.assert_root_review_path(path)


def test_complete_current_admission_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from moex_bot.live_execution import policy_digest

    policy = load_prod_long_policy(ROOT / "config/prod_long.json")
    value = {
        "version": 1,
        "decision": "APPROVED_CANARY",
        "account_id": "iis",
        "release_sha256": "release",
        "policy_sha256": policy_digest(policy),
        "reviewed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "checks": {
            name: True
            for name in (
                "current_strategy_promotion_passed",
                "broker_reconciliation_passed",
                "protective_orders_tested",
                "restore_tested",
                "independent_code_review_passed",
            )
        },
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(module, "assert_root_review_path", lambda path: None)
    module.require_live_admission(path, account="iis", policy=policy, release="release", now=NOW)


def test_release_digest_excludes_review_attestation_itself(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("policy.json", "universe.json", "settings.json")]
    for path in paths:
        path.write_text("{}")
    before = module.release_digest(*paths)
    (tmp_path / "prod-admission.json").write_text('{"review":"metadata"}')
    assert module.release_digest(*paths) == before


@pytest.mark.parametrize(
    "fault",
    [
        "runtime",
        "credentials",
        "observer",
        "no_plan",
        "no_output",
        "protected_output",
        "unknown_action",
        "plan_changed",
        "digest",
        "skip",
        "partial",
        "kill",
        "release_changed",
        "final_plan_changed",
        "probe_missing",
        "probe_quote",
        "reconcile",
        "preview_posix",
        "preview_nt",
    ],
)
def test_live_operator_dispatch_failures_and_terminal_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    intent = _intent()
    settings = {
        "enabled": True,
        "journal": str(tmp_path / "orders.sqlite3"),
        "admission": str(tmp_path / "admission.json"),
    }
    policy = replace(
        load_prod_long_policy(ROOT / "config/prod_long.json"),
        execution_stage="observer" if fault == "observer" else "canary",
    )
    monkeypatch.setattr(module, "load_live_settings", lambda path: settings)
    monkeypatch.setattr(module, "load_prod_long_policy", lambda path: policy)
    monkeypatch.setattr(module, "release_digest", lambda *args: "release")
    monkeypatch.setattr(module, "require_live_admission", lambda *a, **k: None)
    monkeypatch.setattr(module, "read_live_plan", lambda *args: intent)
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "fake")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "iis")
    broker = _broker(Transport())
    fake = Mock()
    fake.instruments_by_uid = {}
    fake.capture.return_value = broker.capture(intent)
    fake._post.return_value = {"instrument": {"minPriceIncrement": {"units": "1"}}}
    monkeypatch.setattr(module, "TInvestLiveBroker", lambda *args: fake)
    monkeypatch.setattr(module, "reconcile_live_journal", lambda *args: 1)
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    moment = datetime.now(UTC)
    approval = {
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "account_id": "iis",
        "intent_sha256": intent_digest(intent),
        "policy_sha256": "policy",
        "release_sha256": "release",
        "confirmed_at": moment.isoformat(),
        "expires_at": (moment + timedelta(minutes=5)).isoformat(),
    }
    confirmation = tmp_path / "confirmation.json"
    action = "execute"
    output = tmp_path / "new.json"
    runtime = ROOT / "config/prod_runtime.json"
    if fault == "runtime":
        runtime = ROOT / "config/runtime.json"
    elif fault == "credentials":
        monkeypatch.delenv("T_INVEST_PROD_TOKEN")
    elif fault == "plan_changed":
        approval["plan_sha256"] = "wrong"
    elif fault == "digest":
        approval["intent_sha256"] = "wrong"
    elif fault in {"no_plan", "no_output", "protected_output"}:
        action = "preview"
        if fault == "no_output":
            output = None
        elif fault == "protected_output":
            output = plan
    elif fault == "unknown_action":
        action = "unknown"
    elif fault in {"probe_missing", "probe_quote"}:
        action = "probe"
        if fault == "probe_missing":
            monkeypatch.setattr(module, "load_universe", lambda path: ())
    elif fault == "reconcile":
        action = "reconcile"
    elif fault in {"preview_posix", "preview_nt"}:
        action = "preview"
        Path(settings["journal"]).touch()
        monkeypatch.setattr(module, "registered_live_journal", lambda *args: "registered-uuid")
        monkeypatch.setattr(
            module,
            "os",
            SimpleNamespace(name="posix" if fault == "preview_posix" else "nt", getenv=os.getenv),
        )
    confirmation.write_text(json.dumps(approval))

    def execute(**kwargs: object) -> OrderRecord | None:
        if fault == "skip":
            return None
        if fault == "kill":
            monkeypatch.setattr(
                module, "load_live_settings", lambda path: {**settings, "enabled": False}
            )
        elif fault == "release_changed":
            monkeypatch.setattr(module, "release_digest", lambda *args: "changed")
        elif fault == "final_plan_changed":
            plan.write_text('{"changed":true}')
        kwargs["before_submit"]()
        return OrderRecord(intent, OrderStatus.PARTIALLY_FILLED, 1)

    monkeypatch.setattr(module, "execute_live_intent", execute)
    result = module.live_operator(
        action,
        settings_path=tmp_path / "settings.json",
        policy_path=tmp_path / "policy.json",
        universe_path=ROOT / "config/universe.json",
        runtime_path=runtime,
        plan_path=None
        if fault in {"no_plan", "probe_missing", "probe_quote", "reconcile"}
        else plan,
        order_index=0,
        output_path=output,
        confirmation_path=confirmation,
        confirm_digest=intent_digest(intent),
    )
    assert result == (0 if fault in {"skip", "preview_posix", "preview_nt"} else 2)
    fake.submit.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"enabled": "true"},
        {"account_id_env": "T_INVEST_SANDBOX_ACCOUNT_ID"},
        {"journal": "relative.sqlite3"},
        {"admission": "relative.json"},
        {"journal": 123},
        {"admission": None},
        {"journal": "/var/lib/ledger.json"},
        {"journal": "/same.sqlite3", "admission": "/same.sqlite3"},
    ],
)
def test_live_settings_reject_unsafe_bindings(tmp_path: Path, change: dict) -> None:
    value = {
        "version": 1,
        "enabled": False,
        "account_id_env": "T_INVEST_PROD_ACCOUNT_ID",
        "journal": "/var/lib/ledger.sqlite3",
        "admission": "/etc/review.json",
    }
    value.update(change)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_live_settings(path)


@pytest.mark.parametrize("value", [[], None, 1, "settings"])
def test_settings_require_object(tmp_path: Path, value: object) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="JSON object"):
        load_live_settings(path)


@pytest.mark.parametrize(
    "failure",
    [
        "quality_type",
        "portfolio_type",
        "short_hash",
        "invalid_hash",
        "hash_type",
        "relative_origin",
        "origin_list",
        "origin_source",
        "run_prefix",
        "naive_time",
        "orders_type",
        "bool_index",
        "negative_index",
        "large_index",
    ],
)
def test_plan_rejects_malformed_provenance_and_selection(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "plan.json"
    value = _plan(path)
    index = 0
    if failure == "quality_type":
        value["quality"] = []
    elif failure == "portfolio_type":
        value["portfolio_input"] = []
    elif failure in {"short_hash", "invalid_hash", "hash_type"}:
        value["portfolio_input_sha256"] = {
            "short_hash": "abc",
            "invalid_hash": "g" * 64,
            "hash_type": 3,
        }[failure]
    elif failure == "relative_origin":
        value["portfolio_input_path"] = "relative.json"
    elif failure in {"origin_list", "origin_source"}:
        origin = Path(value["portfolio_input_path"])
        origin.write_text(
            json.dumps(
                []
                if failure == "origin_list"
                else {
                    "account_id": "real-iis",
                    "source": "sandbox",
                }
            )
        )
        value["portfolio_input_sha256"] = hashlib.sha256(origin.read_bytes()).hexdigest()
    elif failure == "run_prefix":
        value["run_id"] = "intraday-" + NOW.isoformat()
    elif failure == "naive_time":
        value["run_id"] = "shadow-" + NOW.replace(tzinfo=None).isoformat()
    elif failure == "orders_type":
        value["orders"] = {}
    else:
        index = {"bool_index": True, "negative_index": -1, "large_index": 2}[failure]
    path.write_text(json.dumps(value, default=str))
    with pytest.raises(ValueError):
        read_live_plan(path, index, NOW, "real-iis")
