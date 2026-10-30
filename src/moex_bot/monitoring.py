"""Bounded read-only probes and persistent incident/recovery notifications."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .notifications import SQLiteOutbox


def load_settings(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for key in ("disk_warn_pct", "disk_critical_pct", "disk_min_free_gib", "failures_to_alert",
                "timeout_seconds", "repeat_hours", "intraday_max_age_seconds",
                "long_max_age_seconds", "backup_max_age_hours"):
        if isinstance(raw.get(key), bool) or not isinstance(raw.get(key), (int, float)):
            raise ValueError(f"invalid monitoring setting: {key}")
        if raw[key] <= 0:
            raise ValueError(f"nonpositive monitoring setting: {key}")
    if not 0 < raw["disk_critical_pct"] < raw["disk_warn_pct"] < 100:
        raise ValueError("disk thresholds must satisfy critical < warning < 100")
    if not isinstance(raw.get("calendar_required"), bool):
        raise ValueError("calendar_required must be boolean")
    envs = raw.get("required_tinvest_environments")
    if not isinstance(envs, list) or any(env not in {"sandbox", "prod"} for env in envs):
        raise ValueError("invalid required_tinvest_environments")
    return raw


def disk_status(total: int, free: int, settings: dict[str, Any]) -> str:
    pct = 100 * free / total if total else 0
    if pct < settings["disk_critical_pct"] or free < settings["disk_min_free_gib"] * 2**30:
        return f"CRITICAL: free={free / 2**30:.2f} GiB ({pct:.1f}%)"
    if pct < settings["disk_warn_pct"]:
        return f"WARNING: free={free / 2**30:.2f} GiB ({pct:.1f}%)"
    return "OK"


def request_probe(url: str, *, token: str = "", payload: dict[str, object] | None = None,
                  timeout: float = 10, block: str = "", xml: bool = False) -> str:
    headers = {"Content-Type": "application/json", "User-Agent": "moex-tinvest-shadow/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers,
                      data=None if payload is None else json.dumps(payload).encode())
    started = monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            data = response.read(4 * 1024 * 1024)
        if xml:
            root = ElementTree.fromstring(data)
            return "OK" if root.tag.lower() in {"rss", "feed"} else "FAIL: RSS schema"
        raw = json.loads(data)
        if not isinstance(raw, dict) or (block and block not in raw):
            return "FAIL: response schema"
        if raw.get("ok") is False:
            return "FAIL: API rejected request"
        return "OK"
    except HTTPError as exc:
        return f"FAIL: HTTP {exc.code}"
    except Exception:
        # Never include URL/exception: Telegram URL contains a secret.
        return f"FAIL: transport/schema ({monotonic() - started:.1f}s)"


def systemd(unit: str, property_name: str) -> str:
    try:
        result = subprocess.run(["systemctl", "show", unit, f"--property={property_name}",
                                 "--value"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode:
        return "unavailable"
    return result.stdout.strip()


def collect(app: Path, state: Path, settings: dict[str, Any], now: datetime) -> dict[str, str]:
    checks: dict[str, str] = {}
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0])
    total_memory = int(memory["MemTotal"].split()[0])
    checks["memory:available"] = (
        "OK" if available / total_memory >= .05 else "WARNING: available memory <5%")
    for path in ("/", "/var/lib/moex-tinvest-bot", "/var/log/moex-tinvest-bot"):
        usage = shutil.disk_usage(path)
        checks[f"disk:{path}"] = disk_status(usage.total, usage.free, settings)
        stats = os.statvfs(path)  # type: ignore[attr-defined]  # Ubuntu-only collector
        inode_pct = 100 * stats.f_favail / stats.f_files if stats.f_files else 100
        checks[f"inodes:{path}"] = "OK" if inode_pct >= 5 else f"CRITICAL: {inode_pct:.1f}% free"
    units = ["shadow", "intraday", "health", "daily-report", "maintenance",
             "prod-long-observer"]
    if settings["calendar_required"]:
        units.append("calendar")
    for name in units:
        unit = f"moex-tinvest-{name}"
        active = systemd(unit + ".timer", "ActiveState")
        enabled = systemd(unit + ".timer", "UnitFileState")
        following = systemd(unit + ".timer", "NextElapseUSecRealtime")
        monotonic_next = systemd(unit + ".timer", "NextElapseUSecMonotonic")
        checks[f"timer:{name}"] = (
            "OK" if active == "active" and enabled in {"enabled", "enabled-runtime"} and any(
                value not in {"", "0", "n/a", "unavailable"}
                for value in (following, monotonic_next)) else f"FAIL: active={active}, no schedule"
        )
        if name != "health":
            result = systemd(unit + ".service", "Result")
            checks[f"service:{name}"] = "OK" if result == "success" else f"FAIL: result={result}"
        if name != "health" and systemd(unit + ".service", "ActiveState") == "activating":
            entered = systemd(unit + ".service", "StateChangeTimestampMonotonic")
            try:
                uptime = float(Path("/proc/uptime").read_text().split()[0])
                if uptime - int(entered) / 1_000_000 > 600:
                    checks[f"service:{name}"] = "FAIL: stuck activating >600s"
            except (ValueError, OSError):
                checks[f"service:{name}"] = "FAIL: cannot determine run duration"
    services = json.loads((app / "config/services.json").read_text())
    timeout = settings["timeout_seconds"]
    checks["api:moex_iss"] = request_probe(
        services["moex"]["iss"] + "/engines/stock/markets/shares/boards/TQBR/securities.json"
        "?iss.only=securities&securities=SBER&iss.meta=off", timeout=timeout, block="securities")
    token = os.environ.get("MOEX_APIKEY", "")
    for dataset in ("tradestats", "orderstats", "obstats"):
        url = services["moex"]["algopack"] + f"/datashop/algopack/eq/{dataset}.json?" + urlencode({
            "date": now.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat(),
            "latest": 1, "limit": 1, "iss.meta": "off"})
        checks[f"api:algopack_{dataset}"] = request_probe(
            url, token=token, timeout=timeout, block="data") if token else "FAIL: missing token"
    for env in ("sandbox", "prod"):
        if env not in settings["required_tinvest_environments"]:
            continue
        token = os.environ.get(f"T_INVEST_{env.upper()}_TOKEN", "")
        if not token:
            checks[f"api:tinvest_{env}"] = "FAIL: missing token"
            continue
        method = ("SandboxService/GetSandboxAccounts" if env == "sandbox"
                  else "UsersService/GetAccounts")
        checks[f"api:tinvest_{env}"] = request_probe(
            services["t_invest"][env + "_rest"]
            + "/tinkoff.public.invest.api.contract.v1." + method,
            token=token, payload={}, timeout=timeout)
    token = os.environ.get("T_INVEST_READONLY_TOKEN", "")
    if "calendar" in units:
        checks["api:calendar"] = request_probe(
            services["t_invest"]["prod_rest"]
            + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/TradingSchedules",
            token=token, payload={}, timeout=timeout) if token else "FAIL: missing token"
        from .event_calendar import EventStore

        try:
            checks["data:calendar"] = (
                "FAIL: stale/incomplete" if "INCOMPLETE/STALE" in EventStore(
                    state / "data/events.sqlite3").status(now) else "OK")
        except Exception:
            checks["data:calendar"] = "FAIL: storage/read error"
    telegram = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    for feed in json.loads((app / "config/geo_sources.json").read_text()):
        checks["api:geo_" + feed["source_id"]] = request_probe(
            feed["url"], timeout=timeout, xml=True)
    checks["api:telegram_chat"] = request_probe(
        services["telegram"]["api"] + f"/bot{telegram}/getChat",
        payload={"chat_id": os.environ.get("TELEGRAM_CHAT_ID", "")}, timeout=timeout,
        block="ok") if telegram else "FAIL: missing token"
    alerts_chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if alerts_chat:
        checks["api:telegram_alert_chat"] = request_probe(
            services["telegram"]["api"] + f"/bot{telegram}/getChat",
            payload={"chat_id": alerts_chat}, timeout=timeout, block="ok"
        ) if telegram else "FAIL: missing token"
    try:
        outbox = SQLiteOutbox(state / "data/notifications.sqlite3").health(now=now)
        checks["telegram:outbox"] = (
            "OK" if outbox["dead"] == 0 and outbox["pending_due"] <= 20 else f"FAIL: {outbox}")
    except Exception:
        checks["telegram:outbox"] = "FAIL: storage/read error"
    local = now.astimezone(ZoneInfo("Europe/Moscow"))
    if local.weekday() < 5 and 625 <= local.hour * 60 + local.minute < 1140:
        limits = (("intraday", "intraday-plan-", settings["intraday_max_age_seconds"]),
                  ("long", "shadow-", settings["long_max_age_seconds"]))
        for kind, prefix, limit in limits:
            files = list((state / "artifacts").glob(prefix + "*.json"))
            age = now.timestamp() - max((p.stat().st_mtime for p in files), default=0)
            checks[f"data:{kind}"] = "OK" if 0 <= age <= limit else f"FAIL: age={age:.0f}s"
            if files:
                try:
                    latest_plan = max(files, key=lambda path: path.stat().st_mtime)
                    quality = json.loads(latest_plan.read_text()).get("quality", {})
                    checks[f"quality:{kind}"] = (
                        "OK" if quality.get("passed") is True else "FAIL: quality gate blocked")
                except Exception:
                    checks[f"quality:{kind}"] = "FAIL: artifact read/schema error"
    portfolios = list((state / "artifacts").glob("intraday-portfolio-*.json"))
    if portfolios and local.hour * 60 + local.minute >= 1120:
        latest = max(portfolios, key=lambda path: path.stat().st_mtime)
        try:
            payload = json.loads(latest.read_text())
            positions = payload.get("positions", {})
            checks["intraday:overnight_exposure"] = (
                "FAIL: last broker snapshot has positions after 18:40 MSK"
                if positions else "OK")
        except Exception:
            checks["intraday:overnight_exposure"] = "FAIL: broker snapshot read error"
    backups = list(Path("/var/backups/moex-tinvest-bot").glob("moex-tinvest-*.tar.gz"))
    age = now.timestamp() - max((p.stat().st_mtime for p in backups), default=0)
    checks["backup:age"] = "OK" if age <= settings["backup_max_age_hours"] * 3600 else "FAIL: stale"
    return checks


def notify(checks: dict[str, str], database: Path, outbox: SQLiteOutbox,
           now: datetime, settings: dict[str, Any]) -> int:
    with closing(sqlite3.connect(database, timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS incidents "
                   "(key TEXT PRIMARY KEY, failures INTEGER, alerted INTEGER, last_sent REAL)")
        for key, value in checks.items():
            row = db.execute("SELECT failures,alerted,last_sent FROM incidents WHERE key=?",
                             (key,)).fetchone() or (0, 0, 0)
            failures, alerted, sent = row
            failed = value != "OK"
            failures = failures + 1 if failed else 0
            if value.startswith("CRITICAL"):
                failures = max(failures, settings["failures_to_alert"])
            if ((failed and failures >= settings["failures_to_alert"] and
                 (not alerted or now.timestamp() - sent >= settings["repeat_hours"] * 3600))
                    or (not failed and alerted)):
                body = f"{'🚨 СБОЙ' if failed else '✅ ВОССТАНОВЛЕНО'} · MOEX BOT\n{key}\n{value}"
                outbox.enqueue(kind="monitoring", dedupe_key=f"monitor:{key}:{now.isoformat()}",
                               body=body, now=now)
                sent, alerted = now.timestamp(), int(failed)
            db.execute("INSERT OR REPLACE INTO incidents VALUES(?,?,?,?)",
                       (key, failures, alerted, sent))
    return sum(value != "OK" for value in checks.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=Path("/opt/moex-tinvest-bot"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/moex-tinvest-bot"))
    parser.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()
    now = datetime.now(UTC)
    if args.test_alert:
        SQLiteOutbox(args.state / "data/notifications.sqlite3").enqueue(
            kind="monitoring_test", dedupe_key=f"monitor-test:{now.isoformat()}",
            body="🧪 ТЕСТ МОНИТОРИНГА · MOEX BOT\nПроверка канала алертов. Это не авария.", now=now)
        return 0
    settings = load_settings(args.app / "config/monitoring.json")
    try:
        checks = collect(args.app, args.state, settings, now)
    except Exception as exc:
        checks = {"monitor:collector": f"FAIL: {type(exc).__name__}; inspect configuration/storage"}
    outbox = SQLiteOutbox(args.state / "data/notifications.sqlite3")
    failures = notify(checks, args.state / "data/monitoring.sqlite3", outbox, now, settings)
    output = args.state / "artifacts/monitoring-latest.json"
    output.write_text(json.dumps({"observed_at": now.isoformat(), "checks": checks},
                                 ensure_ascii=False, indent=2))
    for key, value in checks.items():
        print(f"{key}: {value}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
