"""Allowlisted diagnostics only: never serialize broker errors, URLs or account bindings."""
from __future__ import annotations

import json
import socket
import sqlite3
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError

READ_METHODS = {"GetSandboxAccounts", "GetSandboxOrderState", "GetSandboxPositions",
                "GetSandboxOrders", "GetSandboxStopOrders"}

STAGES = {
    "scope": "проверка контура и путей",
    "account_config": "чтение привязки sandbox-счёта",
    "writer_lock": "получение блокировки проверки",
    "broker_init": "подготовка read-only sandbox API",
    "request_validation": "проверка параметров сверки",
    "ledger_binding": "проверка привязки журнала и допуска",
    "ledger_status": "чтение состояния журнала",
    "ledger_candidate": "чтение сохранённого сигнала",
    "ledger_parent": "чтение сохранённой покупки",
    "account_readback": "проверка доступности sandbox-счёта",
    "entry_readback": "сверка покупки у брокера",
    "positions_readback": "чтение позиций у брокера",
    "orders_readback": "чтение активных заявок",
    "stops_readback": "чтение активных стопов",
    "reconcile": "сверка покупки, позиций и заявок",
    "stop_evidence": "проверка сохранённого подтверждения стопа",
    "protection_readback": "проверка параметров защитного стопа у брокера",
}

# Only exact, static messages from our validation code can cross the log boundary.
VALIDATION_REASONS = {
    "explicit separate working sandbox account bindings required":
        "нет однозначной отдельной привязки рабочего sandbox-счёта",
    "explicit sandbox token/account required": "не настроены sandbox-токен или счёт",
    "sandbox enrollment or canonical ledger path invalid":
        "маркер участия или путь журнала не прошёл проверку",
    "one-shot ledger corrupt or bound to another account":
        "журнал повреждён или привязан к другому счёту",
    "CRITICAL: restored WAITING ledger has independent claim anchor":
        "обнаружен откат журнала: независимая запись допуска уже существует",
    "CRITICAL: claimed ledger lacks independent anchor; never retry":
        "для использованного допуска отсутствует независимая контрольная запись",
    "CRITICAL: claim anchor/account/candidate mismatch":
        "контрольная запись допуска не совпадает со счётом или сигналом",
    "CRITICAL: native stop dispatch fence mismatch":
        "контрольная запись отправки стопа не совпадает с журналом",
    "claimed ledger without candidate": "в журнале нет сигнала для занятого допуска",
    "foreign/changed candidate": "привязка счёта или объём сохранённого сигнала изменились",
    "durable parent mismatch": "сохранённая покупка не совпадает с сигналом",
    "working sandbox account not open": "рабочий sandbox-счёт не подтверждён как открытый",
    "position still loading": "брокер ещё загружает лимиты и позиции",
    "ambiguous active orders": "у активной заявки или стопа отсутствует инструмент",
    "missing stop dispatch fence": "нет контрольной записи отправки стопа",
    "sandbox account response binding changed": "ответ брокера относится к другому счёту",
    "sandbox order correlation ambiguous": "ответ о покупке не совпадает с ожидаемой заявкой",
    "sandbox FILLED quantity mismatch": "статус исполнения и исполненный объём расходятся",
    "sandbox exchange id changed": "биржевой идентификатор покупки изменился",
    "sandbox stop POST correlation ambiguous; never retry":
        "сохранённое подтверждение стопа не совпадает с запросом",
    "sandbox stop readback ambiguous": "единственный соответствующий активный стоп не найден",
    "sandbox stop fields mismatched": "параметры стопа не совпадают с ожидаемой защитой",
    "invalid sandbox response": "API вернул ответ неверного формата",
    "invalid sandbox response rows": "API вернул список неверного формата",
    "unknown response cannot prove empty sandbox rows":
        "неизвестный формат ответа не подтверждает отсутствие позиций или заявок",
}


def _safe_reason(exc: BaseException) -> tuple[str, str, int | None]:
    """Bounded causal traversal; raw exception text/headers/body are never returned."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, HTTPError):
            code = (current.code
                    if type(current.code) is int and 100 <= current.code <= 599 else None)
            return "http", "API вернул HTTP-ошибку", code
        if isinstance(current, ssl.SSLError):
            return "tls", "ошибка TLS или проверки сертификата", None
        if isinstance(current, socket.gaierror):
            return "dns", "не удалось разрешить имя API", None
        if isinstance(current, TimeoutError):
            return "timeout", "истекло время ожидания ответа", None
        if isinstance(current, ConnectionError):
            return "connection", "соединение не установлено или разорвано", None
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            current = current.reason
        else:
            current = current.__cause__ or current.__context__
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json", "не удалось прочитать JSON", None
    if isinstance(exc, sqlite3.Error):
        return "sqlite", "ошибка чтения SQLite-журнала", None
    if isinstance(exc, FileNotFoundError):
        return "missing_file", "не найден обязательный файл", None
    if isinstance(exc, PermissionError):
        return "permission", "недостаточно прав для проверки", None
    if isinstance(exc, (URLError, ConnectionError)):
        return "connection", "соединение не установлено или разорвано", None
    if isinstance(exc, OSError):
        return "io", "ошибка ввода-вывода", None
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        message = exc.args[0] if len(exc.args) == 1 else None
        reason = VALIDATION_REASONS.get(message) if isinstance(message, str) else None
        return "validation", reason or "проверка данных или контрольных привязок не пройдена", None
    return "unexpected", "непредвиденная ошибка проверки; требуется разбор", None


@dataclass
class VerificationProgress:
    stage: str = "scope"
    api_method: str | None = None

    def enter(self, stage: str, api_method: str | None = None) -> None:
        if stage not in STAGES:
            raise ValueError("unknown verification stage")
        self.stage, self.api_method = stage, api_method

    def failure(self, exc: BaseException) -> dict[str, Any]:
        category, reason, http_status = _safe_reason(exc)
        method = self.api_method if self.api_method in READ_METHODS else None
        return {
            "stage": self.stage if self.stage in STAGES else "scope",
            "source": "t_invest_sandbox" if method else "local",
            "host": "sandbox-invest-public-api.tbank.ru" if method else None,
            "method": method, "category": category, "reason": reason,
            "http_status": http_status,
        }
