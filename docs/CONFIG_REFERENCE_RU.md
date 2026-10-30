# Справочник конфигурации

Этот документ описывает все файлы каталога `config/`, их место на Ubuntu-сервере и влияние
каждого параметра. Секреты здесь не хранятся: токены и account ID находятся только в
`/etc/moex-tinvest-bot/bot.env`.

## Где находятся конфиги

| Файл | Серверный путь | Кто изменяет |
| --- | --- | --- |
| `runtime.json` | `/etc/moex-tinvest-bot/runtime.json` | оператор |
| остальные `config/*.json` | `/opt/moex-tinvest-bot/config/` | проверенное обновление кода |

Установщик создаёт полный `runtime.json`. При обновлении существующий файл не заменяется:
`runtime-normalize` добавляет только отсутствующие документированные поля, сохраняя выбранный
контур, пользовательские значения и неизвестные будущие расширения.

Проверка всех конфигов и текущего окружения:

```bash
sudo moex-botctl prelaunch
```

После изменения `runtime.json` примените настройки командой `sudo moex-botctl start`.

## `prod_runtime.json`, `prod_long.json`, `prod_shadow.json`

Это отдельный read-only контур ИИС. Он не заменяет основной `runtime.json` и не влияет на
Sandbox intraday. `prod_runtime.json` выбирает официальный production endpoint, но содержит
`sandbox_orders_enabled=false`. `prod_shadow.json` задаёт только расчёт виртуального canary-плана.

`prod_long.json` фиксирует безопасную границу первой фазы:

| Поле | Значение | Влияние |
| --- | --- | --- |
| `execution_stage` | `observer` | Любое другое значение отклоняется загрузчиком; реального adapter нет. |
| `account_id_env` | `T_INVEST_PROD_ACCOUNT_ID` | ID читается только из защищённого `bot.env`. |
| `account_type` | `ACCOUNT_TYPE_TINKOFF_IIS` | Контур привязан к проверенному типу счёта. |
| `manual_position_policy` | `protect` | Ручные позиции не входят в управляемый субпортфель. |
| `protected_secids` | `GAZP`, `LKOH`, `RAGR`, `TATNP` | Текущие ручные позиции ИИС; observer не создаёт намерения на их продажу. |
| `managed_secids` | пусто | До отдельного live-подтверждения бот не владеет реальными позициями. |
| `allowed_secids` | 20 SECID | Отдельный торговый allowlist; защищённые ручные бумаги в него не входят. |
| `canary.capital_limit_rub` | `40000` | Выделенный капитал production-пилота; остальной ИИС не является бюджетом бота. |
| `canary.max_order_notional_rub` | `10000` | Максимальный размер одной будущей canary-заявки. |
| `canary.max_daily_turnover_rub` | `20000` | Дневной лимит оборота, чтобы не развернуть весь пилот за одно утро. |
| `canary.max_orders_per_day` | `2` | Не более двух будущих заявок за день; код допускает настройку от 1 до 4. |
| `canary.max_account_daily_loss_weight` | `0.015` | Проектируемый account-level kill switch; observer только отображает план. |
| `canary.allow_margin` | `false` | Маржа и short запрещены. |
| `canary.allowed_asset_classes` | `["share"]` | Фьючерсы и опционы исключены. |

Любая ненулевая позиция production-счёта должна быть явно указана в `protected_secids` или
`managed_secids`; иначе цикл завершается ошибкой. Это предотвращает случайную торговлю поверх
ручного портфеля. Лимиты `prod_shadow.json` сверяются с canary-политикой в `config-check`.
Стратегия production observer получает только `allowed_secids`, даже если мастер-universe содержит
дополнительные бумаги для reconciliation. Текущий allowlist: `SBER`, `T`, `MOEX`, `NVTK`,
`GMKN`, `PLZL`, `PHOR`, `CHMF`, `YDEX`, `X5`, `MTSS`, `ROSN`, `VTBR`, `AFLT`, `ALRS`,
`MAGN`, `IRAO`, `SBERP`, `TATN`, `TRNFP`.

## `accounts.json`

Реестр независимых портфельных контуров. В нём нет реальных account ID: хранятся только имена
переменных окружения. Схема требует два Sandbox-профиля:

