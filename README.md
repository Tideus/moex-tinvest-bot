# MOEX + ALGOPACK + T-Invest bot harness

Safety-first Python 3.12 scaffold for research, deterministic replay and shadow operation with
read-only T-Invest portfolio synchronization. It implements the control plane described in
`docs/plan` and deliberately does **not** implement live order submission.

Полная инструкция оператора на русском: [`docs/USER_GUIDE_RU.md`](docs/USER_GUIDE_RU.md).
Установка на Ubuntu Server 24.04 LTS: [`docs/UBUNTU_24_SERVER_RU.md`](docs/UBUNTU_24_SERVER_RU.md).
Все параметры JSON: [`docs/CONFIG_REFERENCE_RU.md`](docs/CONFIG_REFERENCE_RU.md).
Алгоритм и чтение BUY/SELL: [`docs/ALGORITHM_RU.md`](docs/ALGORITHM_RU.md).
Целевая long/short модель, derivatives и weekly review:
[`docs/MULTI_ASSET_STRATEGY_RU.md`](docs/MULTI_ASSET_STRATEGY_RU.md).

После серверной установки операционные команды сведены к одному интерфейсу:

```bash
sudo moex-botctl prelaunch
sudo moex-botctl start
sudo moex-botctl stop
sudo moex-botctl diagnose
sudo moex-botctl diagnose --watch
sudo moex-botctl portfolio
sudo moex-botctl decisions
```

Проверка Ubuntu deployment без изменения системы:

```bash
bash scripts/ubuntu/test-deployment.sh
```

## What works now

- immutable domain models using `Decimal`;
- instrument lot/tick validation;
- completed-candle quality gate;
- long-only momentum/trend target generation;
- deterministic GeoRisk reduction/blocking;
- independent pre-trade risk gate;
- execution-plan generation with stable idempotency keys;
- local order state machine and audit JSONL;
- replay CLI and preflight checks;
- read-only hourly MOEX snapshot command;
- idempotent T-Invest sandbox account bootstrap and optional RUB top-up;
- read-only sandbox/prod cash, equity, positions and active-order snapshots;
- cash-reserve, position-weight and gross-exposure diversification gates;
- 13 independently cross-checked TQBR shares with sector and correlated-risk limits;
- verified Russian Trusted CA bundle installed by Ubuntu install/update scripts;
- T-Invest sandbox-only REST adapter with timeout-to-UNKNOWN semantics;
- unit and integration-style replay tests;
- CI workflow.
- durable SQLite notification outbox with idempotent one-way Telegram delivery;
- entitled ALGOPACK TradeStats/FUTOI/HI2 flow reports;
- dated, source-linked ownership disclosure registry.

## Safety boundary

`ExecutionMode.LIVE` is denied by configuration validation and by the executor. No T-Invest mutation API is called. Adding real execution requires a separately reviewed adapter, sandbox verification, shadow evidence, a recovery drill and an explicit code/config change.
Production credentials may be configured for read-only shadow synchronization, but they do not
enable order submission.

## Quick start

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python -m moex_bot.cli replay --config config/replay.json --input examples/replay_snapshot.json
python -m moex_bot.cli preflight --config config/shadow.json
python -m moex_bot.cli integration-preflight
python -m moex_bot.cli sandbox-bootstrap
python -m moex_bot.cli hourly-shadow
python -m moex_bot.cli ownership-report --secid SBER --output artifacts/sber_ownership.md
```

The replay command writes an auditable result under `artifacts/`.

Service hosts are kept in `config/services.json`; secrets are kept only in a local `.env`.
Supported credential names are `T_INVEST_SANDBOX_TOKEN`/`T_INVEST_SANDBOX_ACCOUNT_ID` and
`T_INVEST_PROD_TOKEN`/`T_INVEST_PROD_ACCOUNT_ID`. Production credentials do not enable live
execution: the configuration and executor interlocks continue to deny `LIVE` mode.

Official T-Invest endpoints:

- prod gRPC: `invest-public-api.tbank.ru:443`;
- sandbox gRPC: `sandbox-invest-public-api.tbank.ru:443`;
- prod REST: `https://invest-public-api.tbank.ru/rest`;
- sandbox REST: `https://sandbox-invest-public-api.tbank.ru/rest`;
- prod WebSocket: `wss://invest-public-api.tbank.ru/ws/`.

The active API contour is selected in `config/runtime.json`. The committed default is
`sandbox`. Use the CLI instead of editing the file by hand:

```powershell
python -m moex_bot.cli environment-status
python -m moex_bot.cli environment-set --environment sandbox
python -m moex_bot.cli environment-set --environment prod
```

The switch chooses the endpoint and corresponding environment variables. Selecting `prod` does
not authorize order submission; `LIVE` remains blocked independently.

## Sandbox account bootstrap

Put only `T_INVEST_SANDBOX_TOKEN` in the local `.env`, then start the sandbox setup:

