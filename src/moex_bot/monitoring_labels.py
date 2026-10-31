"""Non-secret, stable labels shared by monitoring and the Kuma adapter."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# Keys remain stable for existing incident databases; labels are presentation only.
API_LABELS = {
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


def safe_failure_details(name: str, path: Path, now: datetime) -> str:
    """Whitelist transformed local checks, never raw webhook/exception/response text."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        age = (now - datetime.fromisoformat(raw["observed_at"])).total_seconds()
        if not 0 <= age <= 600:
            return ""
        checks = raw["checks"]
        units = {"shadow", "intraday", "health", "daily-report", "maintenance",
                 "prod-long-observer", "calendar"}
        allowed = set(API_LABELS) | {
            f"{kind}:{unit}" for kind in ("timer", "service") for unit in units
        } | {"api:geo_cbr_press", "api:geo_moex_risk",
             "data:calendar", "data:long", "data:intraday", "quality:long",
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
            reason = "сбой; подробности в diagnose"
            http = re.fullmatch(r"FAIL: HTTP ([1-5][0-9]{2})", value)
            if http:
                reason = "HTTP " + http[1]
            elif value == "FAIL: missing token":
                reason = "токен отсутствует"
            elif value in {"FAIL: stale", "FAIL: stale/incomplete", "FAIL: no refresh"}:
                reason = "данные устарели или не получены"
            elif value in {"FAIL: storage/read error", "FAIL: artifact read/schema error"}:
                reason = "ошибка чтения хранилища/формата"
            elif value == "FAIL: active=inactive, no schedule":
                reason = "таймер выключен; проверить, запланирована ли остановка"
            elif value == "FAIL: quality gate blocked":
                reason = "контроль качества данных не пройден"
            elif re.fullmatch(r"CRITICAL: free=[0-9.]+ GiB \([0-9.]+%\)", value):
                reason = value
            lines.append(f"• {key}: {reason}")
        return "\n".join(lines[:10])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
