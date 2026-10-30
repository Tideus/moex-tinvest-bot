# Мониторинг 0.6.1

## Рефакторинг 0.6.3 / подготовка Kuma

monitoring.py — совместимый CLI; monitoring_config.py — настройки;
monitoring_probes.py — сбор; monitoring_incidents.py — инциденты и outbox.
Сбой одной группы probes не стирает другие группы. Изоляция внутри каждой
группы остаётся отдельной задачей; это не полная независимость каждого probe.
Чтение календаря/outbox выполняется SQLite mode=ro, без CREATE/PRAGMA WAL.
`python -m moex_bot.monitoring --collect-only` собирает read-only метрики и
пишет только атомарный snapshot schema_version=1, не создавая инциденты/уведомления.
Это сетевые read-only probes, а не offline-режим.
Повтор snapshot не продвигает счётчик сбоев; стабильный event key защищает от
дублирования enqueue при сбое до commit журнала инцидентов.
Код торговли этим рефакторингом не изменён.

План и инфраструктурные шаблоны: [uptime-kuma-migration.md](uptime-kuma-migration.md).
Kuma/relay ещё не развёрнуты; старый мониторинг продолжает работать до переключения.

В 0.6.2 добавлен TELEGRAM_ALERT_CHAT_ID: monitoring, monitoring_test и health_failure
отправляются туда; торговые сообщения остаются в TELEGRAM_CHAT_ID. Токен пока общий.
Если отдельный адрес не задан, сохраняется прежний общий маршрут. Адрес алертов
проверяется отдельным API probe; неверный адрес не перенаправляется в чат сделок.

Выполняется каждым health.timer (по умолчанию каждые 15 минут), до старых проверок.
Модуль monitoring отделяет read-only probes, snapshot и persistent incident ledger.
При ошибках проверок не изменяет счета, заявки, контуры и не очищает диск.

## Метрики

- Диск: /, /var/lib/moex-tinvest-bot, /var/log/moex-tinvest-bot; свободные GiB/% и inode.
- Доступная память /proc/meminfo: предупреждение ниже 5%.
- Таймеры shadow, intraday, health, daily-report, maintenance, prod-long-observer:
  active, enabled, следующий realtime/monotonic запуск. Calendar проверяется, если включён.
- Result всех соответствующих служб; activating более 600 секунд считается зависанием.
- MOEX ISS, три ALGOPACK набора, T-Invest sandbox/prod (чтение списка счетов),
  календарный токен (если таймер включён), Telegram getChat и обе RSS-ленты геофильтра.
- Свежесть long/intraday артефактов по будням 10:25–19:00 МСК.
- Последний intraday broker snapshot: наличие позиции после 18:40 МСК.
  Это предупреждение по последнему снимку, НЕ гарантия текущей позиции при недоступном API.
- Возраст резервной копии, dead/pending_due очереди Telegram, свежесть календаря.

## config/monitoring.json

| Параметр | Смысл и значение по умолчанию |
|---|---|
| calendar_required | true: календарь обязателен, отключённый timer вызывает сбой |
| required_tinvest_environments | sandbox и prod: оба используемых контура; неиспользуемый удалить из списка, его токен не проверяется |
| disk_warn_pct | Warning ниже 15% свободного места |
| disk_critical_pct | Critical ниже 8% |
| disk_min_free_gib | Critical ниже 2 GiB независимо от процентов |
| failures_to_alert | 2 последовательных сбоя до сообщения; Critical немедленно |
| timeout_seconds | 10 секунд на HTTP запрос |
| repeat_hours | Повтор продолжающегося инцидента через 6 часов |
| intraday_max_age_seconds | Допустимый возраст артефакта 1200 секунд |
| long_max_age_seconds | Допустимый возраст артефакта 7200 секунд |
| backup_max_age_hours | Допустимый возраст резервной копии 192 часа |

Snapshot: /var/lib/moex-tinvest-bot/artifacts/monitoring-latest.json.
Ledger: /var/lib/moex-tinvest-bot/data/monitoring.sqlite3.
Сообщения идут через существующую durable outbox с retry. Секреты, URL Telegram
и текст исключений HTTP в метрики не включаются. При восстановлении — отдельный алерт.
Если Telegram недоступен, сообщение останется в очереди: мгновенная доставка невозможна.

```bash
sudo systemctl start moex-tinvest-health.service
sudo journalctl -u moex-tinvest-health.service -n 100 --no-pager
```

Тест очереди без аварии: в окружении moexbot с загруженным bot.env выполнить
`python -m moex_bot.monitoring --test-alert`, затем `moex-bot telegram-send`.

## Ограничения и независимый аудит

Необходим внешний watchdog на другом узле: HTTP heartbeat/Dead Man's Switch с
ожиданием сигнала каждые 15 минут и grace не менее 30 минут. Его URL/провайдер пока
не настроены. Без этого нельзя обнаружить полное выключение VM, остановку самого
health.timer, потерю сети и полный отказ systemd изнутри этой VM.

Текущие биржевые часы мониторинга консервативны: праздничный/выходной календарь
ещё не управляет ими. Проверка успешного HTTP не доказывает актуальность биржевых
цен или прибыльность. Глубокую проверку freshness/order reconciliation выполняет
торговый цикл; нужны также недельные проверки качества и фактических fills.

Аудит нашёл и исправил: ALGOPACK ExecStartPre блокировал обязательные выходы,
исключение загрузки данных не доходило до broker fallback, старые бары вытесняли
актуальную оценку при выходах, прямой exit пропускал ERR-алерт. Конфликтующие даты
одного отчётного периода теперь блокируют обновление вместо last-write-wins;
неподтверждённые повторным refresh события помечаются UNCONFIRMED.

Дальнейший рефакторинг: typed calendar repository, отдельный position ledger,
разделение большого cli.py на команды. Массовый рефакторинг исполнения перед
накоплением integration fixtures не рекомендуется.
