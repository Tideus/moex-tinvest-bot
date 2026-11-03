"""Offline DR. A checksum is not a freshness authority. Never installs or enables a bot."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path, PureWindowsPath
from uuid import NAMESPACE_URL, UUID, uuid5

PAIR = ("prod-orders.sqlite3", "prod-protection.sqlite3")
CONFIG = (
    "runtime.json",
    "maintenance.json",
    "prod-long.json",
    "prod-live.json",
    "prod-experiment.json",
)
ONCE_CONFIG = "prod-long-once.json"
CONTROL = Path("/var/lib/moex-tinvest-control/prod-long-once")
META = ("BUILD_INFO", "BUILD_MANIFEST.sha256", "VERSION")
STATE = "/var/lib/moex-tinvest-bot"
CORE = {
    "pyproject.toml",
    "src/moex_bot/__init__.py",
    "src/moex_bot/live_execution.py",
    "src/moex_bot/live_protection.py",
    "src/moex_bot/supervised_operator.py",
    "config/prod_experiment.json",
    "scripts/ubuntu/install.sh",
    "scripts/ubuntu/moex-botctl.sh",
}
SKIP = {".venv", "artifacts", "logs", "data", "work"}
CACHE = {"__pycache__", ".pytest_cache"}
GIB = 1 << 30
MAX_ARCHIVE, MAX_EXPANDED, MAX_MEMBER, MAX_FILES = 8 * GIB, 32 * GIB, 8 * GIB, 100_000


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def small_bytes(path):
    with path.open("rb") as src:
        data = src.read(16 * 1024 * 1024 + 1)
    check(len(data) <= 16 * 1024 * 1024, "oversized recovery metadata")
    return data


def clean_name(name):
    # Reject ambiguous portable names, traversal, aliases, ADS, and special files.
    name = name.removeprefix("./")
    parts = name.split("/")
    check(
        len(name.encode()) <= 1024
        and len(parts) <= 32
        and all(
            p
            and len(p.encode()) <= 255
            and p not in (".", "..")
            and not p.endswith((" ", "."))
            and not PureWindowsPath(p).is_reserved()
            for p in parts
        )
        and not re.search(r'[\\:\x00-\x1f<>|"?*]', name),
        "unsafe archive path",
    )
    return name


def source_file(root, relative):
    path = root / clean_name(relative)
    check(
        path.resolve(strict=True) == root.resolve(strict=True) / relative,
        "symlink in evidence path",
    )
    check(stat.S_ISREG(path.lstat().st_mode), "expected a regular evidence file")
    return path


def git_inventory(checkout, revision):
    # Only an operator-supplied Git object store. Never use archive-supplied
    # source_checkout or inspect/execute the contents of its working tree.
    raw = subprocess.check_output(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(checkout),
            "ls-tree",
            "-r",
            "-z",
            revision + "^{commit}",
        ]
    )
    result = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, kind, blob = header.decode().split()
        name = clean_name(path.decode("utf-8"))
        parts = name.split("/")
        if parts[0] in SKIP or set(parts[:-1]) & CACHE:
            continue
        check(kind == "blob" and mode in ("100644", "100755"), "unsupported Git release member")
        result[name] = blob
    return result


def release_files(root, checkout=None):
    files = {}
    for line in small_bytes(source_file(root, META[1])).decode().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        check(match, "malformed release manifest")
        sha, raw = match.groups()
        name = clean_name(raw)
        check(name not in files and name not in META, "duplicate release member")
        check(Path(name).name not in ("bot.env", ".env"), "unexpected secret in release")
        check(digest(source_file(root, name)) == sha, "release content mismatch")
        files[name] = sha
    check(
        CORE.issubset(files),
        "incomplete release manifest",
    )
    revision = small_bytes(source_file(root, "VERSION")).decode().strip()
    info = dict(
        line.split("=", 1)
        for line in small_bytes(source_file(root, "BUILD_INFO")).decode().splitlines()
    )
    check(
        re.fullmatch("[a-f0-9]{40}", revision)
        and revision == info.get("git_revision")
        and info.get("tree_state") == "clean",
        "release identity mismatch or dirty checkout",
    )
    actual = set()
    for base, dirs, names in os.walk(root):
        here = Path(base)
        dirs[:] = [d for d in dirs if d not in CACHE and not (here == root and d in SKIP)]
        check(not any((here / d).is_symlink() for d in dirs), "linked release directory")
        actual.update((here / n).relative_to(root).as_posix() for n in names)
    check(actual == set(files) | set(META), "unmanifested or missing release content")
    if checkout is not None:
        pinned = git_inventory(checkout, revision)
        check(set(pinned) == set(files), "release manifest differs from pinned Git inventory")
        for name, expected in pinned.items():
            path = root / name
            blob = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0")
            with path.open("rb") as src:
                while chunk := src.read(1024 * 1024):
                    blob.update(chunk)
            check(blob.hexdigest() == expected, "release differs from pinned Git blob")
    return files


def readonly(path):
    return closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True))


def logical_hash(db):
    # Includes mutable states/records, not just counts or MAX(timestamp).
    h = hashlib.sha256()
    for statement in db.iterdump():
        h.update(statement.encode())
        h.update(b"\x00")
    return h.hexdigest()


def linked_actions(orders, protection, account, identity):
    parents = {}
    for owner, rid, plan_hash, data, uid, status in protection.execute(
        "SELECT account,request_id,plan_hash,plan,live_uuid,state FROM protection"
    ):
        plan = json.loads(data)
        request, logical = plan["entry_request_id"], plan["entry_logical_id"]
        check(
            owner == plan["account_id"] == account
            and uid == identity
            and logical == str(UUID(logical))
            and request == str(uuid5(NAMESPACE_URL, f"prod:{account}:{logical}"))
            and request not in parents
            and type(plan["lots"]) is int
            and plan["lots"] == 1
            and rid == str(uuid5(NAMESPACE_URL, f"prod-protection:{account}:{request}"))
            and hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() == plan_hash,
            "inconsistent protection enrollment or plan",
        )
        parents[request] = (logical, plan["instrument"], status)
    linked = set()
    exits = []
    for logical, request, data in orders.execute(
        "SELECT logical_id,request_id,record FROM actions"
    ):
        intent = json.loads(data)["intent"]
        check(
            parents and intent["order_request_id"] == request and intent["side"] in ("buy", "sell"),
            "action lacks supervised protection record",
        )
        if intent["side"] == "buy":
            check(
                request in parents
                and parents[request][:2] == (logical, intent["instrument"])
                and type(intent["lots"]) is int
                and intent["lots"] == 1,
                "entry lacks matching supervised protection plan",
            )
            linked.add(request)
        else:
            exits.append(intent["instrument"])
    check(
        all(any(parents[r][1] == instrument for r in linked) for instrument in exits),
        "exit lacks matching supervised parent",
    )
    check(
        all(r in linked or p[2] == "PLANNED" for r, p in parents.items()),
        "progressed protection lacks parent order",
    )


def config_names(config):
    return CONFIG + ((ONCE_CONFIG,) if os.path.lexists(config / ONCE_CONFIG) else ())


def once_control(config, control, orders, identity, account):
    if ONCE_CONFIG not in config_names(config):
        check(control is None or not os.path.lexists(control), "control without one-shot settings")
        return None
    from moex_bot.prod_long_once_operator import load_settings
    from moex_bot.prod_once_observer import candidate_from_claim
    from moex_bot.prod_once_restore import control_digest

    settings = load_settings(source_file(config, ONCE_CONFIG))
    check(control is not None, "explicit one-shot control source required")
    if not os.path.lexists(control):
        check(settings["enabled"] is False and not orders.execute(
            "SELECT 1 FROM actions LIMIT 1").fetchone(), "missing active/spent control evidence")
        return {"state": "NOT_ENROLLED", "files": {}}
    verified = control_digest(control)
    claim = json.loads(small_bytes(control / "claim.json"))
    candidate = candidate_from_claim(claim)
    check(candidate.account_id == account and claim["journal_uuid"] == identity,
          "control belongs to another account/journal")
    if (control / "buy-dispatch.json").exists():
        from moex_bot.prod_long_once import paired

        _, plan = paired(candidate)
        check(orders.execute("SELECT 1 FROM actions WHERE request_id=?", (
            plan.entry_request_id,)).fetchone(), "dispatch fence newer than entry journal")
    return {"state": "CLAIMED_NO_REARM", "digest": verified,
            "files": {p.name: digest(source_file(control, p.name)) for p in control.iterdir()}}


def head(app, config, state, checkout=None, control=None):
    release_files(app, checkout)
    policies = {name: digest(source_file(config, name)) for name in config_names(config)}
    for name in ("prod-experiment.json", "prod-live.json"):
        cfg = json.loads(small_bytes(config / name))
        check(
            cfg.get("journal") == STATE + "/execution/" + PAIR[0],
            "custom journal paths unsupported: do not back up the wrong pair",
        )
        if name == "prod-experiment.json":
            check(
                cfg.get("protection_journal") == STATE + "/execution/" + PAIR[1],
                "custom protection journal path unsupported",
            )

    def integrity(db):
        check(
            db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "SQLite integrity check failed",
        )
        check(
            db.execute("SELECT value FROM metadata WHERE key='format'").fetchone() == ("1",),
            "unexpected journal format",
        )

    with readonly(source_file(state, "execution/" + PAIR[0])) as orders:
        integrity(orders)
        enrollment = dict(orders.execute("SELECT key,value FROM metadata"))
        identity = str(UUID(enrollment["live_journal_uuid"]))
        account = enrollment["live_account_id"]
        check(
            account.strip() and enrollment["migration_state"] == "ready",
            "unenrolled journal or migration incomplete",
        )
        check(
            not orders.execute(
                "SELECT 1 FROM actions WHERE account != ? LIMIT 1", ("prod:" + account,)
            ).fetchone(),
            "wrong entry account",
        )
        folder = state / "execution"
        audit = {}
        audit_dir = folder / "audit"
        if os.path.lexists(audit_dir):
            check(audit_dir.is_dir() and not audit_dir.is_symlink(), "unsafe audit directory")
            for p in audit_dir.rglob("*"):
                check(not p.is_symlink(), "linked audit evidence")
                if not p.is_dir():
                    rel = p.relative_to(folder).as_posix()
                    audit[rel] = digest(source_file(folder, rel))
        protection_hash = None
        protection_activity = False
        if os.path.lexists(folder / PAIR[1]):
            with readonly(source_file(state, "execution/" + PAIR[1])) as protection:
                integrity(protection)
                check(
                    not protection.execute(
                        "SELECT 1 FROM protection WHERE account != ? OR live_uuid != ? LIMIT 1",
                        (account, identity),
                    ).fetchone(),
                    "mismatched protection enrollment",
                )
                protection_hash = logical_hash(protection)
                protection_activity = bool(protection.execute(
                    "SELECT 1 FROM protection LIMIT 1").fetchone())
                linked_actions(orders, protection, account, identity)
        else:
            check(
                not orders.execute("SELECT 1 FROM actions LIMIT 1").fetchone()
                and not any(folder.glob(PAIR[1] + "-*"))
                and not any((folder / "audit").rglob("*")),
                "protection journal missing despite evidence of activity",
            )
        result = {
            "format": 2,
            "account": account,
            "enrollment": identity,
            "orders": logical_hash(orders),
            "protection": protection_hash,
            "audit": audit,
            "release": {name: digest(app / name) for name in META},
            "config": policies,
        }
        once = once_control(config, control, orders, identity, account)
        if once is not None:
            check(once["state"] != "NOT_ENROLLED" or not protection_activity,
                  "one-shot control absent despite protection evidence")
            result.update(format=3, control=once)
        return result


def snapshot(app, config, state, stage, checkout, control=None):
    """Caller holds production_session() throughout this entire operation."""
    app_out, config_out, state_out = stage / "release", stage / "config", stage / "state"
    before = head(app, config, state, checkout, control)
    app_out.mkdir()
    config_out.mkdir(exist_ok=True)
    for name in (*release_files(app), *META):
        target = app_out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file(app, name), target)
    for name in config_names(config):
        shutil.copyfile(source_file(config, name), config_out / name)
    check(not stage.resolve().is_relative_to(state.resolve()), "stage inside source state")
    state_out.mkdir(exist_ok=True)
    check(not any(state_out.iterdir()), "state staging directory must be empty")
    for p in state.rglob("*"):
        rel = clean_name(p.relative_to(state).as_posix())
        check(not p.is_symlink(), "linked state evidence")
        target = state_out / rel
        if p.is_dir():
            target.mkdir(exist_ok=True)
        elif any(p.name.endswith(s) for s in ("-wal", "-shm", "-journal")):
            # Captured through the SQLite backup API, not as racing standalone files.
            main = p.with_name(p.name.rsplit("-", 1)[0])
            with source_file(state, main.relative_to(state).as_posix()).open("rb") as f:
                check(f.read(16) == b"SQLite format 3\x00", "orphan SQLite companion")
        else:
            srcfile = source_file(state, rel)
            with srcfile.open("rb") as f:
                sqlite = f.read(16) == b"SQLite format 3\x00"
            if sqlite or p.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
                with readonly(srcfile) as src, closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")
                    check(
                        dst.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                        "state SQLite backup integrity failure",
                    )
            else:
                shutil.copyfile(srcfile, target)
    control_out = stage / "control" / "prod-long-once"
    if control is not None and os.path.lexists(control):
        control_out.mkdir(parents=True)
        for name in before["control"]["files"]:
            shutil.copyfile(source_file(control, name), control_out / name)
    check(before == head(app_out, config_out, state_out, control=control_out),
          "snapshot changed during capture")
    check(before == head(app, config, state, checkout, control), "source changed during snapshot")
    (stage / "HEAD.json").write_text(json.dumps(before, sort_keys=True), encoding="utf-8")


def extract(payload, out):
    seen = set()
    total = 0
    for index, member in enumerate(payload):
        check(index < MAX_FILES, "archive exceeds member count")
        if member.name == "." and member.isdir():
            continue
        name = clean_name(member.name)
        key = name.casefold()
        check(key not in seen and (member.isfile() or member.isdir()), "unsafe archive member")
        seen.add(key)
        total += member.size
        check(
            0 <= member.size <= MAX_MEMBER
            and total <= MAX_EXPANDED
            and len(seen) <= MAX_FILES
            and not member.issparse(),
            "archive exceeds extraction limits",
        )
        top = name.split("/")[0]
        check(
            top
            in (
                "release",
                "state",
                "config",
                "logs",
                "metadata",
                "control",
                "HEAD.json",
                "RECOVERY_MANIFEST.txt",
            ),
            "unexpected archive member",
        )
        check(
            Path(name).name
            not in ("bot.env", ".env", "prod-admission.json", "prod-experiment-admission.json",
                    "prod-long-once-admission.json"),
            "unexpected secret/admission",
        )
        path = out / name
        if member.isdir():
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            check(
                shutil.disk_usage(out).free > member.size + (256 << 20),
                "insufficient scratch space",
            )
            with payload.extractfile(member) as src, path.open("xb") as dst:
                shutil.copyfileobj(src, dst)


def bounded_copy(src, dst, limit, scratch=None):
    count = 0
    while chunk := src.read(1024 * 1024):
        count += len(chunk)
        check(count <= limit, "archive exceeds byte limit")
        if scratch is not None and (count - len(chunk)) % (16 << 20) == 0:
            check(shutil.disk_usage(scratch).free > 272 << 20, "insufficient scratch space")
        dst.write(chunk)


class TarReader:
    """Bound tarfile's first read of an extended PAX/GNU header, before getmembers validation."""

    def __init__(self, raw):
        self.raw = raw
        self.seek = raw.seek
        self.tell = raw.tell

    def read(self, size):
        check(0 <= size <= 4 * 1024 * 1024, "oversized tar metadata read")
        return self.raw.read(size)


