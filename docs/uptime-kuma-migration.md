# План перехода на Uptime Kuma

Статус: проектирование и шаблоны; работающий мониторинг 0.6.1 не отключён.
В 0.6.3 выполнен первый кодовый этап: collectors/config/incidents отделены от CLI,
SQLite reads переведены в mode=ro, snapshot публикуется атомарно с schema_version=1,
добавлен --collect-only без incident/outbox writes. Отдельный agent deployment,
Push exporter, Kuma, notification relay и SMTP fallback ещё не реализованы.
Канал технических алертов: Moex Алерты, TELEGRAM_ALERT_CHAT_ID=-1004295022477;
адрес торговых отчётов не меняется. Пока используется существующий Telegram-токен.
Новые контейнеры, DNS, nginx и каналы уведомлений не устанавливались.

## 1. Разделение ответственности

```text
VM A: vivek                         VM B: отдельный monitoring-хост
торговый бот → audit snapshots      nginx/TLS → Uptime Kuma
  │ только сделки и отчёты TG         ↑ push отдельных метрик
  └ локальные risk gates           observer-agent на VM A
                                       │
                                   Kuma webhook → alert-relay → Telegram
                                                          └→ SMTP fallback

Внешний watchdog / узел C → контроль доступности VM B и heartbeat relay
```

Рекомендуется VM B у другого провайдера/в другой зоне отказа. На том же vivek
можно сделать пилот, но он НЕ обнаружит полное отключение vivek собственными силами.

| Компонент | Ответственность | Чего не делает |
|---|---|---|
| Торговый бот | Сигналы, риск, заявки, reconciliation, сделки, дневные/недельные отчёты | Не решает, кому отправлять технический алерт |
| observer-agent | Диск/inode/память, systemd, API, freshness/quality, backup; отправка измерений | Не размещает заявки, не меняет runtime, не чистит диск |
| Uptime Kuma | Интервалы, UP/DOWN, повторы, maintenance windows, история доступности | Не считает стратегию и бухгалтерию позиций |
| alert-relay | Durable очередь доставки, Telegram → SMTP, дедупликация, recovery | Не определяет состояние торгового риска |
| Внешний watchdog | VM B/Kuma/relay целиком недоступны | Не зависит от VM A и VM B |

Kuma поддерживает HTTP/TCP/Push и Telegram/SMTP. Список возможностей:
https://github.com/louislam/uptime-kuma/blob/master/README.md

## 2. Что переносим из кода

Вынести collectors из src/moex_bot/monitoring.py в отдельный пакет/service observer-agent
со своим venv, пользователем, конфигурацией и журналом. Сохранить независимые unit-тесты.
Заменить общий collect try/except на изолированные probes: ошибка одной метрики
не должна отменять уже собранные результаты остальных.

Из бота наружу публиковать только атомарный sanitised snapshot:
schema_version, generated_at, last_cycle, last_quality_ok, last_reconciliation,
unresolved_orders, execution_block_reason, outbox_health. Без account ID, токенов,
подробностей портфеля и исходных новостей. Старую схему версионировать.

Agent читает snapshots read-only, а не импортирует торговый cli.py и не открывает
брокерский счёт для записи. systemctl show — без sudo. Отдельные read-only API-токены.
Файлы агента и secrets: root:observer, 0640; snapshots — только чтение.
Все вызовы API ограничены timeout, retry/backoff и бюджетом запросов.

Локальные risk gates, fail-closed запрет входов и защитные выходы остаются в боте:
доступность Kuma/Telegram не должна определять возможность закрыть позицию.

## 3. Матрица мониторов

| Группа | Способ | Интервал / ожидание | Алерт |
|---|---|---|---|
| VM A | TCP SSH или private HTTP из VM B | 60 с, 3 сбоя | Critical |
| observer-agent heartbeat | Push | 60 с, grace 180 с | Critical |
| Диск / inode | Push на каждую FS/метрику | 60 с; warn <15%, critical <8% или <2 GiB; inode <5% | Warning/Critical |
| Память | Push | 60 с; available <5% устойчиво 5 минут | Warning |
| Все 7 timers | Push | 60 с; enabled/active/next; expected state из конфига | Critical для trading/health |
| Services | Push | 60 с; last result, running duration | Critical при fail/stuck |
| Long/intraday | Отдельные Push | 60 с; cycle age, quality age, reconciliation age | По активной сессии |
| Broker exposure | Push | после reconciliation; no overnight, unresolved orders | Critical |
| MOEX ISS, 3 ALGOPACK, T-Invest sandbox/prod, calendar | Push с VM A | 300 с; auth/schema/freshness/latency | 2 сбоя, 401/403 отдельно |
| RSS ЦБ/MOEX | Push с VM A | 300 с; HTTP/XML/freshness | Warning, не скрывать текущий сбой MOEX RSS |
| Calendar | Push | 15 мин; refresh age <26 ч, ambiguity | Warning |
| Trade-report outbox | Push | 60 с; oldest pending, dead, last success | Warning/Critical |
| Daily/weekly reports | Push | по deadline из расписания, а не просто uptime timer | Warning |
| Backup | Push | 15 мин; last verified backup + restore drill age | Warning |
| nginx TLS | HTTPS из независимого узла | 60 с; expiry 30/14/7 дней | Warning/Critical |
| Kuma / relay / heartbeat queue | Внешний watchdog | 60 с, grace 180 с | Email вне VM B |

