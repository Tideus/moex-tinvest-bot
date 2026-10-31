import hashlib
import json
import stat
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.live_execution import LiveState, policy_digest
from moex_bot.live_operator import (
    live_operator,
    load_live_settings,
    read_live_plan,
    release_digest,
    require_live_admission,
    validate_review_node,
)
from moex_bot.prod_long import load_prod_long_policy

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 30, 10, tzinfo=UTC)


def _plan(path: Path) -> dict:
    intent = OrderIntent(
        str(uuid4()),
        Instrument(
            "SBER",
            "uid",
            "TQBR",
            10,
            Decimal(".01"),
            issuer_id="sber",
            sector="finance",
            risk_cluster="domestic",
        ),
        Side.BUY,
        1,
        Decimal("100"),
        Decimal("1000"),
        "test",
    )
    origin = path.with_suffix(".portfolio.json")
    origin.write_text(json.dumps({"source": "t_invest_prod_observer", "account_id": "real-iis"}))
    value = {
        "quality": {"passed": True},
        "portfolio_input": {"source": "t_invest_prod_observer", "account_id": "real-iis"},
        "portfolio_input_sha256": hashlib.sha256(origin.read_bytes()).hexdigest(),
        "portfolio_input_path": str(origin.resolve()),
        "run_id": "shadow-" + NOW.isoformat(),
        "orders": [{"status": "validated", "intent": asdict(intent)}],
    }
    path.write_text(json.dumps(value, default=str))
    return value


@pytest.mark.parametrize(
    "failure", ["account", "source", "stale", "future", "quality", "provenance", "source_tampered"]
)
def test_live_plan_has_fresh_production_account_provenance(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "plan.json"
    value = _plan(path)
    if failure == "account":
        value["portfolio_input"]["account_id"] = "other-iis"
    if failure == "source":
        value["portfolio_input"]["source"] = "t_invest_sandbox"
    if failure == "stale":
        value["run_id"] = "shadow-" + (NOW - timedelta(hours=1)).isoformat()
    if failure == "future":
        value["run_id"] = "shadow-" + (NOW + timedelta(minutes=1)).isoformat()
    if failure == "quality":
        value["quality"]["passed"] = False
    if failure == "provenance":
        del value["portfolio_input_sha256"]
    if failure == "source_tampered":
        Path(value["portfolio_input_path"]).write_text('{"account_id":"wrong"}')
    path.write_text(json.dumps(value, default=str))
    with pytest.raises(ValueError):
        read_live_plan(path, 0, NOW, "real-iis")


def test_live_plan_decoder_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    _plan(path)
    before = path.read_bytes()
    assert read_live_plan(path, 0, NOW, "real-iis").lots == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "uid,mode,link,directory",
    [
        (1001, stat.S_IFREG | 0o600, False, False),  # Service-owned 0600 is not root review.
        (0, stat.S_IFREG | 0o666, False, False),
        (0, stat.S_IFDIR | 0o775, False, True),
        (0, stat.S_IFREG | 0o600, True, False),
        (0, stat.S_IFREG | 0o600, False, True),
    ],
)
def test_untrusted_review_file_or_parent_is_rejected(
    uid: int,
    mode: int,
    link: bool,
    directory: bool,
) -> None:
    with pytest.raises(ValueError, match="root-owned"):
        validate_review_node(uid, mode, symlink=link, directory=directory)


def test_root_private_review_and_nonwritable_parent_are_accepted() -> None:
    validate_review_node(0, stat.S_IFREG | 0o640, symlink=False, directory=False)
    validate_review_node(0, stat.S_IFDIR | 0o750, symlink=False, directory=True)