```powershell
python -m moex_bot.cli sandbox-bootstrap
```

The command checks the saved account ID against `GetSandboxAccounts`. If the account has expired
or the ID is absent, it reuses the bot's still-open named account or creates a new one, then
atomically writes `T_INVEST_SANDBOX_ACCOUNT_ID` to `.env`. It obtains available RUB cash through
`GetSandboxWithdrawLimits`, displays it and asks how much virtual cash to add. Enter `0` to skip.
For scripts and systemd, prompts are deliberately disabled explicitly:

```powershell
python -m moex_bot.cli sandbox-bootstrap --no-prompt
python -m moex_bot.cli sandbox-bootstrap --top-up 300000
```

`--top-up` changes only virtual sandbox money. The command is hard-wired to the official sandbox
host and cannot fund or mutate a production account. T-Invest limits one sandbox top-up operation
to a positive amount not exceeding 30,000,000 RUB.

Ubuntu installation and updates install the five CA certificates currently shipped in the two
official Linux archives linked by T-Bank. Exact PEM hashes, certificate fingerprints, validity and
source URLs are recorded in
[`deploy/ubuntu/certificates/README.md`](deploy/ubuntu/certificates/README.md). Installation fails
closed on any checksum mismatch and never disables TLS verification.

## Optional MOEX integration

Install the isolated integration profile and put `MOEX_APIKEY` only in a local `.env` when
entitled ALGOPACK data is required. Public ISS can work without a token but can be delayed.

```powershell
python -m pip install -e ".[integrations]"
python -m moex_bot.cli moex-snapshot `
  --secid SBER `
  --uid VERIFIED_T_INVEST_UID `
  --board TQBR `
  --output artifacts/sber_hourly.json
```

The UID must be independently verified against T-Invest before any sandbox test. The command
only reads MOEX data and cannot submit orders.

For the hourly flow layer, set `MOEX_APIKEY` locally and run:

```powershell
python -m moex_bot.cli algopack-flow `
  --secid SBER --futures-ticker SBERF `
  --output artifacts/sber_flow.json `
  --outbox data/notifications.sqlite3
```

`TradeStats` shows whether buyer- or seller-initiated executed value dominates; it does not
measure open shorts. `FUTOI` supplies aggregated long/short positions for individuals and legal
entities in futures. `HI2` measures anonymous concentration. See `docs/data-semantics.md`.

## Telegram reports

Create a bot with BotFather, send it one message, then store `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` only in local `.env`. The trading loop writes to SQLite first; delivery is a
separate command, so a Telegram outage cannot stop risk checks or create an order retry.

```powershell
python -m moex_bot.cli telegram-send --outbox data/notifications.sqlite3
```

The sender is outgoing-only: no Telegram command can place, replace, or cancel an order.

`hourly-shadow` performs one cycle and exits. Use Windows Task Scheduler or another supervisor to
invoke it hourly at `HH:05` Moscow time. It reads completed MOEX candles and writes only dry-run
intents. The runner first polls allowlisted official CBR and MOEX RSS feeds. If any feed fails or
the timestamp is older than two hours, exposure is automatically reduced. Keyword classification
is intentionally conservative and must be expanded with verified issuer and sanctions sources.

Register the included Windows task after creating `.venv`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_hourly_task.ps1
```

## Project map

```text
src/moex_bot/domain.py       money-safe immutable models
src/moex_bot/quality.py      freshness/completeness gate
src/moex_bot/strategy.py     long-only momentum baseline
src/moex_bot/geo.py          geopolitical risk policy
src/moex_bot/risk.py         independent pre-trade controls
src/moex_bot/execution.py    plans, idempotency, state machine
src/moex_bot/harness.py      deterministic orchestration
src/moex_bot/adapters.py     ports plus replay/no-live adapters
src/moex_bot/integrations/   read-only MOEX and sandbox-only T-Invest adapters
src/moex_bot/scheduler.py    Moscow-time hourly boundary calculation
src/moex_bot/shadow.py       one-shot hourly market/risk/audit pipeline
src/moex_bot/notifications.py durable SQLite outbox and Telegram sender
src/moex_bot/reporting.py    shadow and ALGOPACK report formatters
src/moex_bot/ownership.py    dated ownership disclosure registry
src/moex_bot/cli.py          replay and preflight CLI
tests/                       control and regression tests
docs/plan/                   full Russian project plan
docs/practices.md            global control-practice mapping
```

## Next integrations

1. Expand the verified instrument/futures mapping beyond SBER/SBERF.
2. Add the current official T-Invest SDK as a read-only/sandbox dependency after pinning its contract version.
3. Promote SQLite outbox to PostgreSQL before multi-process deployment.
4. Add historical point-in-time datasets and event replay.
5. Run sandbox mechanics tests, then 4–8 weeks of shadow mode.

Material is for research and is not individualized investment advice.

