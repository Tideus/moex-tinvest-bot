import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from moex_bot.domain import OrderRecord, OrderStatus
from moex_bot.runtime_config import RuntimeConfig, RuntimeSchedule
from moex_bot.sandbox_execution import execute_shadow_plan
from moex_bot.service_config import TInvestEnvironment


class FakeAdapter:
    account_id = "sandbox-1"

    def submit(self, intent: object) -> OrderRecord:
        return OrderRecord(intent=intent, status=OrderStatus.ACCEPTED)  # type: ignore[arg-type]


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
        runtime=runtime,
        adapter=FakeAdapter(),
        as_of=datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
    )
    assert result.submitted[0].intent.confirm_margin_trade
    assert result.submitted[0].intent.side.value == "sell"