| Профиль | Назначение | Целевой баланс | Account ID в `bot.env` | Стратегии |
| --- | --- | ---: | --- | --- |
| `long` | дневной long-only портфель | 300 000 ₽ | `T_INVEST_SANDBOX_LONG_ACCOUNT_ID` | `daily_long_momentum_v1` → `shadow.json` |
| `intraday` | сделки внутри сессии | 300 000 ₽ | `T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID` | отдельные momentum и mean-reversion |

`order_execution_enabled` включён для обоих Sandbox-профилей. Общий аварийный выключатель long
исполнения — `runtime.json.sandbox_orders_enabled`; оба gate проверяются перед отправкой. Общий токен Sandbox остаётся
в `T_INVEST_SANDBOX_TOKEN`. Bootstrap также записывает long account в прежний
`T_INVEST_SANDBOX_ACCOUNT_ID`, чтобы действующий дневной runner сохранил совместимость.

Создать или восстановить оба счёта и довести каждый до целевого баланса:

```bash
sudo bash -c '
set -a
source /etc/moex-tinvest-bot/bot.env
set +a
exec /opt/moex-tinvest-bot/.venv/bin/python -m moex_bot.cli \
  sandbox-bootstrap-profiles \
  --accounts /opt/moex-tinvest-bot/config/accounts.json \
  --env-file /etc/moex-tinvest-bot/bot.env \
  --fund-targets
'
```

Повторный запуск использует сохранённый ID или открытый счёт с тем же именем и пополняет только
обнаруженный дефицит. При первой миграции старый `T_INVEST_SANDBOX_ACCOUNT_ID` переиспользуется
как long-счёт. Если старый и новый long ID различаются, bootstrap завершается ошибкой: сначала
нужно проверить позиции и заявки прежнего счёта.

## `intraday.json`

Отдельная схема работающего внутридневного Sandbox-движка. Production-значение отсутствует и
отклоняется валидатором.

| Параметр | Значение/диапазон | Влияние |
| --- | --- | --- |
| `execution_stage` | `research_only` или `sandbox` | Совпадает с gate профиля `intraday`; production запрещён. |
| `candle_minutes` | только `5` | Размер завершённого интервала TradeStats/OrderStats/OBStats. |
| `scan_interval_minutes` | только `5` | Частота systemd-цикла. |
| `session.new_entries_start_moscow` | `HH:MM` | До этого времени выполняется только мониторинг/reconciliation. |
| `session.new_entries_stop_moscow` | `HH:MM` | После этого времени новые позиции запрещены. |
| `session.force_flat_moscow` | `HH:MM` | Начиная с этого времени все позиции закрываются рыночными Sandbox-заявками. |
| `risk.max_capital_weight` | `(0,0.10]` | Совокупный капитал под intraday-позициями. |
| `risk.max_position_notional_rub` | положительная сумма | Максимальный номинал одного входа; сейчас 10 000 ₽. |
| `risk.max_concurrent_positions` | `1…2` | Одновременные позиции. |
| `risk.max_entries_per_day` | `1…3` | Новые сигналы, зафиксированные в SQLite за московский день. |
| `risk.max_daily_loss_weight` | `(0,0.01]` | При достижении убытка относительно первого equity дня новые входы блокируются, позиции закрываются. |
| `risk.max_daily_turnover_rub` | положительная сумма | Двусторонний оборот по операциям intraday-счёта. |
| `risk.stop_loss_bps` | `(0,1000]` б.п. | Закрывает открытую позицию market-заявкой при достижении заданного убытка от цены сигнала входа. |
| `risk.take_profit_bps` | `(0,1000]` б.п.; больше stop | Закрывает позицию при достижении цели прибыли. |
| `risk.max_holding_minutes` | `5…480` | Максимальное время удержания intraday-позиции независимо от P&L. |
| `risk.allow_short` | boolean | Разрешает short только при подтверждённом `shortEnabledFlag`. |
| `risk.allow_overnight` | только `false` | Перенос позиции запрещён. |
| `execution.order_type` | только `limit` | Входы — лимитные; обязательное закрытие использует market отдельно в коде. |
| `execution.order_ttl_seconds` | `5…300` | После отправки бот опрашивает финальный статус; незаполненный остаток отменяется сразу по истечении TTL. |
| `execution.cancel_if_signal_invalidated` | boolean | При подтверждённом противоположном сигнале открытая позиция закрывается market-заявкой. |
| `execution.chase_price` | только `false` | Валидатор запрещает переставлять вход вслед за ценой. |
| `execution.estimated_round_trip_cost_bps` | `(0,1000]` | Оценка комиссии и прочих двусторонних издержек для фильтра входа. |
| `execution.min_edge_after_cost_bps` | `(0,1000]` | Минимальный запас движения после вычета round-trip cost и текущего spread. |
| `signal.history_bars` | `3…12` | Число строго последовательных завершённых интервалов. |
| `signal.min_price_move` | `(0,1]` | Минимальное абсолютное движение цены за окно. |
| `signal.min_abs_trade_imbalance` | `(0,1]` | Порог исполненного потока `(Σval_b−Σval_s)/(Σval_b+Σval_s)`. |
| `signal.min_abs_order_flow` | `(0,1]`; сейчас `0.002` | Порог нормированного направления выставлений минус снятия заявок. Значение 0,002 означает 0,2%. |
| `signal.min_abs_book_imbalance` | `(0,1]` | Порог видимого дисбаланса стакана. |
| `signal.max_spread_bbo` | `(0,100]`; сейчас `3.0` | Максимальный `spread_bbo` ALGOPACK в базисных пунктах. Значение `3.0` означает 3 б.п., а не 3%. |
| `strategies[]` | именованный массив | Momentum включён; mean-reversion оставлен выключенным до отдельного OOS-теста. |

