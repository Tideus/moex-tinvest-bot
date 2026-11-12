"""Non-secret, stable labels shared by monitoring and the Kuma adapter."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# Keys remain stable for existing incident databases; labels are presentation only.
API_LABELS = {
    "api:news_iss_observer": ("MOEX ISS · новости · ТОЛЬКО НАБЛЮДЕНИЕ", "iss.moex.com"),
    "api:geo_moex_risk": ("MOEX RSS · риск-параметры · текущий геоисточник", "www.moex.com"),
    "api:tinvest_sandbox": ("T-Invest · SANDBOX", "sandbox-invest-public-api.tbank.ru"),
    "api:tinvest_prod": ("T-Invest · PROD", "invest-public-api.tbank.ru"),
    "api:moex_iss": ("MOEX ISS · публичные рыночные данные", "iss.moex.com"),
    "api:algopack_tradestats": ("ALGOPACK · TradeStats", "apim.moex.com"),
    "api:algopack_orderstats": ("ALGOPACK · OrderStats", "apim.moex.com"),
    "api:algopack_obstats": ("ALGOPACK · OBStats", "apim.moex.com"),
    "api:calendar": ("T-Invest · PROD · календарь инвестора (read-only)",
                     "invest-public-api.tbank.ru"),
    "api:telegram_chat": ("Telegram · торговые отчёты", "api.telegram.org"),
    "api:telegram_alert_chat": ("Telegram · инфраструктурные алерты", "api.telegram.org"),
}


def check_description(key: str) -> str:
    if key == "backup:age":
        return ("MOEX BOT / Резервирование состояния\n"
                "Полная копия БД и истории, не отчёт и не sidecar")
    if key in API_LABELS:
        title, host = API_LABELS[key]
        return f"MOEX BOT / {title}\nЦель: {host}"
    if key.startswith(("disk:", "inodes:", "memory:", "collector:resources")):
        return f"SERVER / Ресурсы виртуальной машины\nМетрика: {key}"
    if key.startswith("api:geo_"):
        return f"MOEX BOT / Геориск · публичная новостная лента\nИсточник: {key[8:]}"
    if key.startswith(("timer:", "service:")):
        return f"MOEX BOT / Планировщик и службы\nКомпонент: {key}"
    return f"MOEX BOT / Данные и диагностика\nМетрика: {key}"


def check_action(key: str) -> str:
    if key == "backup:age":
        return ("Проверить moex-tinvest-backup.service и maintenance.timer; "
                "запустить backup.sh, проверить SHA и восстановление в изолированной папке.")
    if key == "api:news_iss_observer":
        return "Проверить moex-news-observer; ISS не заменяет RSS и не влияет на сделки."
    if key.startswith("api:tinvest_"):
        environment = "SANDBOX" if key.endswith("sandbox") else "PROD"
        return f"Проверить сеть, сертификаты и токен именно {environment}; не повторять заявки."
    if key.startswith("api:"):
        return "Проверить доступность источника, HTTP-статус и права API; секреты не публиковать."
    if key.startswith(("disk:", "inodes:", "memory:")):
        return "Проверить df -h, df -i и free -h; не удалять торговый журнал исполнения."
    if key.startswith(("timer:", "service:")):
        return "Проверить systemctl и journalctl; учитывать согласованную остановку бота."
    return "Запустить sudo moex-botctl diagnose и проверить соответствующий источник/артефакт."


def monitor_description(name: str) -> str:
    """Never echo webhook descriptions, URLs or broker responses."""
    lowered = name.lower()
    if "resources" in lowered or "ресурсы" in lowered or "vm disk" in lowered:
        return "SERVER / Ресурсы ВМ: свободное место, inode и доступная RAM. Не торговый API."
    if "sync" in lowered:
        return "INFRA / Syncthing: синхронизация файлов. Не компонент торговой модели."
    if "minio" in lowered or "s3" in lowered:
        return "INFRA / MinIO S3: объектное хранилище. Не компонент торговой модели."
    for key, (title, host) in API_LABELS.items():
        if key in name:
            return f"MOEX BOT / {title}\nЦель: {host}\nДействие: {check_action(key)}"
    if "api integrations" in lowered or "all api sources" in lowered:
        return "MOEX BOT / API (legacy aggregate). Точный источник — в diagnose, строки api:*."
    if "trading data" in lowered or "data freshness" in lowered:
        return ("MOEX BOT / Торговые данные: свежесть long/intraday/observer, календарь, "
                "качество артефактов, резервные копии и очередь отчётов. См. diagnose.")
    if "timer" in lowered:
        return "MOEX BOT / Таймеры и службы: systemd; согласованная остановка также даёт DOWN."
    if "heartbeat" in lowered:
        return "MONITORING / Observer heartbeat: наблюдатель завершает цикл и доставляет Push."
    return "MONITORING / Проверка Kuma; подробности и история доступны в dashboard."


UNITS = {"shadow", "intraday", "health", "daily-report", "maintenance", "prod-long-observer",
         "news-observer",
         "calendar", "long-once-sandbox", "long-once-prod", "long-sandbox-verifier",
         "legacy-long-excluded"}
INACTIVE = "FAIL: active=inactive, no schedule"


def safe_reason(value: str) -> str:
    """Only known templates; never echo arbitrary collector errors to Kuma or Telegram."""
    if value == "FAIL: backup missing":
        return "нет завершённой резервной копии состояния с SHA256"
    if value == "FAIL: backup stale":
        return "резервная копия состояния просрочена"
    if value == "FAIL: news observer stale or missing":
        return "ISS-наблюдение: нет свежего отчёта; состояние источника неизвестно"
    if value == "FAIL: news observer timer inactive":
        return "таймер наблюдения новостей ISS выключен"
    news = re.fullmatch(
        r"FAIL: news observer (timeout|dns|tls|connection|schema|http|unknown)", value)
    if news:
        return "ISS-наблюдение новостей: " + news[1] + "; торговые решения не меняются"
    probe = re.fullmatch(
        r"FAIL: probe (timeout|dns|tls|schema|connection|unknown) \(([0-9]+\.[0-9])s\)", value,
    )
    if probe:
        labels = {"timeout": "источник не ответил до таймаута",
                  "dns": "не удалось разрешить DNS-имя источника",
                  "tls": "ошибка TLS/сертификата",
                  "schema": "ответ получен, но формат данных некорректен",
                  "connection": "сбой сетевого соединения",
                  "unknown": "неопределённая ошибка проверки"}
        return f"{labels[probe[1]]}; проверка {probe[2]} с"
    if value == "FAIL: RSS schema":
        return "ответ получен, но это не RSS/Atom"
    intraday_read = re.fullmatch(
        r"FAIL: intraday read ALGOPACK/(tradestats|orderstats|obstats) "
        r"(IncompleteRead|RemoteDisconnected|TimeoutError|URLError|HTTPError|ConnectionResetError)",
        value,
    )
    if intraday_read:
        return (f"INTRADAY SANDBOX: в последнем цикле не прочитан ALGOPACK/{intraday_read[1]} "
                f"({intraday_read[2]}); входы заблокированы, выходы по данным брокера сохранены. "
                "Это ошибка цикла, не подтверждение текущей недоступности API.")
    long_reasons = {
        "FAIL: LONG flow recovery not confirmed": (
            "LONG sandbox: нет полного потока ALGOPACK; восстановление по бумагам не подтверждено"),
        "FAIL: PROD admission requires operator check": "LONG ИИС: требуется проверка допуска",
        "FAIL: PROD attempt needs review": "LONG ИИС: зарезервированную попытку нужно сверить",
        "FAIL: PROD position or stop unverified": "LONG ИИС: лот/защитный стоп не подтверждён",
        "FAIL: PROD evidence or read-only API unavailable": (
            "LONG ИИС: журнал, допуск или PROD API недоступен"),
        "FAIL: reconcile one-shot entry/protection": "LONG sandbox: сверить покупку и защиту",
        "FAIL: scheduled one-shot plan missing": "LONG sandbox: нет ожидаемого утреннего плана",
        "FAIL: one-shot enrollment/ledger/plan unreadable": "LONG sandbox: проверить журнал/допуск",
        "FAIL: legacy LONG writer not disabled": "старый LONG должен быть выключен",
    }
    if value in long_reasons:
        return long_reasons[value]
    http = re.fullmatch(r"FAIL: HTTP ([1-5][0-9]{2})", value)
    if http:
        return "HTTP " + http[1]
    if value == "FAIL: missing token":
        return "токен отсутствует"
    if value == "FAIL: calendar database missing":
        return "нет локальной БД events.sqlite3; проверить calendar.timer"
    if value in {"FAIL: stale", "FAIL: stale/incomplete", "FAIL: no refresh"}:
        return "данные устарели или не получены"
    if value in {"FAIL: storage/read error", "FAIL: artifact read/schema error"}:
        return "ошибка чтения хранилища/формата"
    if value == INACTIVE:
        return "таймер выключен; проверить, запланирована ли остановка"
    if value == "FAIL: quality gate blocked":
        return "контроль качества данных не пройден"
    if re.fullmatch(r"(CRITICAL|WARNING): free=[0-9.]+ GiB \([0-9.]+%\)", value):
        return value
    return "сбой; подробности в diagnose"


def intraday_read_failure(value: object) -> str | None:
    """Extract only a known read-failure template, never arbitrary artifact text."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"api=ALGOPACK/shared stage=(tradestats|orderstats|obstats) page=[0-9]+ "
        r"error=(IncompleteRead|RemoteDisconnected|TimeoutError|URLError|HTTPError|"
        r"ConnectionResetError); read failed; entries blocked", value,
    )
    return f"FAIL: intraday read ALGOPACK/{match[1]} {match[2]}" if match else None


