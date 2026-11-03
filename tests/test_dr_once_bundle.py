import json
import shutil

import pytest
from test_dr_journals import ROOT, dr, prepare, run_cli, seal
from test_prod_long_once import case as case
from test_prod_long_once import execute


def scenario(tmp_path, case=None):
    work = tmp_path / "fixture"
    work.mkdir()
    app, config, state, stage = prepare(work, virgin=True)
    shutil.copyfile(ROOT / "config/prod_long_once.json", config / dr.ONCE_CONFIG)
    control = tmp_path / "not-enrolled"
    if case:
        execute(case)
        control = case["claim_directory"]
        shutil.copyfile(case["journal"], state / "execution" / dr.PAIR[0])
        shutil.copyfile(case["protection_journal"], state / "execution" / dr.PAIR[1])
    trusted, archive = tmp_path / "external-head.json", tmp_path / "backup.tar.gz"
    trusted.write_text(json.dumps(dr.head(app, config, state, work / "trusted", control)))
    dr.snapshot(app, config, state, stage, work / "trusted", control)
    seal(stage, archive)
    return stage, trusted, archive


@pytest.mark.parametrize("filled", [False, True])
def test_real_archive_rehearsal_preserves_once_state_without_rearming(tmp_path, case, filled):
    stage, trusted, archive = scenario(tmp_path, case if filled else None)
    assert json.loads(trusted.read_text())["format"] == 3
    result = run_cli(archive, trusted)
    assert result.returncode == 0, result.stderr
    from pathlib import Path

    out = Path(result.stdout.splitlines()[0].split("QUARANTINE ONLY: ")[1])
    try:
        assert not (out / "config/prod-long-once-admission.json").exists()
        assert (out / "control/prod-long-once/claim.json").exists() == filled
        assert json.loads((out / "config/prod-long-once.json").read_text())["enabled"] is False
    finally:
        shutil.rmtree(out)


@pytest.mark.parametrize("lost", ["claim.json", "buy-dispatch.json", "stop-dispatch.json",
                                  "result.json", "all"])
def test_control_rollback_rejected_even_with_valid_archive_checksum(tmp_path, case, lost):
    stage, trusted, archive = scenario(tmp_path, case)
    control = stage / "control/prod-long-once"
    if lost == "all":
        shutil.rmtree(control)
    else:
        (control / lost).unlink()
    seal(stage, archive)  # Attacker/recovery can recompute checksum, not trusted external head.
    assert run_cli(archive, trusted).returncode != 0


def test_draft_or_live_admission_must_never_enter_restore_archive(tmp_path):
    stage, trusted, archive = scenario(tmp_path)
    (stage / "config/prod-long-once-admission.json").write_text("{}")
    seal(stage, archive)
    assert run_cli(archive, trusted).returncode != 0
