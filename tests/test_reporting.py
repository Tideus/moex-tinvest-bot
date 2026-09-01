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
from moex_bot.harness import HarnessResult, SignalDiagnostic
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
        (
            Target(
                "SBER",
                Decimal("0.15"),
                "momentum=0.029122; price=300.5>trend=290.25; geo_multiplier=1",
            ),
        ),
        (OrderRecord(intent, OrderStatus.VALIDATED),),
        (
            {
                "side": "buy",
                "secid": "LKOH",
                "reasons": ("daily turnover limit exceeded",),
            },
        ),
    )
    report = render_shadow_report(result, datetime(2026, 8, 18, 10, tzinfo=UTC))
    assert "18.08.2026 · 13:00 МСК" in report
    assert "🎯 ЦЕЛЕВОЙ ПОРТФЕЛЬ" in report
    assert "импульс +2,91% · цена 300,50 ₽ выше тренда 290,25 ₽" in report
    assert "🧾 ЗАЯВКИ ДЛЯ T‑INVEST SANDBOX" in report
    assert "ВИРТУАЛЬНЫЕ СДЕЛКИ" not in report
    assert "🟢 BUY SBER · 2 лот. · 600 ₽" in report
    assert "BUY LKOH — исчерпан дневной лимит оборота" in report
    assert "Разрешённые заявки отправляются только" in report


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


def test_monitoring_report_explains_empty_signal_and_next_rebalance() -> None:
    result = HarnessResult(
        "shadow-monitor",
        QualityReport(True, (), ()),
        GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ()),
        (),
        (),
        (),
        rebalance_allowed=False,
        rebalance_hours_moscow=(10,),
        signal_diagnostics=(
            SignalDiagnostic(
                "SBER",
                Decimal("-0.0088"),
                Decimal("0.01"),
                Decimal("277.72"),
                Decimal("288.69"),
                False,
                ("momentum threshold not passed", "price is not above trend"),
            ),
        ),
    )

    report = render_shadow_report(result, datetime(2026, 8, 18, 13, 53, tzinfo=UTC))

    assert "Целевых бумаг после фильтров: 0" in report
    assert "Кандидатов нет" in report
    assert "SBER: импульс -0,88% (нужно >+1,00%); цена ≤ тренда" in report
    assert "Следующее разрешённое окно: 19.08.2026 · 10:00–10:59 МСК" in report


def test_empty_rebalance_plan_does_not_claim_risk_rejection() -> None:
    result = HarnessResult(
        "shadow-rebalance",
        QualityReport(True, (), ()),
        GeoRiskSnapshot(GeoRiskLevel.NORMAL, Decimal("1"), frozenset(), ()),
        (),
        (),
        (),
    )

    report = render_shadow_report(result, datetime(2026, 8, 19, 7, 5, tzinfo=UTC))

    assert "целевой список пуст, поэтому risk-control нечего проверять" in report
