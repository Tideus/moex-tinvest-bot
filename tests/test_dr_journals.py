"""Exercise real SQLite enrollment/protection state and the offline restore executable."""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path

import pytest
from test_live_execution import _setup
from test_live_protection import _case

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ubuntu/dr_journals.py"
spec = importlib.util.spec_from_file_location("dr_journals", SCRIPT)
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)


def prepare(work, *, virgin=False, full_release=False):
    app, config, state, stage = (work / name for name in ("app", "config", "state", "stage"))
    folder = state / "execution"
    folder.mkdir(parents=True)
    if virgin:
        _, kw, _ = _setup(work)
        shutil.copyfile(kw["journal_path"], folder / dr.PAIR[0])
    else:
        plan, journal, entry, broker = _case(work)
        assert journal.activate(plan, entry, broker) == "ACTIVE"
        shutil.copyfile(entry, folder / dr.PAIR[0])
        shutil.copyfile(journal.path, folder / dr.PAIR[1])
    config.mkdir()
    for name in dr.CONFIG:
        shutil.copyfile(ROOT / "config" / name.replace("-", "_"), config / name)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (app / "src/moex_bot").mkdir(parents=True)
    manifest = []
    names = sorted(dr.CORE)
    if full_release:
        release_tar = work / "checkout.tar"
        subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "archive",
                "--format=tar",
                "-o",
                str(release_tar),
                revision,
            ],
            cwd=ROOT,
            check=True,
        )
        with tarfile.open(release_tar) as payload:
            names = [m.name for m in payload if m.isfile() and m.name.split("/")[0] not in dr.SKIP]
            payload.extractall(app, filter="data")
    else:
        trusted_repo = work / "trusted"
        for name in names:
            p = trusted_repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((ROOT / name).read_bytes())
            (app / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, app / name)

        def git(*args):
            return subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(trusted_repo),
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "user.name=DR Test",
                    "-c",
                    "user.email=dr@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ]
            )

        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "offline fixture")
        revision = git("rev-parse", "HEAD").decode().strip()
    for name in names:
        manifest.append(f"{dr.digest(app / name)}  ./{name}\n")
    (app / "VERSION").write_text(revision)
    (app / "BUILD_INFO").write_text(f"git_revision={revision}\ntree_state=clean\n")
    (app / "BUILD_MANIFEST.sha256").write_text("".join(manifest))
    stage.mkdir()
    (stage / "logs").mkdir()
    (stage / "RECOVERY_MANIFEST.txt").write_text("format_version=2\n")
    return app, config, state, stage


def seal(stage, path):
    with tarfile.open(path, "w:gz") as tar:
        tar.add(stage, arcname=".")


def run_cli(archive, trusted, *extra, digest=None):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SCRIPT),
            "rehearse",
            "--backup",
            str(archive),
            "--expected-sha256",
            digest or dr.digest(archive),
            "--trusted-head",
            str(trusted),
            "--rehearse",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_cold_restore_cli_and_same_count_rollback(tmp_path):
    app, config, state, stage = prepare(tmp_path)
    trusted, archive = tmp_path / "independent.json", tmp_path / "archive.tar.gz"
    # Capture the independent source separately, NOT from the archive manifest.
    repo = tmp_path / "trusted"
    trusted.write_text(json.dumps(dr.head(app, config, state, repo)))
    dr.snapshot(app, config, state, stage, repo)
    seal(stage, archive)
    result = run_cli(archive, trusted)
    assert result.returncode == 0, result.stderr
    out = Path(result.stdout.splitlines()[0].split("QUARANTINE ONLY: ")[1])
    try:
        assert dr.head(out / "release", out / "config", out / "state") == json.loads(
            trusted.read_bytes()
        )
        assert not (out / "config/bot.env").exists()
    finally:
        shutil.rmtree(out)
    assert run_cli(archive, trusted, digest="0" * 64).returncode != 0
    for forbidden in ("--enable", "--env-file", "--destination"):
        assert run_cli(archive, trusted, forbidden).returncode != 0
    with closing(sqlite3.connect(state / "execution" / dr.PAIR[1])) as db, db:
        db.execute("UPDATE protection SET state='UNKNOWN'")
    trusted.write_text(json.dumps(dr.head(app, config, state, repo)))
    result = run_cli(archive, trusted)
    assert result.returncode and "divergent/stale" in result.stderr


