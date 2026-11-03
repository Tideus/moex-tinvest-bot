import json
from datetime import UTC, datetime

import pytest

from moex_bot.prod_long_once_operator import validate_grant
from moex_bot.prod_once_readiness import draft


def test_preparation_cannot_be_consumed_as_live_authority(tmp_path):
    evidence = {}
    for kind in ("tests", "independent_audit", "restore_drill"):
        path = tmp_path / (kind + ".json")
        path.write_text(json.dumps(dict(kind=kind, revision="test-revision",
                                        result="PASS", scope="offline technical test")))
        evidence[kind] = path
    now = datetime.now(UTC)
    packet = draft(account="private-account", identity="journal", release="a" * 64,
                   revision="test-revision", evidence=evidence, now=now)
    assert "private-account" not in json.dumps(packet)
    assert packet["evidence_sha256"]["sandbox_fill_and_native_stop"] is None
    with pytest.raises(ValueError):
        validate_grant(packet, account="private-account", release="a" * 64,
                       journal_uuid="journal", now=now)
    evidence["tests"].write_text('{"result":"PASS"}')
    with pytest.raises(ValueError):
        draft(account="private-account", identity="journal", release="a" * 64,
              revision="test-revision", evidence=evidence, now=now)