Каждый цикл выполняет только три пакетных запроса `latest=1` — по одному для TradeStats,
OrderStats и OBStats — и сохраняет объединённые интервалы в
`/var/lib/moex-tinvest-bot/data/intraday.sqlite3`. Сигнал создаётся только при совпадении
`SECID + tradedate + tradetime` во всех трёх наборах.

Это консервативные Sandbox-ограничения, а не доказанная прибыльная модель. Валидатор допускает только
`research_only` и `sandbox`; любое production-значение отклоняется. Кроме того, значения
`accounts.json.order_execution_enabled` и `intraday.json.execution_stage` обязаны совпадать.

## `notifications.json`

| Параметр | Допустимое значение | Влияние |
| --- | --- | --- |
| `timezone` | IANA timezone | Часовой пояс окон Telegram; по умолчанию `Europe/Moscow`. |
| `long.morning_analysis_hour` | `0..23` | Единственный час, когда long-анализ ставится в outbox. Остальные расчёты только сохраняются. |
| `long.evening_report_enabled` | boolean | Отправлять дневной P&L, баланс и вклад бумаг. |
| `intraday.notify_filled_operations` | boolean | Отправлять только новые broker BUY/SELL operations; `accepted` и планы не уведомляют. |
| `intraday.evening_report_enabled` | boolean | Отправлять отдельный intraday итог дня. |
| `audit.persist_every_cycle` | только `true` | Каждый цикл обязан оставить машинный след. |
| `audit.include_config_snapshot` | только `true` | Сохранять использованные параметры стратегии. |
| `audit.include_market_inputs` | только `true` | Сохранять свечи, SuperCandles и рассчитанные признаки. |
| `audit.include_portfolio_input` | только `true` | Сохранять баланс, позиции, блокировки и заявки до решения. |
| `audit.include_decisions_and_rejections` | только `true` | Сохранять цели, заявки и причины отказов risk-gate. |

Audit-флаги намеренно нельзя отключить: неполные данные делают недельную оценку модели
невоспроизводимой.

## `runtime.json`

Операторский конфиг контура и systemd-расписания. Секретов содержать не должен.

