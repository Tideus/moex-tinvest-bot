"""Private raw execution evidence: no credentials, no network without request log."""

import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_tinvest_sandbox import RecordingTransport

from moex_bot import live_audit
from moex_bot.live_audit import PREFIX, AuditJsonTransport


def test_audit_append_preserves_raw_fills_but_not_credentials_or_urls(tmp_path):
    path = tmp_path / "audit" / "execution.jsonl"
    transport = RecordingTransport(
        {
            "commission": {"units": "12"},
            "actualFillPrice": "101.3",
            "message": "Bearer PRIVATE-TOKEN at https://example.test/secrets",
            "nested": [{"api_key": "should-not-appear"}, "PRIVATE-TOKEN"],
        }
    )
    audit = AuditJsonTransport(transport, path)
    audit.post(
        PREFIX + "OrdersService/PostOrder",
        headers={
            "Authorization": "Bearer PRIVATE-TOKEN",
            "Content-Type": "application/json",
            "X-Api-Key": "PRIVATE-API",
            "token": "",
        },
        payload={"accountId": "private-account", "token": "hidden", "nested": (1, True)},
        timeout_seconds=1,
    )
    transport.response = {}
    audit.post(PREFIX + "OrdersService/GetOrderState", headers={}, payload={}, timeout_seconds=1)
    text = path.read_text()
    for secret in (
        "PRIVATE-TOKEN",
        "PRIVATE-API",
        "hidden",
        "should-not-appear",
        "https://",
        "Authorization",
    ):
        assert secret not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert len(rows) == 4 and rows[1]["response_raw"]["actualFillPrice"] == "101.3"
    assert rows[0]["payload_withoutsecrets"]["accountId"] == "private-account"


@pytest.mark.parametrize(
    "url",
    [
        "https://sandbox-invest-public-api.tbank.ru/rest/x",
        PREFIX + "OrdersService/PostOrder?token=x",
    ],
)
def test_wrong_host_or_query_never_calls_delegate(tmp_path, url):
    transport = RecordingTransport()
    with pytest.raises(ValueError):
        AuditJsonTransport(transport, tmp_path / "audit" / "log").post(
            url, headers={}, payload={}, timeout_seconds=1
        )
    assert transport.call is None


def test_delegate_exception_logs_class_not_message(tmp_path):
    transport = RecordingTransport(fail=True)
    path = tmp_path / "audit" / "log"
    with pytest.raises(TimeoutError):
        AuditJsonTransport(transport, path).post(
            PREFIX + "OrdersService/PostOrder", headers={}, payload={}, timeout_seconds=1
        )
    assert json.loads(path.read_text().splitlines()[1])["failure_class"] == "TimeoutError"


@pytest.mark.parametrize("fail_at", [1, 2])
def test_audit_write_failure_blocks_io_or_propagates_after_post(tmp_path, fail_at):
    transport = RecordingTransport()
    audit = AuditJsonTransport(transport, tmp_path / "unused")
    effects = [OSError("disk full")] if fail_at == 1 else [None, OSError("disk full")]
    with patch.object(audit, "_append", side_effect=effects), pytest.raises(OSError):
        audit.post(PREFIX + "OrdersService/PostOrder", headers={}, payload={}, timeout_seconds=1)
    assert (transport.call is None) == (fail_at == 1)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "nt",
        "symlink",
        "ancestor",
        "directory_type",
        "directory_mode",
        "directory_owner",
        "file_type",
        "file_links",
        "file_mode",
        "file_owner",
    ],
)
def test_private_evidence_permissions_are_checked_before_writing(tmp_path, fault):
    path = tmp_path / "audit" / "log"
    path.parent.mkdir()
    native = live_audit.os
    platform = SimpleNamespace(**{name: getattr(native, name) for name in dir(native)})
    platform.name = "nt" if fault == "nt" else "posix"
    platform.getuid = lambda: 123
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=123, st_nlink=1)
    if fault == "file_type":
        info.st_mode = stat.S_IFDIR | 0o600
    elif fault == "file_links":
        info.st_nlink = 2
    elif fault == "file_mode":
        info.st_mode |= 0o004
    elif fault == "file_owner":
        info.st_uid = 999
    platform.fstat = Mock(return_value=info)
    original_stat = Path.stat

    def changed_stat(value, *args, **kwargs):
        original = original_stat(value, *args, **kwargs)
        if value == path.parent:
            fields = list(original)
            fields[0] = (stat.S_IFREG if fault == "directory_type" else stat.S_IFDIR) | (
                0o777 if fault == "directory_mode" else 0o700
            )
            fields[4] = 999 if fault == "directory_owner" else 123
            return os.stat_result(fields)
        return original

    def changed_lstat(value):
        if fault == "ancestor" and value == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        return original_stat(value, follow_symlinks=False)

    with (
        patch.object(live_audit, "os", platform),
        patch.object(Path, "stat", changed_stat),
        patch.object(Path, "lstat", changed_lstat),
        patch.object(Path, "mkdir", return_value=None),
        patch.object(AuditJsonTransport, "_sync_directory"),
        patch.object(Path, "is_symlink", side_effect=None) as symbolic,
    ):
        symbolic.side_effect = lambda: fault == "symlink"
        if fault in {None, "nt"}:
            AuditJsonTransport(RecordingTransport(), path)._append({"test": True})
            assert json.loads(path.read_text()) == {"test": True}
        else:
            with pytest.raises(ValueError):
                AuditJsonTransport(RecordingTransport(), path)._append({"test": True})