Push посылается каждые N секунд и при UP, и при DOWN. Один общий heartbeat не
заменяет отдельные метрики: живой агент может видеть сломанный API. Отсутствие push
также означает DOWN. Если конфигурация Push Kuma задаёт только общий интервал,
grace реализовать интервалом монитора и retries, проверить фактическую задержку.

Нерабочая сессия = planned maintenance/N/A по биржевому календарю, а не DOWN.
Для интрадей расписание deadline/закрытия должно браться из торгового конфига и
биржевого календаря. Жёсткие часы текущего мониторинга при миграции убрать.

## 4. Telegram и email

Торговые отчёты остаются в существующем Telegram/outbox бота. Технические алерты —
отдельный Telegram-бот и отдельный чат/тема; отдельный SMTP-аккаунт у независимого
почтового провайдера, не SMTP на vivek. Получатель — личный email пользователя.

### Первый безопасный этап

В Kuma настроить два нативных notification channels: Telegram и SMTP. Critical
посылать одновременно в оба. Это резервирование, НЕ условный fallback.

### Целевое поведение: условный email fallback

Нельзя предполагать, что Kuma автоматически переключит произвольные notification
providers при неудаче. Использовать Kuma Webhook (JSON POST) → внутренний alert-relay.
Официальный provider поддерживает heartbeat/monitor/msg и дополнительные заголовки:
https://github.com/louislam/uptime-kuma/blob/master/server/notification-providers/webhook.js

Relay — небольшой отдельный сервис на VM B в приватной docker-сети, без публичного
порта. Kuma URL: http://alert-relay:8080/kuma, Authorization: Bearer <relay secret>.
Этот сервис пока НЕ реализован; compose в этом пакете поднимает только Kuma.

Требования к relay:

1. Проверить auth/schema, записать инцидент и исходное событие в durable очередь,
   лишь затем ответить Kuma HTTP 2xx. Если storage не работает — вернуть ошибку.
2. Ключ дедупликации: monitor_id + состояние + идентификатор/время события;
   repeats одного инцидента учитывать отдельно, DOWN/UP не склеивать.
3. Telegram timeout 5–10 секунд; 2 попытки в общем бюджете до 30 секунд.
   HTTP 401/403 или chat not found → сразу fallback. 429 → учесть Retry-After,
   но email не откладывать за пределы бюджета.
4. Telegram API ok=true означает приём сервисом, НЕ прочтение пользователем.
   При timeout результат может быть неизвестен: email всё равно отправить,
   в теме указать incident_id; возможный дубль предпочтительнее потери алерта.
5. Если Telegram не подтвердил приём — SMTP через TLS с обязательной проверкой
   сертификата: 465 implicit TLS либо 587 STARTTLS, в зависимости от провайдера.
6. SMTP отказал → сохранить pending, exponential retry, возраст очереди наружу.
   При падении обоих каналов независимый watchdog оповещает другим маршрутом.
7. Recovery доставить тем каналам, куда доставлялся DOWN, чтобы email-инцидент
   не остался «открытым», если Telegram восстановился раньше.
8. Никаких токенов, push URL и SMTP-паролей в сообщениях и access/error logs.

Пока relay не пройдёт тесты, не отключать одновременную SMTP-доставку critical.

## 5. Развёртывание Kuma и nginx

