"""Produce separate, credential-free PR6 shadow reports, including missing evidence."""

import json
import os
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from intraday_markout import analyze
from pr6_observe import MSK, SESSIONS, save


def candles(ticker, day):
    if not ticker.isalnum() or not ticker.isascii():
        raise ValueError("bad ticker")
    url = (
        "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
        f"securities/{ticker}/candles.json?"
    )
    prices = {}
    start = 0
    while True:
        query = urlencode(
            {
                "from": day,
                "till": day,
                "interval": 1,
                "iss.only": "candles",
                "candles.columns": "begin,end,open,close,volume",
                "start": start,
            }
        )
        with urlopen(url + query, timeout=15) as r:
            block = json.load(r)["candles"]
        if not block["data"]:
            return prices
        for row in block["data"]:
            c = dict(zip(block["columns"], row, strict=True))
            begin = datetime.fromisoformat(c["begin"]).replace(tzinfo=MSK)
            if begin.date() == day and c["volume"] > 0:
                prices[begin] = (D(str(c["open"])), D(str(c["close"])))
        start += len(block["data"])


def move(prices, ready, minutes, now):
    # First whole minute strictly after the plan. No backfill or carrying stale prices.
    entry = ready.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = (
        entry + timedelta(minutes=minutes)
        if minutes
        else datetime.combine(ready.date(), time(18, 45), MSK)
    )
    if end <= entry or end.date() != entry.date():
        return {"status": "вне окна"}
    if end + timedelta(minutes=1) > now:
        return {"status": "ожидается"}
    if entry not in prices or end not in prices:
        return {"status": "нет точной свечи"}
    a, b = prices[entry][0], prices[end][0]
    return {
        "status": "готово",
        "from": entry,
        "to": end,
        "first": a,
        "last": b,
        "pct": 100 * (b / a - 1),
    }


def read(path):
    return json.loads(path.read_bytes())