def _collector_code():
    script = (Path(__file__).resolve().parents[1] / "scripts/ubuntu/moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    report = script.split("collect_report() (", 1)[1]
    assert report.index("flock -n 8") < report.index("mktemp -d")
    return report.split('"${PYTHON_BIN}" -c \'', 1)[1].split("\n'\n", 1)[0]


def test_report_collects_sqlite_backup_and_private_raw_audit(tmp_path, monkeypatch):
    source = tmp_path / "execution"
    audit = source / "audit"
    audit.mkdir(parents=True)
    with sqlite3.connect(source / "orders.sqlite3") as db:
        db.execute("CREATE TABLE evidence(value)")
        db.execute("INSERT INTO evidence VALUES('filled')")
    (audit / "live.jsonl").write_text('{"event":"response"}\n')
    target = tmp_path / "report" / "execution"
    monkeypatch.setenv("REPORT_EXECUTION_SOURCE", str(source))
    monkeypatch.setenv("REPORT_EXECUTION_TARGET", str(target))
    exec(_collector_code(), {})
    with sqlite3.connect(target / "orders.sqlite3") as db:
        assert db.execute("SELECT value FROM evidence").fetchone() == ("filled",)
    assert (target / "audit/live.jsonl").read_text() == '{"event":"response"}\n'


def test_posix_directory_sync_closes_descriptor_even_on_error(tmp_path):
    for fail in (False, True):
        platform = SimpleNamespace(
            O_RDONLY=0,
            O_DIRECTORY=64,
            open=Mock(return_value=123),
            close=Mock(),
            fsync=Mock(side_effect=OSError() if fail else None),
        )
        with patch.object(live_audit, "os", platform):
            if fail:
                with pytest.raises(OSError):
                    AuditJsonTransport._sync_directory(tmp_path)
            else:
                AuditJsonTransport._sync_directory(tmp_path)
        platform.close.assert_called_once_with(123)


def test_new_audit_directory_namespace_is_synced_before_request_returns(tmp_path):
    path = tmp_path / "new-audit" / "log"
    native = live_audit.os
    platform = SimpleNamespace(**{name: getattr(native, name) for name in dir(native)})
    platform.name = "posix"
    platform.getuid = lambda: 0
    original_stat = Path.stat

    def trusted_stat(value, *args, **kwargs):
        info = original_stat(value, *args, **kwargs)
        fields = list(info)
        fields[4] = 0
        if value == path.parent:
            fields[0] = stat.S_IFDIR | 0o700
        return os.stat_result(fields)

    platform.fstat = Mock(
        return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
    )
    with (
        patch.object(live_audit, "os", platform),
        patch.object(Path, "stat", trusted_stat),
        patch.object(AuditJsonTransport, "_sync_directory") as sync,
    ):
        AuditJsonTransport(RecordingTransport(), path)._append({"test": True})
    assert [call.args[0] for call in sync.call_args_list] == [path.parent, tmp_path]
