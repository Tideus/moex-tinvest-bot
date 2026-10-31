import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from moex_bot.domain import OrderRecord, OrderStatus
from moex_bot.runtime_config import RuntimeConfig, RuntimeSchedule
from moex_bot.sandbox_execution import execute_shadow_plan, reconcile_execution_journal
from moex_bot.service_config import TInvestEnvironment


class FakeAdapter:
    account_id = "sandbox-1"

    def submit(self, intent: object) -> OrderRecord:
        return OrderRecord(intent=intent, status=OrderStatus.ACCEPTED)  # type: ignore[arg-type]


class SettlingAdapter(FakeAdapter):
    def settle(
        self, records: tuple[OrderRecord, ...], *, ttl_seconds: int
    ) -> tuple[OrderRecord, ...]:
        assert ttl_seconds == 17
        return tuple(
            OrderRecord(
                intent=record.intent,
                status=OrderStatus.FILLED,
                filled_lots=record.intent.lots,
                broker_order_id="broker-1",
            )
            for record in records
        )


def _write_inputs(
    tmp_path: Path, *, open_orders: int = 0, short: bool = False
) -> tuple[Path, Path]:
    shadow = tmp_path / "shadow.json"
    portfolio = tmp_path / "portfolio.json"
    instrument = {
        "secid": "SBER",
        "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
        "board": "TQBR",
        "lot_size": 1,
        "tick_size": "0.01",
        "asset_class": "share",
        "short_enabled": short,
    }
    shadow.write_text(
        json.dumps(
            {
                "run_id": "shadow-2026-08-18T10:00:00+00:00",
                "quality": {"passed": True},
                "orders": [
                    {
                        "status": "validated",
                        "intent": {
                            "order_request_id": str(uuid4()),
                            "instrument": instrument,
                            "side": "sell" if short else "buy",
                            "lots": 1,
                            "limit_price": "300",
                            "notional": "300",
                            "confirm_margin_trade": short,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    portfolio.write_text(
        json.dumps(
            {
                "source": "t_invest_sandbox",
                "account_id": "sandbox-1",
                "observed_at": "2026-08-18T10:00:00+00:00",
                "snapshot_started_at": "2026-08-18T10:00:00+00:00",
                "open_orders": open_orders,
            }
        ),
        encoding="utf-8",
    )
    return shadow, portfolio


def test_enabled_sandbox_submits_validated_plan(tmp_path: Path) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    result = execute_shadow_plan(
        shadow_path=shadow,
        portfolio_path=portfolio,
        output_path=tmp_path / "result.json",
        journal_path=tmp_path / "journal.sqlite3",
        runtime=runtime,
        adapter=FakeAdapter(),
        as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
    )
    assert len(result.submitted) == 1
    assert result.submitted[0].status is OrderStatus.ACCEPTED


def test_active_orders_block_new_sandbox_submission(tmp_path: Path) -> None:
    shadow, portfolio = _write_inputs(tmp_path, open_orders=1)
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    with pytest.raises(ValueError, match="reconciliation"):
        execute_shadow_plan(
            shadow_path=shadow,
            portfolio_path=portfolio,
            output_path=tmp_path / "result.json",
            journal_path=tmp_path / "journal.sqlite3",
            runtime=runtime,
            adapter=FakeAdapter(),
            as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
        )


def test_enabled_sandbox_accepts_verified_explicit_short_plan(tmp_path: Path) -> None:
    shadow, portfolio = _write_inputs(tmp_path, short=True)
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    result = execute_shadow_plan(
        shadow_path=shadow,
        portfolio_path=portfolio,
        output_path=tmp_path / "short-result.json",
        journal_path=tmp_path / "journal.sqlite3",
        runtime=runtime,
        adapter=FakeAdapter(),
        as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
    )
    assert result.submitted[0].intent.confirm_margin_trade
    assert result.submitted[0].intent.side.value == "sell"


def test_execution_finalizes_orders_with_adapter_lifecycle(tmp_path: Path) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    result = execute_shadow_plan(
        shadow_path=shadow,
        portfolio_path=portfolio,
        output_path=tmp_path / "settled.json",
        journal_path=tmp_path / "journal.sqlite3",
        runtime=runtime,
        adapter=SettlingAdapter(),
        as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
        order_ttl_seconds=17,
    )
    assert result.submitted[0].status is OrderStatus.FILLED
    assert result.stopped_reason is None


def test_crash_after_broker_acceptance_cannot_resubmit_after_restart(tmp_path: Path) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    calls = []

    class CrashAdapter(FakeAdapter):
        def submit(self, intent: object) -> OrderRecord:
            calls.append(intent)
            raise TimeoutError("broker may have accepted the order")

    kwargs = dict(
        shadow_path=shadow,
        portfolio_path=portfolio,
        output_path=tmp_path / "result.json",
        journal_path=tmp_path / "journal.sqlite3",
        runtime=runtime,
        adapter=CrashAdapter(),
        as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
    )
    with pytest.raises(TimeoutError):
        execute_shadow_plan(**kwargs)
    # Even a different output location cannot create a second submission.
    kwargs["output_path"] = tmp_path / "different" / "result.json"
    with pytest.raises(ValueError, match="reconciliation"):
        execute_shadow_plan(**kwargs)
    assert len(calls) == 1

    class LookupAdapter(FakeAdapter):
        def lookup(self, record: OrderRecord) -> OrderRecord:
            return OrderRecord(record.intent, OrderStatus.UNKNOWN)

    assert reconcile_execution_journal(tmp_path / "journal.sqlite3", LookupAdapter()) == 1

    class FinalLookupAdapter(FakeAdapter):
        def lookup(self, record: OrderRecord) -> OrderRecord:
            return OrderRecord(record.intent, OrderStatus.FILLED, record.intent.lots)

    assert reconcile_execution_journal(tmp_path / "journal.sqlite3", FinalLookupAdapter()) == 0
    result = execute_shadow_plan(**kwargs)
    assert not result.submitted
    assert len(calls) == 1


@pytest.mark.parametrize(
    "observed",
    [None, "2026-08-18T09:00:00+00:00", "2026-08-18T10:00:00", "2026-08-18T10:02:00+00:00"],
)
def test_stale_naive_missing_or_future_account_snapshot_blocks_submit(
    tmp_path: Path,
    observed: str | None,
) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    data = json.loads(portfolio.read_text())
    data["observed_at"] = observed
    portfolio.write_text(json.dumps(data))
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    with pytest.raises(ValueError):
        execute_shadow_plan(
            shadow_path=shadow,
            portfolio_path=portfolio,
            output_path=tmp_path / "result.json",
            journal_path=tmp_path / "journal.sqlite3",
            runtime=runtime,
            adapter=FakeAdapter(),
            as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
        )


def test_cancelled_order_stops_batch_until_new_account_snapshot(tmp_path: Path) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    data = json.loads(shadow.read_text())
    data["orders"].append(data["orders"][0])
    shadow.write_text(json.dumps(data))

    class CancelAdapter(FakeAdapter):
        def submit(self, intent: object) -> OrderRecord:
            return OrderRecord(intent, OrderStatus.CANCELLED)  # type: ignore[arg-type]

    result = execute_shadow_plan(
        shadow_path=shadow,
        portfolio_path=portfolio,
        output_path=tmp_path / "result.json",
        journal_path=tmp_path / "journal.sqlite3",
        runtime=RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3),
        adapter=CancelAdapter(),
        as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
    )
    assert len(result.submitted) == 1
    assert result.stopped_reason


def test_partial_flat_is_closed_with_new_uuid_after_terminal_and_fresh_snapshot(
    tmp_path: Path,
) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    plan = json.loads(shadow.read_text())
    account = json.loads(portfolio.read_text())
    now = datetime.now(UTC)
    plan["run_id"] = f"intraday-{now.isoformat()}"
    raw = plan["orders"][0]["intent"]
    raw.update(side="sell", lots=10, notional="3000", rationale="intraday mandatory flat")
    account.update(observed_at=now.isoformat(), snapshot_started_at=now.isoformat(),
                   positions={"SBER": {"lots": 10}})
    shadow.write_text(json.dumps(plan))
    portfolio.write_text(json.dumps(account))

    class PartialCancel(FakeAdapter):
        def submit(self, intent: object) -> OrderRecord:
            return OrderRecord(intent, OrderStatus.CANCELLED, 4)  # type: ignore[arg-type]

    kwargs = dict(
        shadow_path=shadow,
        portfolio_path=portfolio,
        output_path=tmp_path / "result.json",
        journal_path=tmp_path / "journal.sqlite3",
        runtime=RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3),
        as_of=now,
        adapter=PartialCancel(),
    )
    first = execute_shadow_plan(**kwargs)
    first_id = first.submitted[0].intent.order_request_id
    later = datetime.now(UTC) + timedelta(microseconds=1)
    plan["run_id"] = f"intraday-{later.isoformat()}"
    raw.update(lots=6, notional="1800")
    account.update(observed_at=later.isoformat(), snapshot_started_at=later.isoformat(),
                   positions={"SBER": {"lots": 6}})
    shadow.write_text(json.dumps(plan))
    portfolio.write_text(json.dumps(account))
    kwargs.update(as_of=later, adapter=SettlingAdapter(), order_ttl_seconds=17)
    account["snapshot_started_at"] = now.isoformat()
    portfolio.write_text(json.dumps(account))
    with pytest.raises(ValueError, match="snapshot"):
        execute_shadow_plan(**kwargs)
    account["snapshot_started_at"] = later.isoformat()
    portfolio.write_text(json.dumps(account))
    second = execute_shadow_plan(**kwargs)
    assert second.submitted[0].intent.order_request_id != first_id
    assert second.submitted[0].filled_lots == 6
    assert second.stopped_reason is None


@pytest.mark.parametrize("empty_file", [False, True])
def test_existing_history_cannot_silently_start_empty_journal(
    tmp_path: Path, empty_file: bool,
) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "sandbox-execution-old.json").write_text("{}")
    if empty_file:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "journal.sqlite3").touch()
    with pytest.raises(ValueError, match="migration"):
        execute_shadow_plan(
            shadow_path=shadow,
            portfolio_path=portfolio,
            output_path=tmp_path / "result.json",
            journal_path=tmp_path / "data" / "journal.sqlite3",
            runtime=RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3),
            adapter=FakeAdapter(),
            as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
        )