def compact_checks(checks: dict[str, str]) -> str:
    """One cause per line, no lost failure count and no unknown labels or response bodies."""
    known = set(API_LABELS) | {
        f"{kind}:{unit}" for kind in ("timer", "service") for unit in UNITS
    } | {"api:geo_cbr_press", "api:geo_moex_risk", "data:calendar", "data:long",
         "data:intraday", "data:prod-long-observer", "data:prod-long-protection",
         "quality:long", "quality:intraday",
         "quality:prod-long-observer", "backup:age", "telegram:outbox", "memory:available",
         "data:news-observer", "intraday:overnight_exposure", "monitor:collector",
         "collector:resources",
         "collector:services", "collector:integrations", "collector:artifacts"}
    for mount in ("/", "/var/lib/moex-tinvest-bot", "/var/log/moex-tinvest-bot"):
        known.update(("disk:" + mount, "inodes:" + mount))
    failures = {k: v for k, v in checks.items() if v != "OK"}
    if not checks:
        return "Нет ожидаемых метрик"
    if not failures:
        return f"OK · проверок: {len(checks)}"
    inactive = sorted(k[6:] for k, v in failures.items()
                      if k.startswith("timer:") and k[6:] in UNITS and v == INACTIVE)
    groups = ([f"Таймеры выключены ({len(inactive)}): {', '.join(inactive)}"] if inactive else [])
    for key, value in sorted(failures.items()):
        if key not in {"timer:" + name for name in inactive}:
            groups.append(f"{key if key in known else 'проверка'}: {safe_reason(value)}")
    extra = f"; ещё причин: {len(groups)-4}" if len(groups) > 4 else ""
    return f"Сбой {len(failures)}/{len(checks)} · " + "; ".join(groups[:4]) + extra