def report(root, profile, now):
    lines = [
        f"# PR6 · {profile} · SHADOW",
        "",
        f"Срез: {now:%d.%m.%Y %H:%M} МСК.",
        "Отдельный sandbox-счёт. Заявки не отправляются и не отменяются. "
        "Изменение цены — не исполнение лимита, не P&L и не доходность счёта.",
        "",
    ]
    pre = sorted((root / "precheck" / profile).glob("cycle-*.json"))
    if pre:
        check = read(pre[-1])
        lines.append(
            f"Проверка до окна: {check['stage']}; код {check.get('code', 2)}; "
            f"капитал {check.get('equity', 'н/д')} ₽; позиций {check.get('positions', 'н/д')}."
        )
    for day in SESSIONS:
        part = root / day.isoformat() / profile
        cycles = [read(p) for p in sorted(part.glob("cycle-*.json"))]
        ready = [c for c in cycles if c.get("plan")]
        expected = 9 if profile == "long" else 108
        lines.extend(
            [
                "",
                f"## {day:%d.%m.%Y}",
                "",
                f"Циклов с результатом: {len(cycles)} из {expected} по расписанию; "
                f"планов-артефактов: {len(ready)}; "
                f"ошибки/блокировки: {sum(c.get('code') != 0 for c in cycles)}.",
            ]
        )
        if not ready:
            lines.append("Нет плана для сверки. Это не нулевой результат стратегии.")
            continue
        plans = [read(part / c["plan"]) for c in ready]
        lines.append(
            f"Качество прошло: {sum(p['quality']['passed'] for p in plans)}/{len(plans)}; "
            f"намерений выставить заявку: {sum(len(p['orders']) for p in plans)}."
        )
        if profile == "intraday":
            m = analyze(part, fee_bps=D(5), latency=1)
            save(part / "markout.json", m)
            lines.extend(
                [
                    f"Решения по бумагам: {m['counts']}. Отказы: {m['rejection_reasons']}.",
                    "Последующие 5/15/30/60 мин: цена закрытия реального 5-минутного бара "
                    "и спред OBStats как приближение bid/ask; задержка ≥1 с, "
                    "комиссия 5 б.п. на сторону. Доходность гипотетическая.",
                    "",
                    "| Решение / минут | Оценено | Нет пары цен | Среднее net, б.п. |",
                    "|---|---:|---:|---:|",
                ]
            )
            for group, v in m["summary"].items():
                mean = v["mean_net_bps"]
                lines.append(
                    f"| {group} | {v['n']} | {v['missing']} | "
                    + (f"{mean:.2f}" if mean is not None else "—")
                    + " |"
                )
            if not m["summary"]:
                lines.append("Нет направленных наблюдений с измеримым горизонтом.")
            if any(p["orders"] for p in plans):
                lines.extend(
                    [
                        "",
                        "Планы (не сделки), движение после решения в б.п. после расходов:",
                        "",
                        "| МСК | Бумага | Сторона / лоты | Лимит | 5 / 15 / 30 / 60 мин |",
                        "|---|---|---|---:|---|",
                    ]
                )
                by_order = {
                    (r["run_id"], r["order_request_id"]): r
                    for r in m["observations"]
                    if r.get("order_request_id")
                }
                for p in plans:
                    t = datetime.fromisoformat(p["analysis_input"]["decision_ready_at"]).astimezone(
                        MSK
                    )
                    for record in p["orders"]:
                        it = record["intent"]
                        obs = by_order.get((p["run_id"], it["order_request_id"]), {})
                        metrics = []
                        for horizon in (5, 15, 30, 60):
                            value = obs.get("horizons", {}).get(str(horizon), {}).get("net_bps")
                            metrics.append(
                                f"{value:+.1f}"
                                if value is not None
                                else (
                                    "ожидается" if now < t + timedelta(minutes=horizon) else "н/д"
                                )
                            )
                        lines.append(
                            f"| {t:%H:%M:%S} | {it['instrument']['secid']} | "
                            f"{it['side']} / {it['lots']} | {it['limit_price']} | "
                            + " / ".join(metrics)
                            + " |"
                        )
            continue
        details = []
        prices = {}
        for cycle, plan in zip(ready, plans, strict=True):
            instant = datetime.fromisoformat(cycle["finished_at"]).astimezone(MSK)
            if not plan["rebalance_allowed"]:
                continue
            lines.append(
                f"Ребалансировка {instant:%H:%M}: целей {len(plan['targets'])}, "
                f"планов заявок {len(plan['orders'])}, отказов {len(plan['rejected'])}; "
                f"геориск: {plan['geo']['level']}."
            )
            intents = [
                (
                    o["intent"]["instrument"]["secid"],
                    o["intent"]["side"],
                    o["intent"]["lots"],
                    o["intent"]["limit_price"],
                )
                for o in plan["orders"]
            ]
            for s in sorted({t["secid"] for t in plan["targets"]} | {v[0] for v in intents}):
                if s not in prices:
                    try:
                        prices[s] = candles(s, day)
                    except (OSError, ValueError, KeyError):
                        prices[s] = {}
                values = [move(prices[s], instant, h, now) for h in (60, 0)]
                desc = (
                    ", ".join(
                        f"{side} {n} лот. @ {lim}" for tick, side, n, lim in intents if tick == s
                    )
                    or "только цель"
                )
                formatted = [f"{v['pct']:+.2f}%" if "pct" in v else v["status"] for v in values]
                lines.append(f"- {s} · {desc}: +60 мин {formatted[0]}; 18:45 МСК {formatted[1]}.")
                details.append(dict(secid=s, plan=desc, ready=instant, windows=values))
        save(part / "price-comparison.json", details)
        lines.append(
            "Источник: минутные сделки MOEX ISS, TQBR. Изменение цены бумаги "
            "от открытия первой полной минуты после готовности плана; без издержек."
        )
    text = "\n".join(lines) + "\n"
    path = root / f"{profile}-report.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    root = Path(os.environ["STATE_DIRECTORY"])
    now = datetime.now(UTC).astimezone(MSK)
    for profile in ("long", "intraday"):
        report(root, profile, now)
