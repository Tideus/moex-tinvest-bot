"""One read-only 24-stock capture; only three exact public-market RPCs are allowed.

Uses T_INVEST_READONLY_TOKEN, never trading/account tokens. No order endpoints exist here.
Run separately from the trading application. Historical daily CSVs supply turnover only.
"""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from universe_history import load
from universe_watch import digest, number, policy, screen, write_result

BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
READS = frozenset(
    {
        "InstrumentsService/GetInstrumentBy",
        "MarketDataService/GetTradingStatus",
        "MarketDataService/GetOrderBook",
    }
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Reader:
    def __init__(self, token):
        if not token.strip():
            raise ValueError("read-only token is missing")
        self._token = token

    def read(self, method, payload):
        if method not in READS or "accountId" in payload:
            raise PermissionError("only the three market-data reads are allowed")
        request = Request(
            BASE + method,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            return json.load(response)


def quotation(raw):
    return number(raw["units"]) + number(raw["nano"]) / 1_000_000_000


def capture_one(entry, daily, reader, clock=lambda: datetime.now(UTC)):
    symbol, uid = entry["secid"], entry["t_invest_uid"]
    instrument = reader.read(
        "InstrumentsService/GetInstrumentBy",
        {
            "idType": "INSTRUMENT_ID_TYPE_UID",
            "id": uid,
        },
    )["instrument"]
    verified = clock()
    if (
        instrument["uid"] != uid
        or instrument["ticker"] != symbol
        or instrument["classCode"] != entry["class_code"]
        or instrument["currency"].lower() != "rub"
        or instrument["apiTradeAvailableFlag"] is not True
    ):
        raise ValueError("instrument identity or availability mismatch")
    lot = number(instrument["lot"])
    if lot <= 0 or lot != lot.to_integral_value():
        raise ValueError("invalid live lot size")
    status = reader.read("MarketDataService/GetTradingStatus", {"instrumentId": uid})
    status_at = clock()
    book = reader.read("MarketDataService/GetOrderBook", {"instrumentId": uid, "depth": 1})
    received = clock()
    if (received - status_at).total_seconds() > 3:
        raise ValueError("stale trading-status read")
    if book.get("instrumentUid") != uid or status.get("instrumentUid") != uid:
        raise ValueError("market-data identity mismatch")
    bid, ask = book["bids"][0], book["asks"][0]
    return {
        "secid": symbol,
        "source": "observed_top_of_book",
        "decision_at": received.isoformat(),
        "received_at": received.isoformat(),
        "exchange_at": book["orderbookTs"],
        "instrument_verified_at": verified.isoformat(),
        "bid": str(quotation(bid["price"])),
        "ask": str(quotation(ask["price"])),
        "bid_size_units": str(number(bid["quantity"]) * lot),
        "ask_size_units": str(number(ask["quantity"]) * lot),
        "lot_size": str(lot),
        "trading_allowed": status.get("apiTradeAvailableFlag") is True
        and status.get("limitOrderAvailableFlag") is True
        and status.get("tradingStatus") == "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        "turnover_sessions": [
            {"date": r["at"].date().isoformat(), "value_rub": str(r["value"])} for r in daily
        ],
        "raw_market_evidence": {"instrument": instrument, "status": status, "book": book},
    }


def capture(universe, cfg, data, reader):
    started = datetime.now(UTC)
    by_symbol = {e["secid"]: e for e in universe}
    if len(by_symbol) != len(universe) or not set(cfg["symbols"]) <= set(by_symbol):
        raise ValueError("universe does not cover the research policy")
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(capture_one, by_symbol[s], load(data, s, 24), reader): s
            for s in cfg["symbols"]
        }
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                # Provider exception strings may include headers: never persist them.
                errors.append({"secid": futures[future], "error_type": type(exc).__name__})
    snapshot = {
        "as_of": datetime.now(UTC).isoformat(),
        "rows": sorted(rows, key=lambda r: r["secid"]),
        "errors": sorted(errors, key=lambda r: r["secid"]),
        "universe_sha256": digest(universe),
    }
    return {
        "started_at": started.isoformat(),
        "snapshot": snapshot,
        "screen": screen(snapshot, cfg),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy", type=Path, default=Path(__file__).with_name("universe24.json"))
    p.add_argument("--universe", type=Path, default=Path("config/universe.json"))
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    reader = Reader(os.environ.get("T_INVEST_READONLY_TOKEN", ""))
    result = capture(json.loads(a.universe.read_bytes()), policy(a.policy), a.data, reader)
    write_result(a.output, result)
    print(
        json.dumps({"cohorts": result["screen"]["cohorts"], "errors": result["snapshot"]["errors"]})
    )
    return 2 if result["snapshot"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