Шаблоны: deploy/monitoring/compose.yaml и deploy/monitoring/nginx/*.conf.
Устанавливать на VM B, НЕ выполнять текущие bot install/update.sh для этого стека.
Отдельный каталог /opt/moex-monitoring, локальное persistent хранилище ./data.
Не использовать NFS. Не монтировать docker.sock и каталоги/секреты торгового бота.

1. Выбрать отдельную VM, домен monitor.<ваш-домен>, A/AAAA записи и admin VPN/IP.
2. Выбрать актуальный стабильный релиз Kuma 2, протестировать, закрепить digest.
   В .env задать KUMA_IMAGE=louislam/uptime-kuma@sha256:<проверенный digest>.
   Не использовать автоматическое обновление latest/Watchtower.
3. `docker compose config --quiet`, создать data, запустить `docker compose up -d`.
   Порт 3001 опубликован ТОЛЬКО на 127.0.0.1. Firewall: 80/443, SSH только admin/VPN.
4. До публичного proxy создать admin через SSH tunnel к 127.0.0.1:3001,
   включить MFA. Не оставлять начальный setup доступным всему интернету.
5. Заменить monitor.example.com и TEST-NET IPs во всех nginx-шаблонах.
6. Установить HTTP-only kuma-bootstrap.conf как site, создать
   /var/www/letsencrypt; `nginx -t`, reload.
7. Получить сертификат: `certbot certonly --webroot -w /var/www/letsencrypt -d <домен>`.
   Проверить renew timer и deploy hook: nginx -t && systemctl reload nginx.
8. Заменить bootstrap на TLS kuma.conf, добавить 00-kuma-websocket.conf в conf.d.
   `nginx -t`, reload. Проверить WebSocket/socket.io, login, MFA.
9. UI и административные API доступны только admin/VPN IP; /api/push/ — только
   egress IP VM A. IPv6 разрешать явно; не создавать AAAA без работающей IPv6 ACL.
10. За CDN/load balancer не доверять X-Forwarded-For всех клиентов: добавить
    set_real_ip_from только для доверенных proxy. Данные шаблоны рассчитаны на
    прямой nginx без CDN. Push-токены секретны, URI не логируются.

Официальная настройка WebSocket reverse proxy:
https://github.com/louislam/uptime-kuma/wiki/Reverse-Proxy

Шаблоны nginx требуют домен, IP и сертификат; nginx -t на целевом хосте и HTTPS
проверки пока НЕ выполнены. Они не являются готовой конфигурацией текущего vivek.

## 6. Переход без потери алертов

1. Инвентаризация: ожидаемые timers, контуры, endpoints, deadlines, API budgets.
2. VM B + TLS + Kuma + Telegram/SMTP, проверка внешнего watchdog.
3. Реализовать agent/snapshot schema и отдельный service/timer; недоступность
   Kuma не должна возвращать ошибку торговому процессу или останавливать выходы.
4. Настроить мониторы вручную в UI, записать их ID/политику в версионный manifest
   БЕЗ push-токенов; секреты агента — только на сервере. Не полагаться на
   неофициальный provisioning API без проверки конкретной версии.
5. Shadow monitoring 3–7 дней: старый мониторинг активен, новые алерты в тестовый чат.
6. Реализовать relay, пройти failure injection, затем включить conditional fallback.
7. Переключить technical alerts на Kuma. Убрать notify из bot monitoring, старый
   healthcheck оставить только локальной диагностикой/risk readiness без Telegram.
   НЕ отключать trading risk gates и outbox торговых отчётов.
8. Обновить moex-botctl: trading start/stop не управляет мониторингом; отдельные
   monitor status/maintenance. Намеренный stop бота требует maintenance window Kuma.
9. Удалить старый incident ledger из active path только после backup, не удалять
   исторические данные. Проверить update/install/uninstall/disaster restore обоих стеков.

Rollback: вернуть прежний technical-alert handler, agent продолжает сбор;
Kuma alerts отключить/maintenance для устранения дублей. Торговые настройки неизменны.

## 7. Приёмочные испытания

- Остановить тестовый heartbeat: VM B сообщает DOWN в пределах заданного SLA.
- Отключить health.timer: независимый agent продолжает heartbeat и сообщает
  DOWN метрики timer:health; остановка самого agent обнаруживается отсутствием push.
- Искусственно подставить disk critical метрику (НЕ заполнять рабочий диск).
- API 401/403/429/timeout, malformed JSON, stale data, отсутствующие credentials
  только обязательного контура: отдельные состояния, другие probes продолжаются.
- Остановить тестовый timer, дать service fail/stuck: алерт, затем recovery.
- Недоступны Telegram DNS/HTTPS, неверный chat/token: письмо через SMTP в пределах 60 с.
- SMTP отказал вместе с Telegram: событие сохранено, retry после восстановления.
- Перезапуск relay после записи, но до отправки: уведомление не теряется.
- Полное выключение VM A; отдельно Kuma/VM B; отдельно agent: разные инциденты.
- Доступ к UI с постороннего IP запрещён; push с чужого IP/токена отклоняется;
  3001 снаружи закрыт; HTTPS/WebSocket и продление TLS работают.
- Restore Kuma data и relay queue на чистой VM без потери настроек/инцидентов.
- Подтверждение: во всех тестах торговые заявки и runtime не изменились.

## 8. До начала установки нужны решения

Отдельная VM (или согласованный временный пилот на vivek), домен, admin VPN/IP,
SMTP host/port/TLS и отправитель/получатель, отдельный Telegram-бот/чат,
провайдер внешнего watchdog. Пароли и токены не присылать в чат.
