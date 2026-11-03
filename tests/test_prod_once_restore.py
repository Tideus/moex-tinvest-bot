import shutil

import pytest
from test_prod_long_once import case as case
from test_prod_long_once import execute

from moex_bot.prod_once_restore import control_digest, verify


def test_restored_fences_verify_but_never_allow_new_buy(case, tmp_path):
    execute(case)
    expected = control_digest(case["claim_directory"])
    restored = tmp_path / "restored-control"
    shutil.copytree(case["claim_directory"], restored)
    assert verify(restored, expected) == "RESTORE_CONTROL_VERIFIED_NO_REARM"
    case["claim_directory"] = restored
    assert execute(case)["state"] == "ALREADY_CLAIMED"
    assert len(case["broker"].calls) == 1


@pytest.mark.parametrize("lost", ["claim.json", "buy-dispatch.json", "stop-dispatch.json",
                                  "result.json"])
def test_lost_or_rolled_back_control_is_blocked(case, lost):
    execute(case)
    expected = control_digest(case["claim_directory"])
    (case["claim_directory"] / lost).unlink()
    with pytest.raises((ValueError, OSError)):
        verify(case["claim_directory"], expected)


def test_foreign_dispatch_blocked(case):
    execute(case)
    (case["claim_directory"] / "stop-dispatch.json").write_text("{}")
    with pytest.raises(ValueError, match="another claim"):
        control_digest(case["claim_directory"])


def test_empty_or_untrusted_head_blocked(tmp_path, case):
    with pytest.raises(ValueError):
        verify(tmp_path, "a" * 64)
    execute(case)
    with pytest.raises(ValueError):
        verify(case["claim_directory"], "a" * 64)
