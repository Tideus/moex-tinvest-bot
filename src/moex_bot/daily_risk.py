"""Separate LONG and account risk. Unknown evidence never becomes zero or a BUY grant."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MSK = ZoneInfo('Europe/Moscow')
BASIS_DIR = Path('/var/lib/moex-tinvest-control/daily-risk')
PROTOCOL = 'account-risk-bod-v1'


def account_hash(account: str) -> str:
    return hashlib.sha256(account.encode()).hexdigest()


def canonical(raw: Mapping[str, Any]) -> bytes:
    return json.dumps(raw, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('risk: invalid numeric evidence')
    try:
        result = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError('risk: invalid numeric evidence') from exc
    if not result.is_finite():
        raise ValueError('risk: nonfinite numeric evidence')
    return result


@dataclass(frozen=True)
class AccountDailyRisk:
    account_sha256: str
    basis_at: datetime
    observed_at: datetime
    opening_equity: Decimal
    current_equity: Decimal
    external_flows: Decimal
    pnl: Decimal
    basis_sha256: str

    def validate(self, account: str, equity: Decimal, now: datetime) -> None:
        if (self.account_sha256 != account_hash(account)
                or any(t.tzinfo is None for t in (self.basis_at, self.observed_at, now))
                or self.basis_at.astimezone(MSK).date() != now.astimezone(MSK).date()
                or not self.basis_at <= self.observed_at <= now
                or now - self.observed_at > timedelta(seconds=20)):
            raise ValueError('IIS risk: stale, foreign or future daily basis')
        midnight = datetime.combine(self.basis_at.astimezone(MSK).date(), time.min, MSK)
        if not timedelta(0) <= self.basis_at - midnight <= timedelta(seconds=60):
            raise ValueError('IIS risk: basis was not observed at the start of day')
        values = (self.opening_equity, self.current_equity, self.external_flows, self.pnl)
        if (any(not v.is_finite() for v in values) or self.opening_equity <= 0
                or self.current_equity <= 0 or self.current_equity != equity
                or self.pnl != self.current_equity-self.opening_equity-self.external_flows
                or len(self.basis_sha256) != 64
                or any(c not in '0123456789abcdef' for c in self.basis_sha256)):
            raise ValueError('IIS risk: inconsistent valuation or cash-flow evidence')


def check_daily_limits(*, account: str, equity: Decimal, now: datetime,
                       account_risk: AccountDailyRisk | None, strategy_pnl: Decimal | None,
                       strategy_capital: Decimal, strategy_weight: Decimal,
                       account_weight: Decimal) -> None:
    if (any(not x.is_finite() or x <= 0
            for x in (strategy_capital, strategy_weight, account_weight))
            or max(strategy_weight, account_weight) > Decimal('.02')):
        raise ValueError('invalid independent daily risk limits')
    if strategy_pnl is None or not strategy_pnl.is_finite():
        raise ValueError('LONG risk UNKNOWN: exact bot P&L attribution required; no new BUY')
    strategy_budget = strategy_capital * strategy_weight
    if strategy_pnl <= -strategy_budget:
        raise ValueError(f'LONG daily loss gate: pnl={strategy_pnl}; budget={strategy_budget} RUB')
    if account_risk is None:
        raise ValueError('IIS risk UNKNOWN: valid BOD and reconciled flows required; no new BUY')
    account_risk.validate(account, equity, now)
    budget = account_risk.opening_equity * account_weight
    if account_risk.pnl <= -budget:
        raise ValueError(f'IIS daily loss gate: pnl={account_risk.pnl}; budget={budget} RUB')


def unused_strategy_pnl(db: sqlite3.Connection, account: str, identity: str) -> Decimal | None:
    """Only the enrolled, completely unused first-entry journal proves a zero bot result.

    This is deliberately NOT a P&L estimator from order limits or current account yield.
    Subsequent entries require a separate, fully reconciled fill/commission attribution.
    """
    meta = dict(db.execute('SELECT key,value FROM metadata'))
    if (meta.get('live_account_id') != account or meta.get('live_journal_uuid') != identity
            or not identity or meta.get('migration_state') != 'ready'):
        raise ValueError('LONG risk: journal enrollment mismatch')
    return None if db.execute('SELECT 1 FROM actions LIMIT 1').fetchone() else Decimal(0)


def validate_basis(raw: Mapping[str, Any], account: str, now: datetime) -> None:
    if raw.get('protocol') != PROTOCOL or raw.get('account_sha256') != account_hash(account):
        raise ValueError('IIS risk: wrong baseline protocol/account')
    observed = datetime.fromisoformat(str(raw['observed_at']))
    started = datetime.fromisoformat(str(raw['started_at']))
    if (now.tzinfo is None or observed.tzinfo is None or started.tzinfo is None
            or not started <= observed <= now or observed-started > timedelta(seconds=20)
            or observed.astimezone(MSK).date() != now.astimezone(MSK).date()):
        raise ValueError('IIS risk: invalid baseline observation time')
    midnight = datetime.combine(observed.astimezone(MSK).date(), time.min, MSK)
    if not midnight <= started <= observed <= midnight+timedelta(seconds=60):
        raise ValueError('IIS risk: no start-of-day baseline; never backdate a current snapshot')
    if number(raw['equity']) <= 0 or not isinstance(raw.get('inventory'), dict):
        raise ValueError('IIS risk: invalid baseline valuation')
    number(raw['rub_cash'])
    contents = {k: v for k, v in raw.items() if k != 'sha256'}
    if raw.get('sha256') != hashlib.sha256(canonical(contents)).hexdigest():
        raise ValueError('IIS risk: baseline checksum mismatch')


def trusted_path(directory: Path, account: str, now: datetime) -> Path:
    for p in (directory, *directory.parents):
        info = p.lstat()
        if (not stat.S_ISDIR(info.st_mode) or p.is_symlink()
                or (os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o022))):
            raise ValueError('IIS risk: untrusted baseline directory')
    return directory / f'{now.astimezone(MSK).date()}-{account_hash(account)}.json'


def load_basis(directory: Path, account: str, now: datetime) -> dict[str, Any]:
    path = trusted_path(directory, account, now)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or info.st_size > 1_000_000
            or (os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o027))):
        raise ValueError('IIS risk: untrusted baseline file')
    raw: dict[str, Any] = json.loads(path.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError('IIS risk: invalid baseline schema')
    validate_basis(raw, account, now)
    return raw


def store_basis(directory: Path, account: str, raw: dict[str, Any], now: datetime) -> Path:
    """Root-only immutable first observation. No directory creation, no late re-baselining."""
    validate_basis(raw, account, now)
    path = trusted_path(directory, account, now)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o640)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as out:
            out.write(canonical(raw))
            out.flush()
            os.fsync(out.fileno())
        if os.name != 'nt':
            chown, chmod = getattr(os, 'fchown', None), getattr(os, 'fchmod', None)
            if chown is None or chmod is None:
                raise ValueError('POSIX ownership primitives unavailable')
            chown(fd, 0, directory.stat().st_gid)
            chmod(fd, 0o640)
    finally:
        os.close(fd)
    if os.name != 'nt':
        folder_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(folder_fd)
        finally:
            os.close(folder_fd)
    return path
