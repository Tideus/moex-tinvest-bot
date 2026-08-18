# Руководство пользователя MOEX + ALGOPACK + T-Invest bot

Полный справочник параметров JSON: [`CONFIG_REFERENCE_RU.md`](CONFIG_REFERENCE_RU.md).
Подробный алгоритм и разбор BUY/SELL: [`ALGORITHM_RU.md`](ALGORITHM_RU.md).
Целевая long/short модель, derivatives и weekly review:
[`MULTI_ASSET_STRATEGY_RU.md`](MULTI_ASSET_STRATEGY_RU.md).

## 1. Что бот умеет сейчас

Проект готов для `replay` и часового `shadow`: он читает завершённые свечи MOEX,
проверяет свежесть, рассчитывает momentum/trend, применяет GeoRisk и риск-лимиты, создаёт
виртуальные заявки, пишет audit trail и отправляет отчёты в Telegram через SQLite outbox.

ALGOPACK добавляет:

- `TradeStats`: исполненный агрессивный поток покупок/продаж;
- `FUTOI`: long/short физических и юридических лиц по фьючерсам;
- `HI2`: анонимную концентрацию участников.

Реальные заявки заблокированы. Переключатель API `sandbox/prod` не равен режиму исполнения:

| Понятие | Что меняет |
| --- | --- |
| `config/runtime.json`: `sandbox/prod` | Сервер T-Invest и пару token/account ID |
| `config/shadow.json`: `mode=shadow` | Поведение бота; реальные заявки не отправляются |
| `ExecutionMode.LIVE` | Сейчас запрещён кодом независимо от выбранного сервера |

## 2. Первичная установка

Откройте PowerShell в корне проекта:

```powershell
cd C:\Users\mtide\Documents\Codex\2026-08-14\z\outputs\moex-tinvest-bot
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,integrations]"
Copy-Item .env.example .env
```

Откройте локальный `.env` и заполните только нужные поля. Не отправляйте токены в Telegram,
чат, логи или git.

```dotenv
MOEX_APIKEY=
T_INVEST_SANDBOX_TOKEN=
# Это поле заполнит sandbox-bootstrap:
T_INVEST_SANDBOX_ACCOUNT_ID=
T_INVEST_PROD_TOKEN=
T_INVEST_PROD_ACCOUNT_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Для shadow достаточно MOEX/ALGOPACK и Telegram. T-Invest prod-токен сейчас не нужен.

### Создание и пополнение sandbox-счёта

В `.env` укажите `T_INVEST_SANDBOX_TOKEN`, но оставьте ID пустым. Затем выполните:

```powershell
python -m moex_bot.cli sandbox-bootstrap
```

При каждом таком запуске бот:

1. Получает список sandbox-счетов и проверяет сохранённый ID.
2. При отсутствующем/просроченном ID переиспользует открытый счёт с именем
   `moex-tinvest-bot` либо создаёт новый.
3. Атомарно сохраняет `T_INVEST_SANDBOX_ACCOUNT_ID` в локальный `.env`.
4. Показывает доступный рублёвый остаток и спрашивает сумму виртуального пополнения.

Введите `0`, чтобы не пополнять. Максимум одного пополнения — 30 000 000 ₽. Для CI, cron и
systemd используйте неинтерактивный режим, чтобы процесс не ожидал ввода:

```powershell
python -m moex_bot.cli sandbox-bootstrap --no-prompt
python -m moex_bot.cli sandbox-bootstrap --top-up 300000
```

Команда работает только с официальным sandbox REST endpoint; production-баланс она не меняет.
Sandbox-деньги виртуальные, а качество исполнения заявок не моделирует реальный рынок.

Проверить read-only снимок выбранного в `config/runtime.json` счёта вручную:

```powershell
python -m moex_bot.cli broker-portfolio-snapshot `
  --runtime config/runtime.json `
  --services config/services.json `
  --universe config/universe.json `
  --output artifacts/portfolio.json
