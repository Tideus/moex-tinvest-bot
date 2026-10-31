"""Offline Kuma monitor presentation migration; no live database editing."""
import argparse
import os
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

TARGETS = {
    "api:tinvest_sandbox": "T-Invest SANDBOX — account API",
    "api:tinvest_prod": "T-Invest PROD — IIS account API",
    "api:moex_iss": "MOEX ISS — public market data",
    "api:algopack_tradestats": "ALGOPACK — TradeStats",
    "api:algopack_orderstats": "ALGOPACK — OrderStats",
    "api:algopack_obstats": "ALGOPACK — OBStats",
    "api:calendar": "T-Invest PROD — investor calendar (read-only)",
    "api:telegram_chat": "Telegram — trading reports chat",
    "api:telegram_alert_chat": "Telegram — infrastructure alerts chat",
}
LEGACY = {
    "Syncthing GUI": ("Infrastructure / Syncthing GUI", "Infrastructure"),
    "MOEX / Syncthing GUI": ("Infrastructure / Syncthing GUI", "Infrastructure"),
    "MOEX / MinIO S3": ("Infrastructure / MinIO S3", "Infrastructure"),
    "MOEX / Observer heartbeat": ("MOEX Trading / Observer heartbeat", "MOEX Trading"),
    "MOEX / Resources": ("Server / VM disk, inodes and available RAM", "Server"),
    "MOEX / Timers and services": ("MOEX Trading / Trading timers and services", "MOEX Trading"),
    "MOEX / API integrations": ("MOEX Trading / All API sources (aggregate)", "MOEX Trading"),
    "MOEX / Trading data": ("MOEX Trading / Data freshness, calendar and report queue",
                            "MOEX Trading"),
}


def migrate(database, backup, *, apply=False, container="moex-monitoring-uptime-kuma-1"):
    database, backup = Path(database), Path(backup)
    if database.is_symlink() or not database.is_file():
        raise ValueError("regular existing database required")
    if apply:
        state = subprocess.check_output(
            ["docker", "inspect", container, "--format", "{{.State.Running}}"],
            text=True, timeout=10).strip()
        if state != "false":
            raise ValueError("Kuma must be stopped before applying migration")
        if backup.exists() or backup.resolve() == database.resolve():
            raise ValueError("new distinct backup path required")
    connection = sqlite3.connect(database.resolve().as_uri() +
                                 ("?mode=rw" if apply else "?mode=ro"), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor)")}
        required = {"id", "name", "type", "parent", "active", "user_id", "interval",
                    "description", "push_token", "maxretries", "retry_interval"}
        if not required <= columns:
            raise ValueError("unsupported Kuma monitor schema")
        joins = {row[1] for row in connection.execute("PRAGMA table_info(monitor_notification)")}
        if not {"monitor_id", "notification_id"} <= joins:
            raise ValueError("unsupported Kuma notification schema")
        rows = [dict(row) for row in connection.execute("SELECT * FROM monitor")]
        names = [row["name"] for row in rows]
        if len(names) != len(set(names)):
            raise ValueError("duplicate monitor names; review manually")
        template = next((row for row in rows if row["name"] in {
            "MOEX / API integrations", "MOEX Trading / All API sources (aggregate)"}), None)
        if template is None or template["type"] != "push":
            raise ValueError("API aggregate Push template required")
        # Validate the complete rename before creating a backup or changing any rows.
        # Broad substring matching can conflate S3 API/console or unrelated sync jobs.
        planned_names = [LEGACY.get(row["name"], (row["name"], None))[0] for row in rows]
        if len(planned_names) != len(set(planned_names)):
            raise ValueError("planned monitor names collide; review aliases manually")
        if apply:
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with sqlite3.connect(backup) as destination:
                connection.backup(destination)
            backup.chmod(0o600)
            connection.execute("BEGIN IMMEDIATE")
        actions = []

        def add(name, group, kind, description):
            existing = next((row for row in rows if row["name"] == name), None)
            if existing:
                if existing["type"] != kind:
                    raise ValueError("monitor name/type conflict")
                return existing["id"]
            actions.append("CREATE " + name)
            record = {key: value for key, value in template.items() if key != "id"}
            record.update(name=name, parent=group, type=kind, description=description,
                          push_token=uuid4().hex if kind == "push" else None,
                          interval=600 if kind == "push" else 60)
            if apply:
                keys = list(record)
                placeholders = ",".join("?" for _ in keys)
                cursor = connection.execute(
                    'INSERT INTO monitor (' + ','.join('"'+key+'"' for key in keys) +
                    ') VALUES (' + placeholders + ')', [record[key] for key in keys])
                identity = cursor.lastrowid
                if kind == "push":
                    connection.execute(
                        "INSERT INTO monitor_notification(monitor_id,notification_id) "
                        "SELECT ?,notification_id FROM monitor_notification WHERE monitor_id=?",
                        (identity, template["id"]))
            else:
                identity = -len(actions)
            rows.append({**record, "id": identity})
            return identity

        groups = {name: add(name, None, "group", name + " — service category")
                  for name in ("MOEX Trading", "Server", "Infrastructure")}
        for row in list(rows):
            change = LEGACY.get(row["name"])
            if change is None:
                change = next((v for v in LEGACY.values() if v[0] == row["name"]), None)
            if not change:
                continue
            name, group = change
            if row["name"] != name or row["parent"] != groups[group]:
                actions.append("UPDATE " + name)
                if apply:
                    connection.execute("UPDATE monitor SET name=?,parent=? WHERE id=?",
                                       (name, groups[group], row["id"]))
        for key, title in TARGETS.items():
            add(f"MOEX Trading / {title} [{key}]", groups["MOEX Trading"], "push",
                f"Read-only probe: {key}. Exact source/environment; inspect moex-botctl diagnose.")
        if apply:
            connection.commit()
        return actions
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--container", default="moex-monitoring-uptime-kuma-1")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for action in migrate(args.database, args.backup, apply=args.apply, container=args.container):
        print(action)
    print("PASS: applied offline" if args.apply else "PASS: dry-run; no writes")


if __name__ == "__main__":
    main()
