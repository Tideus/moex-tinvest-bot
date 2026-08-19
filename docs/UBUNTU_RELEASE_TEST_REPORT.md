# Ubuntu 24 deployment — отчёт проверки

Дата проверки: 2026-08-20.

## Итог

`PASS_WITH_ENVIRONMENT_LIMIT`: deployment-пакет готов для постоянного `replay/shadow`,
дневной/недельной отчётности, часовых long Sandbox-заявок и отдельного пятиминутного intraday
Sandbox-контура на Ubuntu Server 24.04 LTS. Production execution намеренно отсутствует.

## Пройденные проверки

- Ruff: PASS.
- mypy strict: PASS, 36 source files.
- pytest: 138 PASS.
- Coverage: 76.60%, gate 75%.
- Все 15 project/example JSON-конфигов: PASS.
- Реальный historical MOEX backtest: PASS технически; production gate закономерно BLOCKED.
- Сохранение исходных OHLCV, сделок, equity CSV/HTML и promotion assessment: PASS.
- Shadow config preflight: PASS.
- T-Invest endpoint/environment resolver: PASS, `sandbox`; production orders disabled.
- Sandbox execution interlock, UUID, order cap, stale/account/open-order gates: PASS.
- Sandbox short: signed targets, close-before-reverse, per-name/gross caps, dynamic T-Invest
  short flag and explicit margin confirmation: PASS.
- Intraday TradeStats/OrderStats/OBStats exact-interval join, signal gates, deduplication and
  capital/turnover/position limits: PASS.
- Dedicated intraday account resolution, cancel/reconciliation, limit entries, mandatory market
  flattening and sandbox-only interlocks: PASS.
- Five-minute intraday systemd service/timer and isolated artifacts/state: PASS.
- Compact Telegram policy, operation-id fill deduplication and separate intraday daily report:
  PASS.
- OperationsByCursor parser, daily turnover и P&L aggregation: PASS.
- Daily/Friday report runner and structured JSON companion: PASS.
- Russian Trusted CA assets: PASS, five PEM files verified by committed SHA-256 manifest.
- Ubuntu CA staging: PASS, install/update scripts route certificates to the system trust store.
- Live TLS contract: PASS, bundled RSA root validates `sandbox-invest-public-api.tbank.ru`.
- Deterministic replay: PASS, quality true, 3 targets, 3 dry-run orders.
- Telegram outbox health: PASS.
- Bash syntax для всех Ubuntu scripts: PASS.
- `DESTDIR` staging install: PASS.
- systemd unit/timer contract tests: PASS.
- Runner smoke с подменным transport/CLI: PASS.
- Healthcheck smoke: PASS.
- Backup archive + SHA-256 verification: PASS.
- CI расширен запуском полного deployment self-test на Ubuntu runner.

## Найденные и исправленные дефекты

- рекурсивное копирование staging-каталога внутрь пакета;
- некорректное склеивание `DESTDIR` и абсолютных путей;
- Windows/Git Bash permission issue в staging mode;
- backup checksum содержал непереносимый путь;
- healthcheck создавал ложные тревоги ночью/в выходные;
- systemd timers включались до заполнения секретов;
- `.env` не гарантированно загружался в фоновом Windows runner;
- runtime-переключатель находился в обновляемом `/opt` вместо сохраняемого `/etc`;
- update мог включить ранее выключенные timers после сбоя;
- отсутствовала проверка `dead/pending_due` Telegram outbox;
- geo collector мог не создать fail-closed payload до shadow run.

## Ограничение среды проверки

На рабочей машине зарегистрирован `Ubuntu-24.04` WSL, но его `ext4.vhdx` отсутствует, поэтому
запуск `systemd-analyze verify` именно в локальной Ubuntu был невозможен без разрушительной
переустановки пользовательского WSL-дистрибутива. Вместо этого выполнены:

- Linux-compatible Bash syntax/staging/runtime smoke;
- контрактные тесты unit/timer-файлов;
- CI self-test на `ubuntu-latest` добавлен в workflow.

После фактической установки инструкция требует выполнить `systemd-analyze verify` и первый
`systemctl start` до включения timers. Скрипт `activate.sh` блокирует активацию, если первый
shadow cycle не прошёл.

## Не проверялось без внешних секретов

- реальный ALGOPACK entitlement и закрытые датасеты конкретного аккаунта (публичные дневные
  свечи MOEX для backtest проверены);
- реальная доставка Telegram;
- реальный ответ Sandbox PostOrder/OperationsByCursor конкретного аккаунта;
- реальный ответ трёх ALGOPACK SuperCandles endpoints и end-to-end intraday cycle с секретами;
- реалистичность fill/slippage Sandbox (песочница её не обеспечивает);
- production orders — отсутствуют и запрещены.