```

Команда не выставляет заявки. Она сохраняет свободные/заблокированные рубли, broker equity,
позиции и число активных заявок. Неизвестная позиция вне universe приводит к `FAIL`.

## 3. Telegram

1. Создайте бота через `@BotFather`.
2. Напишите своему боту первое сообщение.
3. Получите `chat_id` безопасным локальным способом и запишите token/chat ID в `.env`.
4. Запустите preflight. Значения секретов команда не печатает.

```powershell
python -m moex_bot.cli integration-preflight
```

Telegram используется только для исходящих отчётов. Входящие команды не управляют заявками.

Отправляются два типа торговых отчётов:

- каждый час — цели, виртуальные BUY/SELL данного цикла и причины risk rejection;
- ежедневно в `23:20 Europe/Moscow` — сводка всех виртуальных BUY/SELL за день по SECID.

Дневная сводка дедуплицируется ключом даты, поэтому повторный запуск не создаёт второе сообщение.
Она описывает shadow-намерения, а не фактические fills T‑Invest.

Как читать часовой отчёт:

| Поле | Значение |
|---|---|
| `Цель 15%` | желаемая доля бумаги в расчётном портфеле, а не обещанная доходность |
| `Импульс +2,91%` | изменение последней завершённой часовой цены относительно пяти завершённых часовых свечей назад |
| `Цена` | закрытие последней завершённой часовой свечи; это не гарантированная цена исполнения |
| `Тренд` | средняя цена закрытия последних 20 завершённых часовых свечей |
| `Прошло риск-контроль` | намерение прошло лимиты денег, оборота, позиции, сектора и связанных рисков |
| `Отклонено` | стратегия выбрала бумагу, но risk-gate запретил действие по указанной причине |
| `Заявки у брокера` | незавершённые заявки из фактического T-Invest snapshot до начала расчёта |

`Целевой портфель` отвечает на вопрос «какие бумаги и веса выбрала стратегия».
`Виртуальные сделки` показывают только разницу между текущим и целевым портфелем, ограниченную
лотностью, `max_order_notional`, свободными деньгами и остальными risk-лимитами. Поэтому вес
15% при капитале 300 000 ₽ не означает, что один цикл создаст BUY на 45 000 ₽: текущий лимит
одной заявки может обрезать её до меньшей суммы.

Все суммы в часовом Telegram-сообщении расчётные. Фактические fill, комиссия и проскальзывание
появятся только после реализации execution/reconciliation контура; в режиме SHADOW их нет.

## 4. Выбор T-Invest контура

Проверить выбранный контур:

```powershell
python -m moex_bot.cli environment-status
```

Песочница:

```powershell
python -m moex_bot.cli environment-set --environment sandbox
```

Production API:

```powershell
python -m moex_bot.cli environment-set --environment prod
```

Выбор `prod` не разрешает реальные заявки. Для исследования и shadow оставляйте `sandbox`.

## 5. Безопасная последовательность запуска

### Шаг 1 — тесты

```powershell
python -m ruff check src tests
python -m mypy src
python -m pytest --cov=moex_bot --cov-fail-under=75
```

### Шаг 2 — deterministic replay

```powershell
python -m moex_bot.cli replay `
  --config config/replay.json `
  --input examples/replay_snapshot.json `
  --output artifacts/replay_result.json
```

Ожидайте `quality=True`. Рядом появится `replay_result.audit.jsonl`.

### Шаг 3 — конфигурация и интеграции

```powershell
python -m moex_bot.cli preflight --config config/shadow.json
python -m moex_bot.cli integration-preflight
python -m moex_bot.cli environment-status
```

Если планируется песочница, после добавления токена запустите `sandbox-bootstrap`, затем повторите
`integration-preflight --require tinvest_sandbox`.

### Шаг 4 — ручной shadow-цикл

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_hourly_shadow.ps1
```

Проверьте новые файлы в `artifacts/`, `logs/` и сообщение Telegram. Отсутствие ALGOPACK
entitlement не переключает бот на вымышленные данные: ошибка останется в логе.

### Шаг 5 — часовая задача

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_hourly_task.ps1 -Replace
Get-ScheduledTask -TaskName MOEX-TInvest-Shadow-Hourly
Get-ScheduledTaskInfo -TaskName MOEX-TInvest-Shadow-Hourly
```

Ожидаемый статус задачи — `Ready`, `LastTaskResult` после успешного запуска — `0`.

Задача использует локальное время Windows. Текущая регистрация корректна для Екатеринбурга и
Москвы, потому что смещение составляет целое число часов и минута `05` совпадает. На сервере в
другом часовом поясе расписание нужно пересчитать или перевести хост в московское время.

## 6. Где смотреть результаты

| Место | Содержание |
| --- | --- |
| `artifacts/hourly_shadow-*.json` | quality, GeoRisk, targets, виртуальные заявки и отклонения |
| `artifacts/*.audit.jsonl` | последовательный журнал решений и состояний |
| `artifacts/algopack_flow-*.json` | TradeStats, FUTOI, HI2 и текст отчёта |
| `logs/hourly_shadow-*.log` | ошибки интеграций и итог команд |
| `data/notifications.sqlite3` | pending/sent/dead Telegram-сообщения |
| Telegram | краткий операторский отчёт |

## 7. Как читать один часовой отчёт

- `quality=OK`: данные полные и не старше разрешённого лимита. `BLOCKED` означает, что
  рассчитывать новые цели нельзя.
- `geo=normal/elevated/high/critical`: внешний risk overlay. Он уменьшает целевые веса или
  блокирует затронутые бумаги; это не прогноз направления цены.
