"""Offline retrospective close-to-close accounting; never rewrites daily reports."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from moex_bot.daily_report_store import closing_window
from moex_bot.trader_journal import OPERATIONS_SNAPSHOT, digest, number, timestamp


def bridge(opening, ending, receipt):
    """Fail closed unless one complete broker operations snapshot spans both valuations."""
    result = {"status": "UNAVAILABLE", "pnl": None, "retrospective": True,
              "historical_reports_unchanged": True, "execution_allowed": False}
    try:
        if opening is None or ending is None:
            raise ValueError("valuation_missing")
        account = receipt["account_id"]
        if not isinstance(account, str) or not account:
            raise ValueError("account_missing")
        binding = hashlib.sha256(account.encode()).hexdigest()
        if any(v.get("account_binding") != binding for v in (opening, ending)):
            raise ValueError("account_mismatch")
        if any(v.get("source") != "t_invest_sandbox" for v in (opening, ending, receipt)):
            raise ValueError("unsupported_source")
        a, b = (timestamp(v["observed_at"]) for v in (opening, ending))
        start, end = (timestamp(receipt[k]) for k in
                      ("journal_coverage_from", "journal_coverage_to"))
        if not start <= a < b <= end <= timestamp(receipt["observed_at"]):
            raise ValueError("operations_window_incomplete")
        operations = receipt["executed_operations"]
        proof = receipt["journal_operations_snapshot"]
        ids = [o["operation_id"] for o in operations]
        if (not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids)
                or proof.get("protocol") != OPERATIONS_SNAPSHOT
                or type(proof.get("pages")) is not int or proof["pages"] < 1
                or type(proof.get("count")) is not int or proof["count"] != len(ids)
                or proof.get("ids_sha256") != digest(sorted(ids))):
            raise ValueError("operations_manifest_invalid")
        external = Decimal(0)
        for op in operations:
            at = timestamp(op["occurred_at"])
            if not start <= at <= end:
                raise ValueError("operation_outside_coverage")
            # Missing external flow is unknown, not zero. BUY/SELL are not deposits.
            flow = number(op["external_cashflow"])
            if a < at <= b:
                external += flow
        change = number(ending["reported_equity"]) - number(opening["reported_equity"])
        result.update(status="RECONCILED", pnl=str(change - external),
                      valuation_change=str(change), external_cashflow=str(external),
                      period_started_at=a.isoformat(), period_ended_at=b.isoformat(),
                      method="equity_change_less_external_cashflow",
                      fees="already_reflected_in_broker_equity_not_subtracted_twice",
                      is_full_session_return=False,
                      input_sha256=[digest(v) for v in (opening, ending, receipt)])
    except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
        # Fixed reason avoids exposing accounts or malformed private payloads.
        result["reason"] = "missing_invalid_or_unbound_accounting_evidence"
    return result


def archive_supplement(archive, receipt, first, last):
    if first > last:
        raise ValueError("invalid_period")
    closes, originals = {}, {}
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            match = re.fullmatch(
                r"\./state/artifacts/(long-close|long-daily-performance)-(\d{4}-\d{2}-\d{2})\.json",
                member.name)
            if not match or not first - timedelta(days=1) <= date.fromisoformat(
                    match[2]) <= last:
                continue
            if not member.isfile() or member.size > 10_000_000:
                raise ValueError("invalid_archive_member")
            destination = closes if match[1] == "long-close" else originals
            if match[2] in destination:
                raise ValueError("duplicate_archive_member")
            stream = handle.extractfile(member)
            if stream is None:
                raise ValueError("missing_archive_member")
            with stream:
                payload = json.load(stream)
            if match[1] == "long-close" and (
                    payload.get("report_date") != match[2]
                    or not closing_window(date.fromisoformat(match[2]),
                                          timestamp(payload["observed_at"]))):
                raise ValueError("wrong_close_date")
            destination[match[2]] = payload
    rows = []
    day = first
    while day <= last:
        # Never borrow a later valuation to manufacture a missing day boundary.
        result = bridge(closes.get(str(day - timedelta(days=1))), closes.get(str(day)), receipt)
        result.update(day=str(day), original_pnl=originals.get(str(day), {}).get(
            "performance", {}).get("pnl"))
        rows.append(result)
        day += timedelta(days=1)
    return {"protocol": "long-retrospective-accounting-v1", "execution_allowed": False,
            "rows": rows, "note": "Actual close-snapshot windows, not reconstructed opening prices"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = archive_supplement(args.archive, json.loads(args.receipt.read_bytes()),
                                args.first, args.last)
    # Exclusive creation prevents accidental overwriting of historical reports.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"Accounting windows: {len(result['rows'])}; "
          f"unavailable: {sum(r['status'] != 'RECONCILED' for r in result['rows'])}")


if __name__ == "__main__":
    main()
