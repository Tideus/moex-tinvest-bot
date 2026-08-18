from datetime import UTC, datetime
from decimal import Decimal

from moex_bot.domain import (
    GeoRiskLevel,
    GeoRiskSnapshot,
    Instrument,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    Side,
    Target,
)
from moex_bot.harness import HarnessResult
from moex_bot.quality import QualityReport
from moex_bot.reporting import render_persisted_shadow_decisions, render_shadow_report


def test_hourly_telegram_report_contains_trade_intents() -> None:
    instrument = Instrument("SBER", "uid", "TQBR", 1, Decimal("0.01"))
    intent = OrderIntent(
        "request", instrument, Side.BUY, 2, Decimal("300"), Decimal("600"), "momentum"
    )
    result = HarnessResult(
        "shadow-test",
        QualityReport(True, (), ()),
        GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ()),
        (Target("SBER", Decimal("0.15"), "momentum"),),
        (OrderRecord(intent, OrderStatus.VALIDATED),),
        (),
    )
    report = render_shadow_report(result, datetime(2026, 8, 18, 10, tzinfo=UTC))
    assert "Виртуальные приказы" in report
    assert "BUY SBER: 2 лот.; 600 RUB" in report
    assert "реальные заявки не отправлялись" in report


def test_persisted_shadow_decisions_show_buy_sell_and_rejections() -> None:
    raw = {
        "run_id": "shadow-test",
        "quality": {"passed": True},
        "geo": {"level": "normal", "multiplier": "1"},
        "targets": [{"secid": "SBER", "weight": "0.15", "rationale": "momentum=0.1"}],
        "market": [
            {
                "instrument": {"secid": "SBER"},
                "price": "300",
                "trend": "290",
                "momentum": "0.1",
            },
            {
                "instrument": {"secid": "GAZP"},
                "price": "150",
                "trend": "155",
                "momentum": "-0.02",
            },
        ],
        "orders": [
            {
                "intent": {
                    "instrument": {"secid": "SBER"},
                    "side": "buy",
                    "lots": 2,
                    "limit_price": "300",
                    "notional": "600",
                },
                "status": "validated",
            },
            {
                "intent": {
                    "instrument": {"secid": "GAZP"},
                    "side": "sell",
                    "lots": 1,
                    "limit_price": "150",
                    "notional": "150",
                },
                "status": "validated",
            },
        ],
        "rejected": [
            {
                "secid": "LKOH",
                "side": "buy",
                "reasons": ["order notional exceeds limit"],
            }
        ],
    }
    report = render_persisted_shadow_decisions(raw)
    assert "SBER: 15.00%" in report
    assert "SBER: SELECTED" in report
    assert "GAZP: SKIPPED" in report
    assert "BUY SBER" in report
    assert "SELL GAZP" in report
    assert "BUY LKOH: order notional exceeds limit" in report
    assert "реальные заявки брокеру не отправлялись" in report