- `targets`: желаемые виртуальные веса после стратегии и GeoRisk.
- `orders`: только dry-run планы в текущем shadow-контуре.
- `rejected`: планы, остановленные риск-лимитами.
- `projected sector/risk-cluster exposure`: несколько нефтегазовых, металлургических или
  финансовых бумаг считаются совместной концентрацией, даже если лимит каждой позиции соблюдён.
- `TradeStats imbalance`: `(Σval_b − Σval_s)/(Σval_b + Σval_s)`. Выше `+0.10` — заметно
  преобладает агрессивный buy-flow; ниже `−0.10` — sell-flow; между ними — balanced.
- `FUTOI FIZ/YUR`: gross long/short и net фьючерсов. Это не позиции конкретного фонда.
- `HI2`: концентрация. Высокий HI2 не означает автоматически рост или падение.

## 8. Как оценивать качество бота

Отчёт одного часа и процент прибыльных сделок ничего не доказывают. Оценка проводится по
этапам и всегда после комиссий, bid/ask spread и моделируемого проскальзывания.

### Backtest/walk-forward

Сравнивайте с IMOEX, buy-and-hold и простой равновзвешенной моделью. Минимальный набор:

- CAGR/total return;
- volatility, Sharpe и Sortino;
- max drawdown и Calmar;
- turnover и полные торговые издержки;
- число сделок и expectancy;
- exposure и месяцы в денежных средствах;
- результат по годам и инструментам;
- падение качества от in-sample к out-of-sample.

Нельзя принимать стратегию, если результат держится на одной бумаге, одном году или одном
точном наборе параметров. Повторите расчёт при комиссиях ×2, slippage ×2/×3, задержке сигнала
на одну свечу и параметрах ±20%.

### Shadow: 4–8 недель

Еженедельно считайте:

- долю успешных часовых циклов;
- число stale/blocked/ошибочных циклов;
- расхождение виртуальной цены с последующей доступной ценой;
- моделируемые spread/slippage и turnover;
- число Telegram-сообщений `pending/dead`;
- стабильность сигналов и вклад каждой бумаги;
- результат стратегии против тех же бенчмарков на одинаковом периоде.

Перед дальнейшим этапом должны отсутствовать неизвестные/дублированные заявки, критические
ошибки и необъяснённые расхождения. Стратегия должна оставаться приемлемой в OOS и при
повышенных издержках. Песочница проверяет API-механику, но не прибыльность.

### Ограниченный live — только после отдельной доработки

Требуются официальный execution adapter, broker reconciliation, kill switch, защищённое
хранилище секретов, recovery drill и явное ручное разрешение. Начинать следует на отдельном
небольшом счёте без плеча и не масштабировать до анализа 30–50 реальных исполнений.

## 9. Нужен ли отдельный сервер

Для replay и разработки — нет. Для 4–8 недель shadow можно использовать текущий Windows-ПК,
если он включён, не спит, имеет стабильные интернет и время, а пользовательский контекст задачи
имеет доступ к `.env`.

Windows Task Scheduler не превращает выключенный или спящий компьютер в сервер. Настройка
`StartWhenAvailable` может запустить пропущенную задачу после включения, но пропущенные рыночные
снимки восстановить нельзя.

Для постоянного sandbox и особенно будущего live рекомендуется отдельная постоянно включённая
машина или VPS в доступной юрисдикции с хорошей связью с T-Bank/MOEX. Требования небольшие для
часового цикла: 1–2 vCPU, 2–4 GB RAM, 20–40 GB SSD обычно достаточно. Важнее ресурсов:

- автоматический restart и health checks;
- синхронизация времени;
- шифрование диска и секретов;
- ограниченный firewall и отдельный непривилегированный пользователь;
- ротация/архив логов, backup БД и мониторинг свободного места;
- Telegram heartbeat и alert при пропущенном цикле;
- размещение, соответствующее условиям T-Invest/MOEX и применимому законодательству.

Codex не должен работать постоянно и не должен находиться в realtime/order loop. Он полезен
офлайн для разработки, тестов, анализа недельных результатов и расследования инцидентов.
Постоянную работу обеспечивает детерминированный сервис и планировщик.

Готовые systemd-скрипты и пошаговая установка для Ubuntu 24.04 находятся в
`docs/UBUNTU_24_SERVER_RU.md`.

## 10. Ежедневный чек-лист оператора

1. Проверить последнее время и код результата Windows-задачи.
2. Проверить Telegram heartbeat и отсутствие `dead` сообщений.
3. Просмотреть `quality`, GeoRisk, stale/errors и отклонённые планы.
4. Сверить, что режим остаётся `shadow`, а `live orders` — `DISABLED`.
5. Не менять стратегию по итогам одного дня.
6. Сохранить и разобрать любой инцидент до следующего этапа.

Материал предназначен для исследования и не является индивидуальной инвестиционной рекомендацией.