| Параметр | Тип/допустимые значения | Влияние |
| --- | --- | --- |
| `t_invest_environment` | `sandbox` или `prod` | Выбирает endpoint и соответствующую пару token/account ID. Сам по себе не включает заявки. |
| `sandbox_orders_enabled` | JSON boolean | Разрешает отправку прошедших risk-gate лимитных заявок только на sandbox-host. Для `prod` значение `true` запрещено. В поставляемом Sandbox-конфиге `true`; команда `sandbox-disable` является kill switch. |
| `sandbox_max_orders_per_cycle` | целое `1…10` | Жёсткий предел числа sandbox-заявок из одного часового плана. По умолчанию `3`. |
| `schedule.timezone` | IANA timezone, например `Europe/Moscow` | Часовой пояс выражения запуска shadow. |
| `schedule.shadow_on_calendar` | однострочное systemd `OnCalendar` | Когда создаётся очередной shadow-снимок. По умолчанию каждый час в `HH:05`. |
| `schedule.shadow_randomized_delay_seconds` | целое `0…3600` | Случайная задержка запуска, уменьшающая нагрузку ровно на границе часа. |
| `schedule.daily_report_on_calendar` | однострочное systemd `OnCalendar` | Время дневного Telegram P&L по broker equity и исполненным sandbox-операциям. В пятницу тот же запуск создаёт недельный отчёт. По умолчанию `23:20`. |
| `schedule.health_on_boot` | `30s`, `10min`, `1h` | Задержка первого health-check после загрузки сервера. |
| `schedule.health_interval` | `30s`, `15min`, `1h` | Интервал systemd health-check. |
| `schedule.diagnostics_interval_seconds` | целое `10…86400` | Пауза между циклами `moex-botctl diagnose --watch`. |

Пример полного файла находится в `config/runtime.json`. Для переключения контура используйте:

```bash
sudo moex-botctl contour sandbox
sudo moex-botctl contour prod
```

Управление исполнением sandbox выполняется командой, а не ручным редактированием:

```bash
sudo moex-botctl sandbox-enable --confirm-sandbox
sudo moex-botctl sandbox-disable
```

Первая команда проверяет активный sandbox-контур и credentials. Production-исполнение в этой
версии отсутствует независимо от значения runtime.

## `shadow.json` и `replay.json`

Оба файла имеют одну схему. `shadow.json` управляет серверным dry-run циклом, `replay.json` —
детерминированным воспроизведением исторического снимка. Ни один режим не отправляет live-заявки.

Десятичные суммы и доли рекомендуется задавать JSON-строками, как в примерах, чтобы не вносить
ошибки двоичного `float`.

