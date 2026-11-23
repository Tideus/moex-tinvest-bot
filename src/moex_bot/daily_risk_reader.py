"""Read-only, conservative account NAV evidence for the first LONG entry.

Broker dailyYield is diagnostic only. A complete cursor alone does not prove that
cash movements have arrived: the RUB cash bridge must reconcile as well.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .daily_risk import (
    MSK,
    PROTOCOL,
    AccountDailyRisk,
    account_hash,
    canonical,
    load_basis,
    number,
    validate_basis,
)

Post = Callable[[str, Mapping[str, object]], Mapping[str, Any]]


def quotation(value: object) -> Decimal:
    if not isinstance(value, Mapping) or not ({'units', 'nano'} & value.keys()):
        raise ValueError('missing risk quotation')
    units, nano = number(value.get('units', 0)), number(value.get('nano', 0))
    if (units != int(units) or nano != int(nano) or abs(nano) >= 1000000000
            or units*nano < 0 or not -(2**63) <= units < 2**63):
        raise ValueError('invalid risk quotation')
    return units+nano/Decimal(1000000000)


def rows(raw: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    result = raw.get(name, [])
    if not isinstance(result, list) or any(not isinstance(x, dict) for x in result):
        raise ValueError('invalid repeated field in account risk evidence')
    return result


def money(raw: object) -> Decimal:
    if not isinstance(raw, Mapping) or str(raw.get('currency', '')).upper() != 'RUB':
        raise ValueError('non-RUB or missing cash evidence')
    value = quotation(raw)
    return number(value)


def frame(post: Post, account: str) -> dict[str, Any]:
    portfolio = post('OperationsService/GetPortfolio', {'accountId': account, 'currency': 'RUB'})
    positions = post('OperationsService/GetPositions', {'accountId': account})
    if positions.get('limitsLoadingInProgress', False) is not False:
        raise ValueError('position limits loading')
    inventory: dict[str, str] = {}
    seen_uids: set[str] = set()
    for row in rows(portfolio, 'positions'):
        uid = row.get('instrumentUid')
        quantity = quotation(row['quantity'])
        if not isinstance(uid, str) or not uid or uid in seen_uids:
            raise ValueError('ambiguous inventory')
        seen_uids.add(uid)
        if quantity:
            inventory[uid] = str(quantity)
    cash_rows = rows(positions, 'money')
    if any(not isinstance(r.get('currency'), str) or len(r['currency']) != 3
           or not r['currency'].isascii() or not r['currency'].isalpha() for r in cash_rows):
        raise ValueError('invalid cash currency')
    rub = [r for r in cash_rows if str(r.get('currency', '')).upper() == 'RUB']
    if len(rub) != 1:
        raise ValueError('explicit unique RUB cash balance required')
    # Foreign currency changes may be transfers, not investment performance.
    currencies = {str(r.get('currency', '')).upper(): str(quotation(r))
                  for r in cash_rows if str(r.get('currency', '')).upper() != 'RUB'}
    if len(currencies) != len(cash_rows)-1:
        raise ValueError('duplicate currency balances')
    # Currency portfolio quantities are redundant with explicit GetPositions money.
    for row in rows(portfolio, 'positions'):
        if row.get('instrumentType') == 'currency':
            inventory.pop(str(row.get('instrumentUid')), None)
    return {'equity': str(money(portfolio.get('totalAmountPortfolio'))),
            'rub_cash': str(money(rub[0])), 'inventory': inventory, 'currencies': currencies}


def stable(first: Mapping[str, Any], last: Mapping[str, Any]) -> None:
    if any(first[k] != last[k] for k in ('rub_cash', 'inventory', 'currencies')):
        raise ValueError('cash/inventory changed while reading; retry only on a fresh cycle')


def baseline(post: Post, account: str, clock: Callable[[], datetime]) -> dict[str, Any]:
    started = clock()
    if started.tzinfo is None:
        raise ValueError('naive start-of-day clock')
    midnight = datetime.combine(started.astimezone(MSK).date(), time.min, MSK)
    if not midnight <= started < midnight+timedelta(seconds=60):
        raise ValueError('outside start-of-day window; never backdate')
    first, last = frame(post, account), frame(post, account)
    stable(first, last)
    now = clock()
    # Any visible operation during the opening reads makes their cutoff ambiguous.
    cash_bridge(post, account, started, now, forbid_since=started)
    raw = {'protocol': PROTOCOL, 'account_sha256': account_hash(account),
           'started_at': started.isoformat(), 'observed_at': now.isoformat(), **last}
    raw['sha256'] = hashlib.sha256(canonical(raw)).hexdigest()
    validate_basis(raw, account, now)
    return raw


# Only explicitly classified monetary operations can be reconciled. Trades and
# security transfers change inventory and are currently UNKNOWN for this pilot.
INCOME = {
    'OPERATION_TYPE_DIVIDEND', 'OPERATION_TYPE_DIVIDEND_EXT', 'OPERATION_TYPE_COUPON',
    'OPERATION_TYPE_BROKER_FEE', 'OPERATION_TYPE_SERVICE_FEE', 'OPERATION_TYPE_MARGIN_FEE',
    'OPERATION_TYPE_SUCCESS_FEE', 'OPERATION_TYPE_TRACK_MFEE', 'OPERATION_TYPE_TRACK_PFEE',
    'OPERATION_TYPE_TAX', 'OPERATION_TYPE_TAX_CORRECTION', 'OPERATION_TYPE_TAX_DIVIDEND',
    'OPERATION_TYPE_ACCRUING_VARMARGIN', 'OPERATION_TYPE_WRITING_OFF_VARMARGIN',
}
EXTERNAL = {'OPERATION_TYPE_INPUT', 'OPERATION_TYPE_OUTPUT'}


def cash_bridge(post: Post, account: str, start: datetime, end: datetime,
                *, forbid_since: datetime | None = None) -> tuple[Decimal, Decimal, str]:
    cursor = ''
    cursors = {cursor}
    seen: dict[str, bytes] = {}
    all_cash = external = Decimal(0)
    for _ in range(100):
        raw = post('OperationsService/GetOperationsByCursor', {
            'accountId': account, 'from': start.isoformat(), 'to': end.isoformat(),
            'state': 'OPERATION_STATE_EXECUTED', 'limit': 1000, 'cursor': cursor,
            'withoutCommissions': False, 'withoutTrades': False, 'withoutOvernights': False,
        })
        for row in rows(raw, 'items'):
            identity = row.get('id')
            if not isinstance(identity, str) or not identity:
                raise ValueError('operation identity missing')
            encoded = canonical(row)
            if identity in seen:
                if seen[identity] != encoded:
                    raise ValueError('conflicting duplicate operations')
                continue
            seen[identity] = encoded
            at = datetime.fromisoformat(str(row['date']).replace('Z', '+00:00'))
            if at.tzinfo is None or not start < at <= end:
                raise ValueError('operation at ambiguous baseline boundary or outside cutoff')
            if forbid_since is not None and at >= forbid_since:
                raise ValueError('operation during NAV readback; no coherent cutoff')
            kind = row.get('type')
            if row.get('state') != 'OPERATION_STATE_EXECUTED' or kind not in INCOME | EXTERNAL:
                raise ValueError('unclassified operation or inventory activity; risk UNKNOWN')
            amount = money(row.get('payment'))
            if ((kind == 'OPERATION_TYPE_INPUT' and amount <= 0)
                    or (kind == 'OPERATION_TYPE_OUTPUT' and amount >= 0)):
                raise ValueError('invalid external cash-flow sign')
            all_cash += amount
            if kind in EXTERNAL:
                external += amount
        if raw.get('hasNext', False) is False:
            digest = hashlib.sha256(canonical({k: v.decode() for k, v in seen.items()})).hexdigest()
            return all_cash, external, digest
        following = raw.get('nextCursor')
        if raw.get('hasNext') is not True or not isinstance(following, str) or following in cursors:
            raise ValueError('incomplete or cyclic operations pagination')
        cursors.add(following)
        cursor = following
    raise ValueError('operations pagination exhausted')


def read_risk(post: Post, account: str, clock: Callable[[], datetime], directory: Path
              ) -> AccountDailyRisk:
    started = clock()
    basis = load_basis(directory, account, started)
    first = frame(post, account)
    cutoff = clock()
    bridge = cash_bridge(post, account, datetime.fromisoformat(basis['observed_at']), cutoff,
                         forbid_since=started)
    all_cash, external, _ = bridge
    last = frame(post, account)
    frame_at = clock()
    stable(first, last)
    repeated = cash_bridge(post, account, datetime.fromisoformat(basis['observed_at']), frame_at,
                           forbid_since=started)
    if repeated != bridge:
        raise ValueError('operations changed across NAV readback; risk UNKNOWN')
    if (last['inventory'] != basis['inventory']
            or last['currencies'] != basis.get('currencies')):
        raise ValueError('inventory/FX changed since BOD; exact attribution unavailable')
    if number(last['rub_cash'])-number(basis['rub_cash']) != all_cash:
        raise ValueError('cash bridge mismatch: missing/late broker operation; risk UNKNOWN')
    now = clock()
    if (now-started).total_seconds() > 20 or now < started:
        raise ValueError('account risk readback stale')
    opening, equity = number(basis['equity']), number(last['equity'])
    result = AccountDailyRisk(account_hash(account), datetime.fromisoformat(basis['observed_at']),
                              frame_at, opening, equity, external, equity-opening-external,
                              str(basis['sha256']))
    result.validate(account, equity, now)
    return result
