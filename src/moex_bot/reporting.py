from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .harness import HarnessResult

MOSCOW = ZoneInfo("Europe/Moscow")

_GEO_LABELS = {
    "normal": "нормальный",
    "elevated": "повышенный",
    "high": "высокий",
    "critical": "критический",
    "recovery": "восстановление",
}
_SECTOR_LABELS = {
    "financials": "финансы",
    "energy": "энергетика",
    "metals_mining": "металлы и добыча",
    "materials": "материалы",
    "technology": "технологии",
    "consumer_staples": "товары первой необходимости",
    "telecommunications": "телеком",
}
_RISK_REASON_LABELS = {
    "blocked by geopolitical risk policy": "покупка запрещена геориск-политикой",
    "new exposure forbidden in CRITICAL mode": "новые позиции запрещены при критическом геориске",
    "order notional exceeds limit": "сумма одной заявки выше лимита",
    "daily turnover limit exceeded": "исчерпан дневной лимит оборота",
    "open order limit reached": "достигнут лимит незавершённых заявок",
    "active broker orders require position and exposure reconciliation": (
        "у брокера есть активные заявки — сначала нужна сверка позиций"
    ),
    "sell requires an existing long position; shorting forbidden": (
        "нет длинной позиции для продажи; шорт пока запрещён"
    ),
    "sell quantity exceeds unblocked long position; shorting forbidden": (
        "объём продажи превышает свободный остаток позиции"
    ),
    "insufficient spendable cash after mandatory reserve; margin forbidden": (
        "недостаточно свободных денег после обязательного резерва"
    ),
    "non-positive portfolio equity": "капитал счёта не положительный",
    "resulting position exceeds position-weight limit": "позиция превысит лимит веса",
    "projected gross exposure exceeds limit": "портфель превысит лимит общей экспозиции",
    "projected sector exposure exceeds limit": "превышен лимит на один сектор",
    "projected risk-cluster exposure exceeds limit": "превышен лимит группы связанных рисков",
}


class FlowState(StrEnum):
    BUY_DOMINANT = "buy_dominant"
    BALANCED = "balanced"
    SELL_DOMINANT = "sell_dominant"


@dataclass(frozen=True, slots=True)
class EquityFlowSnapshot:
    secid: str
    window_start: datetime
    window_end: datetime
    buy_value: Decimal
    sell_value: Decimal
    intervals: int

    @property
    def imbalance(self) -> Decimal:
        total = self.buy_value + self.sell_value
        return Decimal("0") if total == 0 else (self.buy_value - self.sell_value) / total

    @property
    def state(self) -> FlowState:
        if self.imbalance > Decimal("0.10"):
            return FlowState.BUY_DOMINANT
        if self.imbalance < Decimal("-0.10"):
            return FlowState.SELL_DOMINANT
        return FlowState.BALANCED


@dataclass(frozen=True, slots=True)
class FutoiGroupSnapshot:
    group: str
    net_contracts: Decimal
    gross_long: Decimal
    gross_short: Decimal
    long_participants: int
    short_participants: int