| Параметр | Допустимое значение | Влияние |
| --- | --- | --- |
| `mode` | `shadow` или `replay` соответственно | Выбирает разрешённый execution adapter. `live` отклоняется. |
| `base_currency` | только `RUB` | Валюта cash, notional и лимитов MVP. |
| `max_data_age_seconds` | целое `> 0` | Блокирует весь цикл, если хотя бы одно наблюдение старше лимита. |
| `max_position_weight` | доля `(0, 1]` | Максимальная итоговая доля одного инструмента в equity. `0.15` означает 15%. |
| `max_order_notional` | конечная сумма `> 0` | Максимальный номинал одной заявки/намерения в рублях. |
| `max_gross_exposure` | доля `(0, 1]` | Максимальная суммарная long-экспозиция к equity после заявки. |
| `min_cash_reserve_weight` | доля `[0, 1]` | Минимальная доля equity, которая должна остаться свободными рублями после BUY. `0.10` сохраняет резерв 10%. |
| `max_sector_weight` | доля `(0, 1]` | Максимальная суммарная экспозиция бумаг одного инженерного `sector` к equity. |
| `max_risk_cluster_weight` | доля `(0, 1]` | Максимальная экспозиция одной коррелированной группы риска, например `hydrocarbons`. |
| `max_daily_turnover` | конечная сумма `> 0` | Максимальный накопленный дневной оборот намерений. Не меньше `max_order_notional`. |
| `min_trade_notional` | конечная сумма `> 0` | Изменения позиции дешевле порога не формируют заявку. Не больше `max_order_notional`. |
| `max_open_orders` | целое `> 0` | Блокирует новую заявку при достижении лимита незавершённых заявок. |
| `allow_margin` | JSON boolean | `true` разрешён только вместе с Sandbox short-стратегией; обычный BUY всё равно ограничен свободными деньгами и резервом. |
| `max_short_position_weight` | доля `(0, 1]` при shorts | Максимальный абсолютный вес одной короткой позиции. |
| `max_short_gross_exposure` | доля `(0, 1]` при shorts | Максимальная сумма абсолютных short-экспозиций к equity. |
| `live_interlock` | только `false` | Независимая блокировка live execution. `true` отклоняется до появления проверенного live adapter. |
| `strategy.top_n` | целое `> 0` | Максимальное число прошедших фильтр инструментов с наибольшим momentum. |
| `strategy.min_momentum` | конечное число `> -1` | Строгий нижний порог доходности: `0.01` означает больше +1%. |
| `strategy.require_above_trend` | JSON boolean | При `true` цена должна быть выше trend; при `false` этот фильтр отключён. |
| `strategy.candle_period` | `1D` для v2 | Таймфрейм завершённых свечей. Дневной период уменьшает внутридневной шум и оборот. |
| `strategy.momentum_windows` | массив положительных целых | Доходности на каждом окне усредняются; пример `[5,10,20]`. |
| `strategy.trend_window` | положительное целое | Число закрытий в простой средней для фильтра тренда. |
| `strategy.volatility_window` | целое `>= 2` | Окно дневных доходностей для оценки волатильности. |
| `strategy.volatility_floor` | конечное число `> 0` | Нижняя граница волатильности, защищающая inverse-vol расчёт от деления на почти ноль. |
| `strategy.inverse_volatility_weights` | JSON boolean | При `true` менее волатильные выбранные бумаги получают больший вес до применения лимитов риска. |
| `strategy.exit_rank_buffer` | целое `>= 0` | Удерживает уже купленную бумагу, пока её ранг не хуже `top_n + buffer`, снижая лишние перестановки. |
| `strategy.rebalance_hours_moscow` | массив уникальных часов `0…23` | В какие московские часы разрешено создавать заявки; в остальные часы выполняются анализ и отчёт без торговли. |
| `strategy.shorts_enabled` | JSON boolean | Включает отрицательные целевые веса; production это не открывает. |
| `strategy.short_top_n` | положительное целое | Максимальное число одновременно выбранных short-кандидатов. |
| `strategy.max_short_momentum` | отрицательное число | Для short momentum должен быть строго ниже порога, например `-0.03`. |
| `strategy.require_below_trend_for_short` | JSON boolean | При `true` short возможен только при цене ниже trend. |
| `strategy.long_target_gross` | доля `[0,1]` | Совокупный исходный бюджет long до остальных risk caps. |
| `strategy.short_target_gross` | доля `(0,1]` при shorts | Совокупный абсолютный бюджет short до caps. |
| `strategy.flow_confirmation_enabled` | boolean | Требует свежий 5-минутный TradeStats/OrderStats/OBStats для каждого нового long-кандидата; отсутствие данных блокирует утреннюю ребалансировку. |
| `strategy.flow_min_trade_imbalance` | `[-1,1]` | Минимальный дисбаланс исполненных покупок/продаж для нового long. |
| `strategy.flow_min_order_flow` | `[-1,1]` | Минимальный нормированный поток выставлений минус отмены для нового long. |
| `strategy.flow_min_book_imbalance` | `[-1,1]` | Минимальный дисбаланс видимой ликвидности для нового long. |
| `strategy.flow_max_spread_bbo` | `(0,100]` б.п. | Максимальный spread для нового long-входа. |

Адаптер считает blended momentum, trend и volatility только по завершённым свечам. На разрешённом
часе ребалансировки новые long-входы дополнительно проходят свежий ALGOPACK flow-фильтр; уже
открытая позиция не продаётся только из-за одного 5-минутного отрицательного бара. Инструменты
получают inverse-volatility либо равные исходные веса, после чего применяются лимит одной бумаги,
сектора, риск-кластера, денежного резерва и геополитический множитель.

Для flow адаптер постранично читает строки текущего торгового дня и для каждой бумаги выбирает
последнюю общую метку времени трёх наборов SuperCandles, не позднее времени запуска. Это важно,
поскольку наборы могут публиковаться с небольшой разницей во времени; соединение трёх независимых
`latest=1` ответов давало ложные пропуски.

## `backtest.json`

| Параметр | Влияние |
| --- | --- |
| `start_date`, `end_date` | Запрошенный исторический интервал; фактическое начало позже из-за прогрева окон. |
| `oos_start_date` | Неизменяемая граница финальной проверки, не используемая для подбора параметров. |
| `initial_cash` | Начальный модельный капитал в рублях. |
| `commission_rate` | Комиссия с каждой исполненной стороны сделки. |
| `half_spread_bps`, `slippage_bps` | Модельные односторонние издержки в базисных пунктах. |
| `short_financing_rate_annual` | Сценарная годовая ставка финансирования перенесённого short; начисляется по календарным дням между сессиями. Это не обещанная ставка брокера. |
| `cost_stress_multiplier` | Множитель всех издержек для стресс-проверки. |
| `benchmark_secid`, `benchmark_board` | Сравниваемый ценовой индекс; текущий `IMOEX/SNDX` не включает дивиденды. |
| `survivorship_safe` | Только evidence-флаг: `true` допустим лишь при point-in-time universe с выбывшими бумагами. |
| `dividends_included` | Только evidence-флаг: `true` допустим лишь после фактической обработки дивидендов. |