@pytest.mark.parametrize("failure", ["missing_check", "expiry", "release", "policy", "account"])
def test_admission_is_bound_to_reviewed_release_and_explicit_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr("moex_bot.live_operator.assert_root_review_path", lambda path: None)
    policy = replace(
        load_prod_long_policy(ROOT / "config/prod_long.json"), execution_stage="canary"
    )
    value = {
        "version": 1,
        "decision": "APPROVED_CANARY",
        "account_id": "real-iis",
        "release_sha256": "a" * 64,
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
    if failure == "missing_check":
        value["checks"]["protective_orders_tested"] = False
    if failure == "expiry":
        value["expires_at"] = NOW.isoformat()
    if failure == "release":
        value["release_sha256"] = "b" * 64
    if failure == "policy":
        value["policy_sha256"] = "b" * 64
    if failure == "account":
        value["account_id"] = "different"
    path = tmp_path / "admission.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        require_live_admission(path, account="real-iis", policy=policy, release="a" * 64, now=NOW)


@pytest.mark.parametrize("action", ["execute", "enroll"])
def test_committed_live_config_blocks_mutations_before_credentials_or_network(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("disabled live path must never reach network")

    monkeypatch.setattr("moex_bot.live_operator.TInvestLiveBroker", forbidden)
    assert (
        live_operator(
            action,
            settings_path=ROOT / "config/prod_live.json",
            policy_path=ROOT / "config/prod_long.json",
            universe_path=ROOT / "config/universe.json",
            runtime_path=ROOT / "config/prod_runtime.json",
            plan_path=None,
            order_index=0,
            output_path=None,
            confirmation_path=None,
            confirm_digest=None,
        )
        == 2
    )


def test_release_digest_changes_with_strategy_config(tmp_path: Path) -> None:
    policy = tmp_path / "prod_long.json"
    universe = tmp_path / "universe.json"
    settings = tmp_path / "prod_live.json"
    for path in (policy, universe, settings):
        path.write_text("{}")
    before = release_digest(policy, universe, settings)
    (tmp_path / "prod_shadow.json").write_text('{"strategy":"changed"}')
    assert release_digest(policy, universe, settings) != before


def test_committed_live_settings_are_off_and_journal_is_separate() -> None:
    settings = load_live_settings(ROOT / "config/prod_live.json")
    assert settings["enabled"] is False
    assert settings["journal"].endswith("/execution/prod-orders.sqlite3")


def test_operator_dispatch_enroll_preview_execute_reconcile_with_mock_broker_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import moex_bot.live_operator as module

    calls = []

    class MockBroker:
        environment = "prod"

        def __init__(self, token: str, account: str, transport: object, instruments: dict) -> None:
            self.account_id = account
            self.instruments_by_uid = instruments

        def _post(self, method: str, payload: dict) -> dict:
            calls.append(method)
            if method.endswith("GetInstrumentBy"):
                return {"instrument": {"minPriceIncrement": {"units": "0", "nano": 10000000}}}
            if method.endswith("GetOrderBook"):
                return {"asks": [{"price": {"units": "100"}}]}
            raise AssertionError("probe must never call a mutation method")

        def capture(self, intent: OrderIntent) -> LiveState:
            calls.append("capture")
            now = datetime.now(UTC)
            return LiveState(
                self.account_id,
                "ACCOUNT_TYPE_TINKOFF_IIS",
                "ACCOUNT_STATUS_OPEN",
                "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                now,
                now,
                intent.instrument,
                True,
                now,
                Decimal("100"),
                Decimal("100.1"),
                Decimal("40000"),
                Decimal("160000"),
                Decimal("0"),
                Decimal("0"),
                Decimal("1001"),
                {},
                0,
                frozenset(),
            )

        def submit(self, intent: OrderIntent) -> OrderRecord:
            calls.append("MOCK_SUBMIT")
            return OrderRecord(intent, OrderStatus.FILLED, intent.lots)

        def lookup(self, record: OrderRecord) -> OrderRecord:
            calls.append("MOCK_LOOKUP")
            return record

    monkeypatch.setattr(module, "TInvestLiveBroker", MockBroker)
    monkeypatch.setattr(module, "require_live_admission", lambda *args, **kwargs: None)
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "real-iis")
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "mock-not-a-real-token")
    config = tmp_path / "config"
    config.mkdir()
    settings = config / "prod_live.json"
    settings.write_text(
        json.dumps(
            {
                "version": 1,
                "enabled": True,
                "account_id_env": "T_INVEST_PROD_ACCOUNT_ID",
                "journal": str(tmp_path / "prod.sqlite3"),
                "admission": str(tmp_path / "admission.json"),
            }
        )
    )
    policy = config / "prod_long.json"
    policy_raw = json.loads((ROOT / "config/prod_long.json").read_text())
    policy_raw["execution_stage"] = "canary"
    policy.write_text(json.dumps(policy_raw))
    plan = tmp_path / "plan.json"
    value = _plan(plan)
    value["run_id"] = "shadow-" + datetime.now(UTC).isoformat()
    plan.write_text(json.dumps(value, default=str))
    common = dict(
        settings_path=settings,
        policy_path=policy,
        universe_path=ROOT / "config/universe.json",
        runtime_path=ROOT / "config/prod_runtime.json",
        plan_path=plan,
        order_index=0,
        output_path=None,
        confirmation_path=None,
        confirm_digest=None,
    )
    assert live_operator("probe", **{**common, "plan_path": None}) == 0
    assert "MOCK_SUBMIT" not in calls
    assert live_operator("enroll", **common) == 0
    output = tmp_path / "confirmation.json"
    common["output_path"] = output
    assert live_operator("preview", **common) == 0
    assert "MOCK_SUBMIT" not in calls  # Neither enrollment nor preview sends orders.
    approval = json.loads(output.read_text())
    common.update(confirmation_path=output, confirm_digest=approval["intent_sha256"])
    assert live_operator("execute", **common) == 0
    assert calls.count("MOCK_SUBMIT") == 1
    assert live_operator("execute", **common) == 0  # Replay does not reach the mock broker.
    assert calls.count("MOCK_SUBMIT") == 1
    assert live_operator("reconcile", **common) == 0
    assert calls.count("MOCK_SUBMIT") == 1