@dataclass(frozen=True, slots=True)
class FutoiSnapshot:
    ticker: str
    observed_at: datetime
    groups: tuple[FutoiGroupSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ConcentrationSnapshot:
    secid: str
    observed_at: datetime
    metrics: Mapping[str, Decimal]


def _number(value: Decimal, places: int = 2, *, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    rendered = f"{value:,.{places}f}".replace(",", "\u00a0").replace(".", ",")
    return prefix + rendered


def _money(value: Decimal) -> str:
    places = 0 if value == value.to_integral_value() else 2
    return f"{_number(value, places)} ₽"


def _rationale_metrics(rationale: str) -> tuple[Decimal, Decimal, Decimal] | None:
    values: dict[str, str] = {}
    for part in rationale.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator:
            values[key] = value
    price, separator, trend = values.get("price", "").partition(">trend=")
    if not separator:
        return None
    try:
        return Decimal(values["momentum"]), Decimal(price), Decimal(trend)
    except (KeyError, ArithmeticError):
        return None


def _risk_reason(reason: object) -> str:
    raw = str(reason)
    return _RISK_REASON_LABELS.get(raw, raw)


def render_shadow_report(result: HarnessResult, as_of: datetime) -> str:
    local_time = as_of.astimezone(MOSCOW)
    quality_icon = "✅" if result.quality.passed else "⛔"
    quality_text = "данные корректны" if result.quality.passed else "расчёт заблокирован"
    geo_label = _GEO_LABELS.get(result.geo.level.value, result.geo.level.value)
    lines = [
        "🧪 MOEX BOT · SHADOW",
        local_time.strftime("%d.%m.%Y · %H:%M МСК"),
        "",
        f"{quality_icon} Качество: {quality_text}",
        f"🌍 Геориск: {geo_label} · размер позиций ×{_number(result.geo.multiplier)}",
        "",
        "📊 ИТОГ",
        f"Выбрано бумаг: {len(result.targets)}",
        f"Прошло риск-контроль: {len(result.orders)}",
        f"Отклонено: {len(result.rejected)}",
    ]
    if result.quality.errors:
        lines.extend(("", "Причины блокировки:"))
        lines.extend(f"• {_risk_reason(value)}" for value in result.quality.errors[:5])
    if result.portfolio is not None:
        portfolio = result.portfolio
        equity_text = (
            "не передан"
            if portfolio.reported_equity is None
            else _money(portfolio.reported_equity)
        )
        lines.extend(
            (
                "",
                "💼 СЧЁТ",
                f"Капитал: {equity_text}",
                f"Свободно: {_money(portfolio.cash)}",
                f"Заблокировано: {_money(portfolio.blocked_cash)}",
                f"Позиции: {len(portfolio.positions)} · заявки у брокера: {portfolio.open_orders}",
            )
        )
    if result.targets:
        lines.extend(("", "🎯 ЦЕЛЕВОЙ ПОРТФЕЛЬ"))
        for index, target in enumerate(result.targets[:12], start=1):
            lines.append(f"{index}. {target.secid} · цель {_number(target.weight * 100)}%")
            metrics = _rationale_metrics(target.rationale)
            if metrics is not None:
                momentum, price, trend = metrics
                lines.append(
                    f"   импульс {_number(momentum * 100, signed=True)}% · "
                    f"цена {_money(price)} · тренд {_money(trend)}"
                )
    if result.orders:
        lines.extend(("", "🧾 ВИРТУАЛЬНЫЕ СДЕЛКИ"))
        for record in result.orders[:12]:
            intent = record.intent
            icon = "🟢" if intent.side.value == "buy" else "🔴"
            sector = _SECTOR_LABELS.get(intent.instrument.sector, intent.instrument.sector)
            lines.append(
                f"{icon} {intent.side.value.upper()} {intent.instrument.secid} · "
                f"{intent.lots} лот. · {_money(intent.notional)}"
            )
            lines.append(f"   лимит {_money(intent.limit_price)} · сектор: {sector}")
    elif result.quality.passed:
        lines.extend(("", "🧾 ВИРТУАЛЬНЫЕ СДЕЛКИ", "Нет сделок, прошедших риск-контроль."))
    if result.rejected:
        lines.extend(("", "⛔ НЕ ПРОШЛИ РИСК-КОНТРОЛЬ"))
        for item in result.rejected[:12]:
            reasons_raw = item.get("reasons", ())
            reasons = (
                "; ".join(_risk_reason(value) for value in reasons_raw)
                if isinstance(reasons_raw, (list, tuple))
                else _risk_reason(reasons_raw)
            )
            lines.append(
                f"• {str(item.get('side', '?')).upper()} {item.get('secid', '?')} — "
                f"{reasons or 'причина не указана'}"
            )
    lines.extend(
        (
            "",
            "ℹ️ SHADOW: это расчёт возможных действий. Реальные заявки брокеру не отправлялись.",
            "Импульс — изменение цены в окне стратегии; "
            "тренд — средняя цена для фильтра направления.",
        )
    )
    report = "\n".join(lines)
    if len(report) > 4096:
        report = report[:4000] + "\n… отчёт сокращён; полный результат сохранён в JSON."
    return report


def render_persisted_shadow_decisions(raw: Mapping[str, object]) -> str:
    quality = raw.get("quality")
    geo = raw.get("geo")
    targets = raw.get("targets")
    orders = raw.get("orders")
    rejected = raw.get("rejected")
    market = raw.get("market", [])
    if not isinstance(quality, Mapping) or not isinstance(geo, Mapping):
        raise ValueError("shadow artifact has no quality/geo sections")
    if (
        not isinstance(targets, list)
        or not isinstance(orders, list)
        or not isinstance(rejected, list)
    ):
        raise ValueError("shadow artifact has invalid decision sections")
    lines = [
        f"Run: {raw.get('run_id', 'unknown')}",
        f"Качество: {'OK' if quality.get('passed') else 'BLOCKED'}",
        f"Геориск: {geo.get('level', 'unknown')}; множитель={geo.get('multiplier', '?')}",
        "",
        "ЦЕЛЕВЫЕ ВЕСА:",
    ]
    if not targets:
        lines.append("- нет: стратегия не выбрала ни одной бумаги")
    for item in targets:
        if not isinstance(item, Mapping):
            raise ValueError("shadow target entry must be an object")
        weight = Decimal(str(item.get("weight", "0"))) * 100
        lines.append(f"- {item.get('secid', '?')}: {weight:.2f}% — {item.get('rationale', '')}")
    if market:
        if not isinstance(market, list):
            raise ValueError("shadow market section must be a list")
        selected = {
            str(item.get("secid")) for item in targets if isinstance(item, Mapping)
        }
        lines.extend(("", "ВСЕ ПРОВЕРЕННЫЕ БУМАГИ:"))
        for item in market:
            if not isinstance(item, Mapping) or not isinstance(item.get("instrument"), Mapping):
                raise ValueError("shadow market entry must contain instrument")
            instrument = item["instrument"]
            secid = str(instrument.get("secid", "?"))
            state = "SELECTED" if secid in selected else "SKIPPED"
            lines.append(
                f"- {secid}: {state}; price={item.get('price', '?')}; "
                f"trend={item.get('trend', '?')}; momentum={item.get('momentum', '?')}; "
                f"sector={instrument.get('sector', '?')}; "
                f"cluster={instrument.get('risk_cluster', '?')}"
            )
    lines.extend(("", "ВИРТУАЛЬНЫЕ BUY/SELL:"))
    if not orders:
        lines.append("- нет: текущий портфель уже соответствует целям либо сумма ниже порога")
    for item in orders:
        if not isinstance(item, Mapping) or not isinstance(item.get("intent"), Mapping):
            raise ValueError("shadow order entry must contain intent")
        intent = item["intent"]
        instrument = intent.get("instrument")
        if not isinstance(instrument, Mapping):
            raise ValueError("shadow order intent has no instrument")
        lines.append(
            f"- {str(intent.get('side', '?')).upper()} {instrument.get('secid', '?')}: "
            f"{intent.get('lots', '?')} лот.; цена {intent.get('limit_price', '?')}; "
            f"сумма {intent.get('notional', '?')} RUB; статус {item.get('status', '?')}; "
            f"sector={instrument.get('sector', '?')}; "
            f"cluster={instrument.get('risk_cluster', '?')}"
        )
    lines.extend(("", "ОТКЛОНЕНО RISK-GATE:"))
    if not rejected:
        lines.append("- нет")
    for item in rejected:
        if not isinstance(item, Mapping):
            raise ValueError("shadow rejected entry must be an object")
        reasons = item.get("reasons", [])
        reason_text = (
            ", ".join(str(value) for value in reasons)
            if isinstance(reasons, list)
            else str(reasons)
        )
        lines.append(
            f"- {str(item.get('side', '?')).upper()} {item.get('secid', '?')}: "
            f"{reason_text or 'причина не указана'}"
        )
    lines.extend(("", "SHADOW: реальные заявки брокеру не отправлялись."))
    return "\n".join(lines)


def render_flow_report(
    equity: EquityFlowSnapshot,
    futoi: FutoiSnapshot | None,
    concentration: ConcentrationSnapshot | None,
) -> str:
    total = equity.buy_value + equity.sell_value
    buy_share = Decimal("0") if total == 0 else equity.buy_value / total * 100
    lines = [
        f"Поток {equity.secid}: {equity.state.value}",
        f"Окно: {equity.window_start.isoformat()} — {equity.window_end.isoformat()}",
        f"Агрессивные покупки: {buy_share:.1f}%; imbalance: {equity.imbalance:+.3f}",
        "Это классификация исполненных сделок, а не число открытых шортов.",
    ]
    if futoi is not None:
        lines.append(f"FUTOI {futoi.ticker} на {futoi.observed_at.isoformat()}:")
        for group in futoi.groups:
            lines.append(
                f"- {group.group}: net {group.net_contracts:+f}, "
                f"long {group.gross_long:f}, short {group.gross_short:f} контрактов"
            )
    if concentration is not None:
        values = ", ".join(f"{key}={value:f}" for key, value in concentration.metrics.items())
        lines.append(f"HI2 (анонимная концентрация): {values or 'нет доступных метрик'}")
    return "\n".join(lines)


def completed_window(as_of: datetime, minutes: int) -> tuple[datetime, datetime]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    completed_minute = as_of.minute - as_of.minute % 5
    end = as_of.replace(minute=completed_minute, second=0, microsecond=0)
    if end == as_of.replace(second=0, microsecond=0):
        end -= timedelta(minutes=5)
    return end - timedelta(minutes=minutes), end


def normalized_groups(groups: Sequence[FutoiGroupSnapshot]) -> tuple[FutoiGroupSnapshot, ...]:
    return tuple(sorted(groups, key=lambda item: item.group))