## `promotion_gates.json`

Это fail-closed критерии допуска к production: минимум OOS-сессий и сделок, Sharpe, excess над
benchmark, максимальная просадка, положительный стресс-результат, point-in-time universe,
дивиденды, длительность Sandbox и число сверенных заявок. Изменение порогов после просмотра OOS
не превращает провал в доказательство прибыльности.

Серверный цикл читает выбранный `sandbox`/`prod`-счёт перед каждым расчётом: свободные и
заблокированные рубли, позиции и активные заявки. Позиция вне point-in-time `universe.json`,
несовпавшая лотность или незавершённая загрузка лимитов блокирует цикл. В shadow-режиме
`daily_turnover` восстанавливается перед каждым циклом из исполненных BUY/SELL операций брокера
за текущие московские сутки. Ошибка чтения операций блокирует снимок и новый торговый цикл.
В часовом long-контуре наличие активной брокерской заявки блокирует все новые намерения. В
отдельном intraday-контуре перед расчётом выполняется cancel/reconciliation всех активных заявок
только выделенного счёта, после чего позиции и доступные средства читаются повторно.

## `maintenance.json`

Управляет ежедневной проверкой диска и ограниченной очисткой. Активные SQLite-базы и конфиги
никогда не входят в список удаления.
Рабочая серверная копия находится в `/etc/moex-tinvest-bot/maintenance.json` и не заменяется при
обновлении; `config/maintenance.json` служит полным шаблоном для новой установки.

| Поле | Значение по умолчанию | Влияние |
| --- | ---: | --- |
| `enabled` | `true` | Разрешает автоматическую очистку при достижении порога. |
| `warning_used_percent` | `75` | Уровень раннего предупреждения. |
| `cleanup_used_percent` | `80` | Запускает удаление файлов старше retention. |
| `critical_used_percent` | `90` | После очистки сервис возвращает ошибку, если диск всё ещё заполнен. |
| `minimum_free_bytes` | `2147483648` | Очистка также запускается при остатке не более 2 GiB. |
| `log_retention_days` | `14` | Минимальный возраст удаляемых логов. |
| `artifact_retention_days` | `90` | Минимальный возраст удаляемых артефактов стратегии. |
| `backup_retention_days` | `90` | Минимальный возраст удаляемых backup-архивов. |
| `keep_min_backups` | `3` | Последние три архива сохраняются независимо от заполнения диска. |

Пороги обязаны удовлетворять `warning < cleanup < critical`. До достижения cleanup-порога
retention ничего не удаляет.

## `services.json`

Allowlist официальных сетевых адресов.

| Параметр | Назначение |
| --- | --- |
| `t_invest.prod_grpc` | production gRPC T‑Invest |
| `t_invest.sandbox_grpc` | sandbox gRPC T‑Invest |
| `t_invest.prod_rest` | production REST T‑Invest |
| `t_invest.sandbox_rest` | sandbox REST T‑Invest |
| `t_invest.prod_websocket` | production WebSocket market data |
| `moex.iss` | публичный MOEX ISS |
| `moex.algopack` | авторизованный ALGOPACK/DataShop |
| `telegram.api` | исходящий Telegram Bot API |

Это не произвольные зеркала: загрузчик сравнивает host и path с утверждёнными официальными
endpoint и отклоняет подмену. Изменять файл следует только после проверки официальной миграции API.

## `universe.json`

Point-in-time список разрешённых инструментов. Список не может быть пустым, а `secid` не должны
повторяться.