def rehearse(archive, expected, trusted):
    check(re.fullmatch("[a-f0-9]{64}", expected), "expected independent SHA256")
    independent = json.loads(small_bytes(trusted))
    tmp = tempfile.gettempdir() if os.name == "nt" else "/var/tmp"
    with tempfile.TemporaryFile(dir=tmp) as src:
        # Authenticate exactly the immutable private bytes that will be extracted.
        with archive.open("rb") as original:
            bounded_copy(original, src, MAX_ARCHIVE, tmp)
        src.seek(0)
        check(
            hashlib.file_digest(src, "sha256").hexdigest() == expected,
            "archive does not match independently supplied checksum",
        )
        src.seek(0)
        # Never accept a destination or obey TMPDIR on Ubuntu. An unchecked archive
        # may only enter scratch; it can never reach installed code/config/journals.
        out = Path(tempfile.mkdtemp(prefix="moex-dr-quarantine-", dir=tmp))
        try:
            with tempfile.TemporaryFile(dir=tmp) as raw:
                with gzip.GzipFile(fileobj=src) as zipped:
                    bounded_copy(zipped, raw, MAX_EXPANDED, tmp)
                raw.seek(0)
                with tarfile.open(fileobj=TarReader(raw), mode="r:") as payload:
                    extract(payload, out)
            app = out / "release"
            release = set(release_files(app)) | set(META)
            check(
                release == {p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()},
                "unmanifested release content",
            )
            check(
                {p.name for p in (out / "config").iterdir()} == set(config_names(out / "config")),
                "unexpected or missing effective policy",
            )
            check(
                (out / "logs").is_dir()
                and b"format_version=2" in small_bytes(out / "RECOVERY_MANIFEST.txt").splitlines(),
                "incomplete archive",
            )
            control = out / "control" / "prod-long-once"
            calculated = head(app, out / "config", out / "state", control=control)
            if (out / "control").exists():
                check({p.relative_to(out / "control").as_posix()
                       for p in (out / "control").rglob("*") if p.is_file()} ==
                      {"prod-long-once/" + n
                       for n in calculated.get("control", {}).get("files", {})},
                      "unexpected control evidence")
            check(
                calculated == independent == json.loads(small_bytes(out / "HEAD.json")),
                "divergent/stale journal, release or policy versus trusted external head",
            )
        except BaseException:
            shutil.rmtree(out)
            raise
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("capture-head", "snapshot"):
        sub = subs.add_parser(name)
        sub.add_argument("--app", type=Path, default=Path("/opt/moex-tinvest-bot"))
        sub.add_argument("--config", type=Path, default=Path("/etc/moex-tinvest-bot"))
        sub.add_argument("--state", type=Path, default=Path(STATE))
        sub.add_argument("--control", type=Path, default=CONTROL)
        sub.add_argument(
            "--checkout",
            type=Path,
            required=True,
            help="independently trusted Git object store for the deployed revision",
        )
        if name == "snapshot":
            sub.add_argument("--stage", type=Path, required=True)
    sub = subs.add_parser("rehearse")
    sub.add_argument("--backup", type=Path, required=True)
    sub.add_argument("--expected-sha256", required=True)
    sub.add_argument("--trusted-head", type=Path, required=True)
    sub.add_argument("--rehearse", action="store_true", required=True)
    args = parser.parse_args()
    # Import only this trusted checker installation, never archive-provided Python.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    if args.command == "rehearse":
        out = rehearse(args.backup, args.expected_sha256, args.trusted_head)
        print(f"QUARANTINE ONLY: {out}\nNO install, credentials, live promotion or activation.")
        return
    # Import only the local audited stdlib flock implementation, never archived code.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from moex_bot.prod_session import production_session

    with production_session():
        if args.command == "snapshot":
            snapshot(args.app, args.config, args.state, args.stage, args.checkout, args.control)
        else:
            print(
                json.dumps(head(args.app, args.config, args.state, args.checkout, args.control),
                           sort_keys=True)
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as exc:
        sys.exit(f"FAIL: {exc}")