def safe_failure_details(name: str, path: Path, now: datetime) -> str:
    """Whitelist transformed local checks, never raw webhook/exception/response text."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        age = (now - datetime.fromisoformat(raw["observed_at"])).total_seconds()
        if not 0 <= age <= 600:
            return ""
        checks = raw["checks"]
        if "timer" in name.lower():
            timers = {k: v for k, v in checks.items() if k.startswith(("timer:", "service:"))
                      and isinstance(v, str)}
            if len(timers) > 1:
                return compact_checks(timers)
        units = {"shadow", "intraday", "health", "daily-report", "maintenance",
                 "prod-long-observer", "calendar"}
        allowed = set(API_LABELS) | {
            f"{kind}:{unit}" for kind in ("timer", "service") for unit in units
        } | {"api:geo_cbr_press", "api:geo_moex_risk",
             "data:calendar", "data:long", "data:intraday", "data:prod-long-protection",
             "quality:long",
             "quality:intraday", "backup:age", "telegram:outbox", "memory:available"}
        for mount in ("/", "/var/lib/moex-tinvest-bot", "/var/log/moex-tinvest-bot"):
            allowed |= {"disk:" + mount, "inodes:" + mount}
        lower = name.lower()
        lines = []
        for key in sorted(allowed):
            value = checks.get(key)
            if not isinstance(value, str) or value == "OK":
                continue
            if "[api:" in name and "[" + key + "]" not in name:
                continue
            if ("resources" in lower or "vm disk" in lower) and not key.startswith(
                ("disk:", "inodes:", "memory:")
            ):
                continue
            if "timer" in lower and not key.startswith(("timer:", "service:")):
                continue
            if ("trading data" in lower or "data freshness" in lower) and not key.startswith(
                ("data:", "quality:", "backup:", "telegram:", "intraday:")
            ):
                continue
            if "api" in lower and "[api:" not in name and not key.startswith("api:"):
                continue
            lines.append(f"• {key}: {safe_reason(value)}")
        return "\n".join(lines[:10])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