| Поле | Влияние |
| --- | --- |
| `secid` | MOEX SECID для свечей, отчётов и идентичности позиции. |
| `board` | Режим торгов MOEX, например `TQBR`. |
| `t_invest_uid` | UUID инструмента T‑Invest; связывает MOEX и брокерскую идентичность. |
| `lot_size_verified` | Последний проверенный размер лота; должен быть положительным. Во время чтения данных сверяется с MOEX metadata. |
| `api_trade_available` | JSON boolean; `false` исключает инструмент и блокирует конфиг. |
| `short_enabled_verified` | Последняя проверенная возможность short. Перед увеличивающей Sandbox short-заявкой адаптер повторно проверяет официальный T-Invest `GetInstrumentBy`. |
| `issuer_id` | Стабильный инженерный ID эмитента; в дальнейшем объединит разные классы его бумаг и derivatives. |
| `sector` | Инженерная отраслевая группа для `max_sector_weight`; не является официальной классификацией MOEX. |
| `risk_cluster` | Группа общего риск-фактора для `max_risk_cluster_weight`, например углеводороды или внутренние финансы. |
| `asset_class` | В текущем universe только `share`; другие значения fail-closed отклоняются. |
| `isin` | Аудитный международный идентификатор; сейчас не участвует в расчётах. |
| `class_code` | Аудитный код класса T‑Invest/MOEX; сейчас не участвует в расчётах. |
| `verified_at` | Когда была проверена связка идентификаторов и лота. |
| `verification_source` | Источник проверки идентичности. |

Изменение universe требует повторной независимой проверки SECID/board/UID/lot. Нельзя копировать
UID по похожему названию бумаги.

Текущий мастер-список содержит 24 акции TQBR — только пересечение независимо проверенных
MOEX ISS и T‑Invest записей: `SBER`, `T`, `MOEX`, `LKOH`, `GAZP`, `NVTK`, `GMKN`, `PLZL`,
`PHOR`, `CHMF`, `YDEX`, `X5`, `MTSS`, `RAGR`, `TATNP`, `ROSN`, `VTBR`, `AFLT`, `ALRS`,
`MAGN`, `IRAO`, `SBERP`, `TATN`, `TRNFP`. RAGR и TATNP добавлены как
защищённые позиции ИИС по официальным данным T‑Invest. Для неоднозначных тикеров `T` и `X5` обязательны exact
UID, ISIN и `TQBR`; поиск только по тикеру запрещён.

## `geo_sources.json`

Allowlist RSS/XML-источников геополитического слоя.

| Поле | Ограничение и влияние |
| --- | --- |
| `source_id` | Непустой уникальный стабильный ID источника. |
| `url` | Только HTTPS и разрешённые домены CBR/MOEX. |
| `source_tier` | Сейчас только `primary`; используется в provenance события. |

Ошибка загрузки или устаревшая новостная лента включает консервативное снижение экспозиции.
Добавление произвольного новостного домена требует изменения и проверки кодового allowlist.

## `ownership_disclosures.json`

Локальный реестр проверяемых публичных раскрытий о крупных держателях и фондах. Пустой массив
`[]` допустим и означает отсутствие внесённых проверенных раскрытий.

| Поле | Тип и значение |
| --- | --- |
| `secid` | Непустой SECID эмитента. |
| `holder_name` | Непустое имя держателя/фонда. |
| `holder_type` | Тип держателя: фонд, государство, компания и т. п. |
| `stake_percent` | Доля `0…100` или `null`, если доля не раскрыта. |
| `report_date` | Отчётная дата `YYYY-MM-DD`. |
| `published_at` | Timestamp публикации с timezone; не может быть из будущего. |
| `source_url` | Прямая HTTPS-ссылка на раскрытие. |
| `source_kind` | Непустой тип первичного документа/раскрытия. |
| `verified` | JSON boolean; в отчёт выводятся только записи `true`. |

HI2 и FUTOI не раскрывают имена участников и не должны автоматически записываться сюда как
доказательство владения конкретного фонда.

## Проверка после изменения

```bash
sudo moex-botctl prelaunch
```

Статус `READY` означает, что структура, типы, диапазоны, endpoint allowlist, активный контур,
credentials и systemd units согласованы. Это проверка готовности системы, а не гарантия
прибыльности стратегии.
# Календарь событий

Параметры `config/event_calendar.json`, команды запуска и ограничения описаны в
[event-calendar.md](event-calendar.md). Режим только наблюдательный.

## Мониторинг

[Параметры config/monitoring.json, метрики и ограничения](monitoring.md).
