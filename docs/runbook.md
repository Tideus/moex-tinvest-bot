# Operator runbook

## Replay

```powershell
python -m moex_bot.cli replay --config config/replay.json --input examples/replay_snapshot.json
```

Review `artifacts/replay_result.json` and the adjacent JSONL audit file.

## Preflight

```powershell
python -m moex_bot.cli preflight --config config/shadow.json
```

This validates configuration only. It does not contact T-Invest or MOEX.

## Integration preflight

```powershell
python -m moex_bot.cli integration-preflight
```

The command reports only whether variables exist. It never prints their values. Keep tokens in
an untracked local `.env`; never pass them as CLI arguments.

## Read-only MOEX smoke test

```powershell
python -m pip install -e ".[integrations]"
python -m moex_bot.cli moex-snapshot --secid SBER --uid VERIFIED_T_INVEST_UID `
  --board TQBR --output artifacts/sber_hourly.json
```

Use `--require-token` for entitled ALGOPACK data. Without it, public ISS may be delayed. A failed
or stale snapshot blocks downstream trading; it must never fall back to guessed values.

## Hourly Windows task

The task runs once per hour at minute `05` in local Windows time. Since Moscow and Yekaterinburg
have a whole-hour offset, this is also minute `05` Moscow time.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_hourly_task.ps1
Get-ScheduledTask -TaskName MOEX-TInvest-Shadow-Hourly
```

Each run creates timestamped files under `artifacts/` and `logs/`. The Windows wrapper remains
shadow-oriented; automatic Sandbox execution is supported by the reviewed Ubuntu runner. To
replace an existing Windows task, inspect it first and then use `-Replace` explicitly.
The wrapper refreshes allowlisted primary-source RSS feeds before market analysis. Any unavailable
source makes the feed stale and therefore reduces new risk; it never silently reports `normal`.
RSS is polled every hour. Market analysis is conservatively skipped outside 07:05–23:05 Moscow
time on weekdays. Weekends and exchange-calendar exceptions remain skipped until a machine-readable
MOEX calendar adapter is added.

Current coverage includes official Bank of Russia press releases and MOEX risk-parameter news.
This is not a complete geopolitical intelligence feed: sanctions and issuer-specific primary
sources must be added and independently tested before restricted live operation.

## Incident response

1. Block new intents.
2. Preserve current audit logs and configuration hash.
3. Obtain broker truth through Portfolio, Positions, Orders and OperationsByCursor.
4. Reconcile orders, fills, cash and positions.
5. Never retry an unknown order solely because of a timeout.
6. Rotate a token if leakage is suspected.
7. Add a replay regression before resuming.
8. Require manual approval to leave the safe state.

## Sandbox execution

On Ubuntu, after a successful prelaunch:

```bash
sudo moex-botctl sandbox-enable --confirm-sandbox
sudo moex-botctl start
```

Emergency stop for new submissions:

```bash
sudo moex-botctl sandbox-disable
sudo moex-botctl stop
```

`sandbox-disable` does not cancel an already active broker order. Reconcile it before restarting.
Never retry an `unknown` order blindly. Review `sandbox-execution-*.json`, the broker account and
the service journal first.

## Production status

Production order execution is intentionally unavailable. Do not work around the interlock by
changing the mode string, endpoint or adapter wiring.

# Секреты и адреса сервисов

Секреты хранятся только в локальном `.env`; безопасный перечень имён находится в
`.env.example`. Адреса официальных серверов находятся в `config/services.json` и проверяются
по allowlist командой `python -m moex_bot.cli integration-preflight`.

Наличие `T_INVEST_PROD_TOKEN` и `T_INVEST_PROD_ACCOUNT_ID` не включает live-торговлю.

Переключение контура:

```powershell
python -m moex_bot.cli environment-set --environment sandbox
python -m moex_bot.cli environment-status
```

Для выбора production API замените `sandbox` на `prod`. Это меняет только сервер и набор
переменных окружения; разрешение выставлять реальные заявки остаётся выключенным.
