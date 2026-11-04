# Пилот Kuma adapters

## Действующая схема (0.7.0rc5)

На развёрнутом сервере 3 группы и 16 проверок (первоначальные 7 описаны ниже
для истории). Server — диск/inode/RAM, Infrastructure — S3 и Syncthing.
MOEX Trading оставляет отдельные проверки SANDBOX, PROD, трёх потоков ALGOPACK,
ISS, календаря и двух получателей Telegram. Не объединять SANDBOX и PROD в
одну метрику, из которой невозможно понять, где проблема.

Push показывает число сбоев и число измеренных проверок. Одинаковая причина
выключенных таймеров выводится одной строкой, при этом статус остаётся DOWN.
Локальная БД календаря и доступность API календаря — разные проверки. Пропажа
одного Push-адресата не лишает остальные мониторы их очередного результата:
экспортёр пробует каждый адресат и возвращает ошибку по итогу. Перечень
мониторов, привязки уведомлений, история и секретные URL не меняются.

После обновления пакета сверить отдельную установленную копию экспортёра
в `/opt/moex-monitoring/kuma-exporter.py` с `scripts/ubuntu/kuma-exporter.py`
этого же релиза; обновлять только после резервирования и проверки старой копии.
Перезапускать для проверки только `moex-observer.service` (read-only) и при
изменении форматирования уведомлений — `moex-kuma-relay.service`. Не запускать
торговые таймеры ради проверки мониторинга. PR6 — отдельный ограниченный по датам
эксперимент; зелёный PR6 не означает, что штатные таймеры включены.

Установлены два отдельных сервиса, торговая логика не менялась:

- moex-observer.timer / .service — каждые 5 минут запускает read-only collect-only,
  отправляет 5 Push: heartbeat, Resources, Timers and services, API integrations,
  Trading data. Отсутствие heartbeat более 420 секунд Kuma считает отказом.
- moex-kuma-relay.service — принимает JSON POST от Kuma на приватном Docker gateway
  :9088/kuma, сохраняет уведомление в отдельную durable outbox и отправляет Telegram
  в TELEGRAM_ALERT_CHAT_ID=-1004295022477. Raw msg/URL/response body не пересылаются.

В UI настроены 7 monitors: пять Push, HTTP MinIO S3 и HTTP Syncthing GUI.
Оповещения подключены через Webhook, enabled по умолчанию и для существующих monitors.
Прямое редактирование БД Kuma не выполнялось: UI создал monitors; provisioning
читает только имена и push_token созданных активных Push-мониторов SQLite mode=ro.

Секреты: /etc/moex-observer/kuma-push.json, relay.env — root:moexbot 0640,
родитель 0750. Push URLs и Telegram token в Git/logs не сохраняются.
relay.env содержит только Telegram credentials и адреса приватной Docker-сети,
не брокерские токены. Данные очереди: /var/lib/moex-observer/relay.sqlite3.

Шлюз слушает только gateway сети moex-monitoring_default и принимает только
исходный IP контейнера Kuma. Если Docker меняет subnet/container IP, требуется
повтор provisioning + service restart. Это пилотная защита доверенной частной сети;
для другого monitoring-хоста нужен TLS + bearer/mTLS, не публикация :9088 наружу.

## Диагностика

```bash
sudo systemctl status moex-observer.timer moex-kuma-relay.service
sudo journalctl -u moex-observer.service -n 30 --no-pager
sudo journalctl -u moex-kuma-relay.service -n 30 --no-pager
```

Повтор установки: scripts/ubuntu/install-kuma-adapters.py --source <каталог файлов>.
Сначала должны существовать пять Push-мониторов с точными именами MOEX / ...,
SQLite Kuma и loopback HTTP 3001. Секреты не выводятся.

## Ограничения

- Старый мониторинг/алерты не отключены, возможны дубли в переходный период.
- Observer пока использует существующий bot venv, пользователя moexbot и broker
  env для read-only probes. Это разделение процессов, ещё не полная изоляция credentials.
- Collector mode=ro не изменяет содержимое БД, но его systemd namespace разрешает
  data directory RW для SQLite WAL sidecars. Целевой вариант — sanitised snapshots
  без чтения live DB и отдельный read-only API env/пользователь.
- Relay использует существующий SQLiteOutbox: ограниченные retry и dead-letter.
  Независимый контроль dead/oldest pending очереди relay ещё нужен до cutover.
- SMTP fallback пока НЕ реализован, так как SMTP/получатель не заданы.
- Kuma/relay находятся на той же VM: внешнего обнаружения полного отказа VM нет.
- Не прошёл полный power-off/reboot/restore drill. Старый health сохраняется.
- Переход завершать только после shadow-периода, тестов DOWN/UP/пропавшего heartbeat,
  настройки SMTP и внешнего watchdog. Не отключать защитные выходы бота.
# Маршрутизация API в Telegram

Общий API-статус и группа MOEX Trading сохраняются на dashboard. Для них
relay подтверждает webhook без повторной отправки DOWN/UP. Именованные
мониторы источников по-прежнему доставляют DOWN и восстановление.
Для ЦБ и риск-ленты MOEX relay отдельно отслеживает переходы двух ключей
из свежего monitoring-latest.json; состояние хранится рядом с очередью relay.
Нет начального UP, повторного DOWN или «восстановления» по отсутствующему
ключу. В алерты не включаются URL, тела ответов или секреты.

