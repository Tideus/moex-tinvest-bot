"""Autonomous research-only scheduler. No account, order or trading imports."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

import universe_capture as collector
import universe_history as history
import universe_watch as watch

MSK = watch.MSK
PROTOCOL = "universe24-server-shadow-v1"


class SourceError(RuntimeError):
    def __init__(self, source, stage, reason):
        self.source, self.stage, self.reason = source, stage, reason
        super().__init__(f"{source}/{stage}/{reason}")


def now():
    return datetime.now(UTC)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, default=str, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def slots(day):
    start = datetime.combine(day, time(10, 15), MSK)
    return [start + timedelta(minutes=5 * n) for n in range(98)]


def table(raw, name):
    block = raw[name]
    keys = [k.lower() for k in block["columns"]]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate_calendar_column")
    return [dict(zip(keys, row, strict=True)) for row in block["data"]]


def calendar_status(raw, day):
    ti = [
        row
        for exchange in raw["tinvest"]["payload"]["exchanges"]
        if exchange["exchange"] == "MOEX"
        for row in exchange["days"]
        if datetime.fromisoformat(row["date"].replace("Z", "+00:00")).date() == day
    ]
    rows = [
        row
        for row in table(raw["moex"]["payload"], "session_schedule")
        if row["tradedate"] == day.isoformat()
        and row["boardid"] == "TQBR"
        and row["tradingsession"] == 1
        and row["type"] == "system"
        and not row.get("secid")
    ]
    if len(ti) != 1 or type(ti[0].get("isTradingDay")) is not bool:
        raise ValueError("tinvest_calendar_missing_or_ambiguous")
    if not ti[0]["isTradingDay"]:
        if rows:
            raise ValueError("calendars_disagree")
        return "CLOSED"
    if len(rows) != 1:
        raise ValueError("moex_calendar_missing_or_ambiguous")
    start, end = [
        datetime.combine(day, time.fromisoformat(rows[0][key]), MSK)
        for key in ("time_from", "time_till")
    ]
    if start != watch.timestamp(ti[0]["startTime"]) or end != watch.timestamp(ti[0]["endTime"]):
        raise ValueError("calendar_session_times_disagree")
    if start > slots(day)[0] or end < slots(day)[-1] + timedelta(minutes=5):
        return "SHORT_SESSION_EXCLUDED"
    return "OPEN"


def safe_error(source, stage, exc):
    return {
        "source": getattr(exc, "source", source),
        "stage": getattr(exc, "stage", stage),
        "error": getattr(exc, "reason", None)
        if isinstance(exc, SourceError)
        else (f"HTTP_{exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__),
    }


def calendar_fetch(day):
    day_start = datetime.combine(day, time(), MSK).astimezone(UTC)
    requests = {
        "tinvest": Request(
            collector.BASE + "InstrumentsService/TradingSchedules",
            data=json.dumps(
                {
                    "exchange": "MOEX",
                    "from": day_start.isoformat(),
                    "to": (day_start + timedelta(days=1)).isoformat(),
                }
            ).encode(),
            headers={
                "Authorization": "Bearer " + os.environ["T_INVEST_READONLY_TOKEN"],
                "Content-Type": "application/json",
            },
        ),
        "moex": Request(
            "https://apim.moex.com/iss/calendars/stock/session.json?"
            + urlencode({"date": day.isoformat(), "iss.meta": "off"}),
            headers={"Authorization": "Bearer " + os.environ["MOEX_APIKEY"]},
        ),
    }
    result = {}
    for source, request in requests.items():
        started = now()
        try:
            with build_opener(collector.NoRedirect()).open(request, timeout=15) as response:
                payload = json.load(response)
        except Exception as exc:
            reason = f"HTTP_{exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            raise SourceError(source, "official_calendar", reason) from None
        result[source] = {
            "request_started_at": started.isoformat(),
            "received_at": now().isoformat(),
            "payload": payload,
        }
    calendar_status(result, day)
    return result


def prepare(state, cfg, day):
    """Immutable day-specific warmup; never mutate an earlier capture's inputs."""
    data = state / "data" / day.isoformat()
    data.mkdir(parents=True, exist_ok=True)
    results, errors = [], []

    def fetch(symbol):
        marker = data / (symbol + ".ready.json")
        if marker.exists():
            record = json.loads(marker.read_bytes())
            csv = data / f"{symbol}_TQBR_24.csv"
            if hashlib.sha256(csv.read_bytes()).hexdigest() != record["csv_sha256"]:
                raise ValueError("warmup_hash_mismatch")
            return record
        result = history.candles(
            symbol, "TQBR", 24, day - timedelta(days=90), day - timedelta(days=1), data
        )
        csv = data / f"{symbol}_TQBR_24.csv"
        result["csv_sha256"] = hashlib.sha256(csv.read_bytes()).hexdigest()
        result["received_at"] = now().isoformat()
        watch.write_result(marker, result)
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch, symbol): symbol for symbol in cfg["symbols"]}
        for task in as_completed(futures):
            try:
                results.append(task.result())
            except Exception as exc:
                errors.append({"secid": futures[task], **safe_error("MOEX_ISS", "warmup", exc)})
    result = {"date": day.isoformat(), "ready": len(results), "errors": errors}
    atomic(data / "health.json", result)
    return result


