"""Immutable IIS day-opening observation and read-only separated risk report.

Never submits orders, creates grants, or changes a trading setting.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .daily_risk import BASIS_DIR, check_daily_limits, store_basis, unused_strategy_pnl
from .daily_risk_reader import baseline, read_risk
from .live_execution import registered_live_journal
from .prod_long import load_prod_long_policy
from .service_config import PROD_REST

POLICY = Path('/opt/moex-tinvest-bot/config/prod_long.json')
JOURNAL = Path('/var/lib/moex-tinvest-bot/execution/prod-orders.sqlite3')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['capture', 'report'])
    args = parser.parse_args()
    stage = 'configuration'
    try:
        from .prod_long_once_operator import ProductionTransport
        policy = load_prod_long_policy(POLICY)
        weight = policy.canary.max_strategy_daily_loss_weight
        if weight is None:
            raise ValueError('split daily risk policy is not enabled')
        account = os.environ['T_INVEST_PROD_ACCOUNT_ID'].strip()
        token = os.environ['T_INVEST_PROD_TOKEN'].strip()
        if not token or not account:
            raise ValueError('credentials missing')
        transport = ProductionTransport()

        def post(method: str, payload: Any) -> Any:
            if method not in {'OperationsService/GetPortfolio', 'OperationsService/GetPositions',
                              'OperationsService/GetOperationsByCursor'}:
                raise ValueError('daily risk is read-only')
            return transport.post(PROD_REST + '/tinkoff.public.invest.api.contract.v1.' + method,
                                  headers={'Authorization': 'Bearer '+token,
                                           'Content-Type': 'application/json'},
                                  payload=payload, timeout_seconds=4)

        def clock() -> datetime:
            return datetime.now(UTC)
        if args.action == 'capture':
            stage = 'BOD capture (first minute of Moscow day only)'
            raw = baseline(post, account, clock)
            store_basis(BASIS_DIR, account, raw, clock())
            print('PASS: immutable IIS BOD stored; no trading action')
            return 0
        result: dict[str, Any] = {'mode': 'READ_ONLY', 'LONG_budget_rub':
                                 str(policy.canary.capital_limit_rub*weight),
                                 'LONG_pnl_rub': None, 'IIS_budget_rub': None,
                                 'IIS_pnl_rub': None, 'errors': []}
        stage = 'LONG attribution'
        identity = registered_live_journal(JOURNAL, account)
        with closing(sqlite3.connect(JOURNAL.as_uri()+'?mode=ro', uri=True)) as db:
            pnl = unused_strategy_pnl(db, account, identity)
        if pnl is None:
            result['errors'].append('LONG: exact fill/commission attribution unavailable')
        else:
            result['LONG_pnl_rub'] = str(pnl)
        stage = 'IIS BOD/cash bridge'
        try:
            risk = read_risk(post, account, clock, BASIS_DIR)
            result.update(IIS_budget_rub=str(risk.opening_equity
                                            *policy.canary.max_account_daily_loss_weight),
                          IIS_pnl_rub=str(risk.pnl), IIS_BOD_equity_rub=str(risk.opening_equity),
                          external_flows_rub=str(risk.external_flows),
                          basis_at=risk.basis_at.isoformat())
            check_daily_limits(account=account, equity=risk.current_equity, now=clock(),
                               account_risk=risk, strategy_pnl=pnl,
                               strategy_capital=policy.canary.capital_limit_rub,
                               strategy_weight=weight,
                               account_weight=policy.canary.max_account_daily_loss_weight)
        except (OSError, ValueError, KeyError, ArithmeticError) as exc:
            reason = str(exc) if type(exc) is ValueError else type(exc).__name__
            result['errors'].append(stage+': '+reason)
        result['status'] = 'UNKNOWN' if result['errors'] else 'EVIDENCE_READY_NOT_A_GRANT'
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result['errors'] else 0
    except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as exc:
        reason = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(json.dumps({'status': 'UNKNOWN', 'stage': stage, 'reason': reason}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
