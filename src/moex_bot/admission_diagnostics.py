"""Allowlisted diagnostics only; never renew or bypass a pinned trading review."""
from __future__ import annotations

# Match only our fixed validation messages, never publish arbitrary exception text.
REASONS = {
    "release changed; re-audit required": (
        "deployment_revision", "release_changed",
        "Установленный релиз отличается от проверенного: нужен новый аудит, не автодопуск."),
    "manifest changed": (
        "deployment_manifest", "manifest_changed", "Манифест релиза отличается от проверенного."),
    "installed file changed": (
        "deployment_files", "installed_file_changed",
        "Файл релиза не прошёл проверку целостности."),
    "root integrity service failed": (
        "deployment_integrity", "integrity_service_failed", "Проверка целостности не завершилась."),
    "fresh root deployment integrity proof required": (
        "deployment_integrity", "integrity_proof_invalid",
        "Нет свежего подтверждения целостности для этого пакета проверки."),
    "review only while unchanged PROD config disabled": (
        "production_settings", "production_settings_changed",
        "Настройки PROD изменены или PROD не выключен; пакет не обновлён."),
    "missing technical evidence": (
        "technical_evidence", "evidence_missing", "Неполный набор технических доказательств."),
    "unreviewed or changed technical evidence": (
        "technical_evidence", "evidence_changed", "Доказательства изменены или не прошли аудит."),
    "invalid time boundary": (
        "sandbox_evidence", "time_boundary_invalid", "Некорректная граница времени доказательств."),
    "receipt content hash differs": (
        "sandbox_evidence", "receipt_hash_mismatch",
        "Контрольная сумма sandbox-подтверждения неверна."),
    "unsafe or oversized evidence": (
        "evidence_read", "evidence_unreadable", "Файл доказательств отсутствует или небезопасен."),
}
STAGES = {"review_inputs", "sandbox_receipts", "sandbox_evaluate", "integrity_inputs"}
ERROR_TYPES = {"ValueError", "KeyError", "TypeError", "FileNotFoundError", "PermissionError",
               "JSONDecodeError", "UnicodeDecodeError", "TimeoutExpired", "CalledProcessError"}


def failure(exc: Exception, stage: str) -> dict[str, str]:
    known = REASONS.get(str(exc)) if type(exc) is ValueError else None
    return {
        "failure_stage": known[0] if known else stage if stage in STAGES else "unknown",
        "failure_reason": known[1] if known else "unclassified_error",
        "error_type": type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else "Exception",
    }


def explanation(packet: dict[str, object]) -> str:
    for stage, reason, text in REASONS.values():
        if packet.get("failure_stage") == stage and packet.get("failure_reason") == reason:
            return f"Этап: {stage}. Причина: {text}"
    raw_stage = packet.get("failure_stage")
    safe_stage = raw_stage if isinstance(raw_stage, str) and raw_stage in STAGES else "unknown"
    return f"Этап: {safe_stage}. Причина не классифицирована; проверьте локальный журнал."