def latest_calendar(state, day, at):
    path = state / "calendar" / f"{day}.json"
    if not path.exists():
        watch.write_result(path, calendar_fetch(day))
        at = now()
    raw = json.loads(path.read_bytes())
    for value in raw.values():
        if (
            not watch.timestamp(value["request_started_at"])
            <= watch.timestamp(value["received_at"])
            <= at
        ):
            raise ValueError("calendar_not_available_at_observation")
    return calendar_status(raw, day)


def capture_slot(state, bundle, cfg, at, calendar=latest_calendar, capture=collector.capture):
    at = at.astimezone(MSK)
    day = at.date()
    slot = at.replace(minute=at.minute // 5 * 5, second=0, microsecond=0)
    if slot not in slots(day) or (at - slot).total_seconds() > 30:
        return {"status": "OUTSIDE_SLOT", "execution_allowed": False}
    folder = state / "captures" / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    key = slot.strftime("%Y%m%dT%H%M")
    if (folder / ("attempt-" + key + ".json")).exists():
        return {"status": "ALREADY_ATTEMPTED", "execution_allowed": False}
    # Reserve before network: restarts cannot duplicate or backfill a decision.
    watch.write_result(folder / ("attempt-" + key + ".json"), {"at": at.isoformat()})
    try:
        status = calendar(state, day, at)
        if status != "OPEN":
            return {"status": status, "execution_allowed": False}
        data = state / "data" / day.isoformat()
        universe = json.loads((bundle / "universe.json").read_bytes())
        reader = collector.Reader(os.environ.get("T_INVEST_READONLY_TOKEN", ""))
        result = capture(universe, cfg, data, reader)
        started = watch.timestamp(result["started_at"])
        if not slot <= started <= slot + timedelta(seconds=30):
            raise ValueError("capture_started_after_slot_deadline")
        result["protocol"] = PROTOCOL
        result["execution_allowed"] = False
        result["calendar_sha256"] = hashlib.sha256(
            (state / "calendar" / f"{day}.json").read_bytes()
        ).hexdigest()
        watch.write_result(folder / ("capture-" + key + ".json"), result)
        return {
            "status": "CAPTURED",
            "execution_allowed": False,
            "cohorts": result["screen"]["cohorts"],
            "errors": result["snapshot"]["errors"],
        }
    except Exception as exc:
        error = safe_error("universe24", "capture_slot", exc)
        watch.write_result(folder / ("error-" + key + ".json"), error)
        return {"status": "FAILED", "execution_allowed": False, "error": error}


def report(state, cfg, day, at):
    if at.astimezone(MSK) < datetime.combine(day, time(18, 25), MSK):
        raise ValueError("observation_window_not_closed")
    directory = state / "captures" / day.isoformat()
    receipt = watch.session_receipt(directory, day, cfg, as_of=at)
    calendar_path = state / "calendar" / f"{day}.json"
    calendar = (
        calendar_status(json.loads(calendar_path.read_bytes()), day)
        if calendar_path.exists()
        else "UNKNOWN"
    )
    grouped = {
        name: {
            "observations": 0,
            "received": 0,
            "eligible": 0,
            "quote_valid": 0,
            "reasons": Counter(),
        }
        for name in watch.cohorts(cfg)
    }
    for name in sorted(receipt["capture_sha256"]):
        path = directory / name
        raw = json.loads(path.read_bytes())
        for name, values in raw["screen"]["cohorts"].items():
            for key in ("received", "eligible", "quote_valid"):
                grouped[name][key] += values[key]
            grouped[name]["observations"] += values["expected"]
            grouped[name]["reasons"].update(values["rejections"])
    for name, symbols in watch.cohorts(cfg).items():
        grouped[name]["expected_observations"] = 98 * len(symbols)
        grouped[name]["quote_coverage"] = watch.number(grouped[name]["quote_valid"]) / (
            98 * len(symbols)
        )
    result = {
        "protocol": PROTOCOL,
        "execution_allowed": False,
        "profitability_status": "NOT_ESTABLISHED",
        "promotion_to_trading": False,
        "as_of": at.isoformat(),
        "calendar": calendar,
        "receipt": receipt,
        "cohorts": grouped,
        "meaning": "Liquidity observations, not signals, executions or portfolio P&L",
    }
    out = state / "reports" / day.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    # Multiple report revisions are explicit, while source captures stay immutable.
    key = at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    watch.write_result(out / f"report-{key}.json", result)
    atomic(out / "latest.json", result)
    receipts = []
    for path in sorted((state / "reports").glob("*/latest.json")):
        row = json.loads(path.read_bytes())
        if row["calendar"] == "OPEN":
            receipts.append(row["receipt"])
    weekly = watch.weekly(receipts, cfg, as_of=at)
    atomic(state / "reports" / "weekly-latest.json", weekly)
    lines = [
        f"# Shadow 24 — {day}",
        "",
        "Только наблюдение; торговых заявок нет.",
        f"Календарь: {calendar}. Снимков: {receipt['observed_snapshots']}/98.",
        "",
        "| Группа | Получено | Валидные котировки | Прошли фильтр |",
        "|---|---:|---:|---:|",
    ]
    for name, row in grouped.items():
        lines.append(f"| {name} | {row['received']} | {row['quote_valid']} | {row['eligible']} |")
    lines += [
        "",
        "Повторные наблюдения не являются независимыми сделками.",
        "Фильтр ликвидности не является торговым сигналом. Прибыльность не установлена.",
    ]
    for name, row in grouped.items():
        lines += ["", f"## Причины отказа: {name}", json.dumps(row["reasons"], ensure_ascii=False)]
    (out / f"report-{key}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def validate_bundle(bundle):
    for line in (bundle / "BUNDLE_MANIFEST.sha256").read_text().splitlines():
        checksum, name = line.split("  ", 1)
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_file():
            raise ValueError("unsafe_bundle_path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError("bundle_hash_mismatch")
    cfg = watch.policy(bundle / "universe24.json")
    if cfg["control_symbols"] != ["SBER", "ROSN", "CHMF"] or len(cfg["symbols"]) != 24:
        raise ValueError("unexpected_control_or_universe")
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "prepare", "report", "probe"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    cfg = validate_bundle(args.bundle)
    state = args.state
    state.mkdir(parents=True, exist_ok=True)
    if any(k.startswith("T_INVEST_") and k != "T_INVEST_READONLY_TOKEN" for k in os.environ):
        raise ValueError("trading_credentials_forbidden")
    if args.command == "report":
        at = now()
        day = at.astimezone(MSK).date()
        if at.astimezone(MSK).time() < time(18, 25):
            day -= timedelta(days=1)
        report(state, cfg, day, at)
        print("PASS: cohort report written; no trading actions")
        return
    if args.command in {"prepare", "probe"}:
        day = now().astimezone(MSK).date()
        latest_calendar(state, day, now())
        result = prepare(state, cfg, day)
        if args.command == "probe":
            result = collector.capture(
                json.loads((args.bundle / "universe.json").read_bytes()),
                cfg,
                state / "data" / str(day),
                collector.Reader(os.environ["T_INVEST_READONLY_TOKEN"]),
            )
            watch.write_result(
                state / "commissioning" / (now().strftime("%Y%m%dT%H%M%SZ") + ".json"), result
            )
            print(
                json.dumps(
                    {
                        "cohorts": result["screen"]["cohorts"],
                        "errors": result["snapshot"]["errors"],
                    },
                    default=str,
                )
            )
        else:
            print(json.dumps(result))
        return
    prepared = None
    last_observation = None
    next_prepare = now()
    while True:
        at = now()
        day = at.astimezone(MSK).date()
        local_time = at.astimezone(MSK).time()
        try:
            if shutil.disk_usage(state).free < 1024**3:
                raise OSError("research_disk_floor")
            if time(9, 35) <= local_time < time(18, 25):
                opened = latest_calendar(state, day, now())
                if opened == "OPEN" and prepared != day and at >= next_prepare:
                    next_prepare = at + timedelta(minutes=15)
                    warmup = prepare(state, cfg, day)
                    if not warmup["errors"]:
                        prepared = day
                result = capture_slot(state, args.bundle, cfg, now())
                if result["status"] in {"CAPTURED", "FAILED"}:
                    last_observation = {"at": now().isoformat(), **result}
            else:
                result = {"status": "OUTSIDE_WINDOW", "execution_allowed": False}
            atomic(
                state / "health.json",
                {
                    "at": now().isoformat(),
                    "protocol": PROTOCOL,
                    "last_observation": last_observation,
                    **result,
                },
            )
        except Exception as exc:
            error = safe_error("universe24", "scheduler", exc)
            atomic(
                state / "health.json",
                {
                    "at": now().isoformat(),
                    "status": "FAILED",
                    "execution_allowed": False,
                    "error": error,
                },
            )
            print(json.dumps(error), flush=True)
        sleep(5)


if __name__ == "__main__":
    main()