@pytest.mark.parametrize(
    "change",
    ["missing", "wrong-enrollment", "release", "policy", "extra", "missing-audit", "changed-audit"],
)
def test_archive_rejects_missing_replaced_policy_or_release(tmp_path, change):
    app, config, state, stage = prepare(tmp_path)
    trusted, archive = tmp_path / "independent.json", tmp_path / "archive.tar.gz"
    audit = Path("execution/audit/parent.jsonl")
    (state / audit).parent.mkdir()
    (state / audit).write_text('{"attempt":1}\n')
    repo = tmp_path / "trusted"
    trusted.write_text(json.dumps(dr.head(app, config, state, repo)))
    dr.snapshot(app, config, state, stage, repo)
    pair = stage / "state/execution"
    if change == "missing":
        (pair / dr.PAIR[0]).unlink()
    elif change == "wrong-enrollment":
        with closing(sqlite3.connect(pair / dr.PAIR[1])) as db, db:
            db.execute("UPDATE protection SET live_uuid='another-id'")
    elif change == "release":
        (stage / "release/pyproject.toml").write_text("version = 'other'")
    elif change == "policy":
        (stage / "config/prod-long.json").write_text("{}")
    elif change == "missing-audit":
        (stage / "state" / audit).unlink()
    elif change == "changed-audit":
        (stage / "state" / audit).write_text('{"attempt":0}\n')
    else:
        (stage / "release/injected.py").write_text("raise SystemExit(0)")
    seal(stage, archive)
    assert run_cli(archive, trusted).returncode != 0


def test_source_snapshot_rejects_missing_or_linked_pair(tmp_path):
    app, config, state, stage = prepare(tmp_path)
    victim = state / "execution" / dr.PAIR[1]
    victim.unlink()
    with pytest.raises(ValueError, match="protection journal missing"):
        dr.snapshot(app, config, state, stage, tmp_path / "trusted")
    assert not (stage / "HEAD.json").exists()
    if os.name != "nt":
        victim.symlink_to(state / "execution" / dr.PAIR[0])
        with pytest.raises(ValueError, match="symlink"):
            dr.snapshot(app, config, state, stage, tmp_path / "trusted")


def test_witnessed_virgin_enrollment_full_git_release_no_fabricated_protection(tmp_path):
    app, config, state, stage = prepare(tmp_path, virgin=True, full_release=True)
    trusted, archive = tmp_path / "independent.json", tmp_path / "archive.tar.gz"
    witness = dr.head(app, config, state, ROOT)
    assert witness["protection"] is None
    trusted.write_text(json.dumps(witness))
    dr.snapshot(app, config, state, stage, ROOT)
    seal(stage, archive)
    result = run_cli(archive, trusted)
    assert result.returncode == 0, result.stderr
    out = Path(result.stdout.splitlines()[0].split("QUARANTINE ONLY: ")[1])
    try:
        assert (out / "release/scripts/ubuntu/install.sh").is_file()
        assert not (out / "state/execution" / dr.PAIR[1]).exists()
    finally:
        shutil.rmtree(out)


@pytest.mark.parametrize(
    "change", ["unlisted", "dropped", "rewritten", "empty", "mismatched", "unlinked-parent"]
)
def test_source_head_rejects_incoherent_evidence(tmp_path, change):
    app, cfg, state, _ = prepare(tmp_path)
    if change == "unlisted":
        (app / "src/moex_bot/unlisted.py").write_text("pass")
    elif change == "dropped":
        manifest = app / "BUILD_MANIFEST.sha256"
        manifest.write_text("\n".join(manifest.read_text().splitlines()[:2]))
    elif change == "rewritten":
        f = app / "src/moex_bot/live_execution.py"
        manifest = app / "BUILD_MANIFEST.sha256"
        old = dr.digest(f)
        f.write_text("pass")
        manifest.write_text(manifest.read_text().replace(old, dr.digest(f)))
    elif change == "unlinked-parent":
        with closing(sqlite3.connect(state / "execution" / dr.PAIR[0])) as db, db:
            db.execute("UPDATE actions SET logical_id='another-logical-order'")
    else:
        with closing(sqlite3.connect(state / "execution" / dr.PAIR[1])) as db, db:
            if change == "empty":
                db.execute("DELETE FROM protection")
            else:
                db.execute("UPDATE protection SET plan_hash='unbound'")
    with pytest.raises(ValueError):
        dr.head(app, cfg, state, tmp_path / "trusted")


def test_other_wal_database_is_snapshotted_with_committed_rows(tmp_path):
    app, cfg, state, stage = prepare(tmp_path, virgin=True)
    dbfile = state / "calendar.sqlite3"
    with closing(sqlite3.connect(dbfile)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE events(t)")
        db.execute("INSERT INTO events VALUES(3)")
        db.commit()
        dr.snapshot(app, cfg, state, stage, tmp_path / "trusted")
    with dr.readonly(stage / "state/calendar.sqlite3") as restored:
        assert restored.execute("SELECT * FROM events").fetchall() == [(3,)]
    assert not list((stage / "state").glob("calendar.sqlite3-*"))


def test_archive_bounds_and_path_limits(tmp_path, monkeypatch):
    import io

    with pytest.raises(ValueError, match="byte limit"):
        dr.bounded_copy(io.BytesIO(b"1234"), io.BytesIO(), 3)
    with pytest.raises(ValueError, match="unsafe archive path"):
        dr.clean_name("dir/" * 50 + "file")
    monkeypatch.setattr(dr, "MAX_MEMBER", 2)
    member = tarfile.TarInfo("state/data")
    member.size = 3
    with pytest.raises(ValueError, match="extraction limits"):
        dr.extract([member], tmp_path)
