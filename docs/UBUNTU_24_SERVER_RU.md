# Установка MOEX/T-Invest bot на Ubuntu Server 24.04 LTS

## Граница готовности

Пакет предназначен для постоянного `replay/shadow`, наблюдения и явно разрешаемых заявок на
виртуальном T-Invest Sandbox. `LIVE`/production execution запрещён конфигурацией и кодом.
Выбор `prod` разрешает только чтение портфеля и отчётность; единственный mutation adapter
жёстко привязан к официальному sandbox-host.

## Рекомендуемый сервер

- Ubuntu Server 24.04 LTS x86_64;
- 2 vCPU, 4 GB RAM, 40 GB SSD;
- стабильный исходящий HTTPS/443 к T-Bank, MOEX, CBR и Telegram;
- синхронизация времени через `systemd-timesyncd`/chrony;
- SSH по ключу, запрет password login, firewall с закрытыми входящими портами кроме SSH;
- регион размещения должен соответствовать правилам провайдеров данных и законодательству.

Бот не открывает входящий HTTP-порт. Для его работы не нужны nginx и публичный домен.

## 1. Передача проекта

Скопируйте каталог проекта на сервер, например через `rsync`:

```bash
rsync -az --delete \
  --exclude .venv --exclude .env --exclude data --exclude artifacts --exclude logs \
  ./moex-tinvest-bot/ ubuntu@SERVER:/tmp/moex-tinvest-bot/
```

На сервере:

```bash
cd /tmp/moex-tinvest-bot
sudo bash scripts/ubuntu/install.sh
```

Установщик:

- ставит Python 3.12, venv, CA certificates, logrotate и rsync;
- создаёт системного пользователя `moexbot` без shell;
- копирует неизменяемый код в `/opt/moex-tinvest-bot`;
- создаёт state в `/var/lib/moex-tinvest-bot` и логи в `/var/log/moex-tinvest-bot`;
- создаёт `/etc/moex-tinvest-bot/bot.env` с правами `0640 root:moexbot`;
- проверяет SHA-256 и устанавливает официальный Russian Trusted CA bundle;
- устанавливает systemd units, но не включает timers до заполнения секретов и активации.

### Системные TLS-сертификаты

В проект включено полное содержимое двух Linux-архивов, на которые ссылается официальный гайд
T-Bank для ПК/Linux: два корневых и три выпускающих сертификата. Источники, сроки действия,
fingerprints сертификатов и SHA-256 архивов зафиксированы в
`deploy/ubuntu/certificates/README.md`. GOST-файлы из источника были в DER и преобразованы в PEM
без изменения X.509-сертификата.

`install.sh` перед установкой проверяет точные хеши `SHA256SUMS`, копирует каждый сертификат
отдельным `.crt` в `/usr/local/share/ca-certificates/` и выполняет `update-ca-certificates`.
Несовпадение хеша останавливает установку; проверка TLS никогда не отключается. Добавление CA
расширяет системное доверие Ubuntu, поэтому обновлять эти файлы можно только из проверенного
официального источника.

Runner-скрипты и сетевые systemd services задают `SSL_CERT_FILE` и `REQUESTS_CA_BUNDLE` равными
`/etc/ssl/certs/ca-certificates.crt`. Это важно для `httpx`, который иначе может выбрать отдельный
bundle `certifi` и не увидеть уже установленные системные T-Bank/Russian Trusted CA. Проверка
сертификата остаётся включённой.

Проверить установку:

```bash
ls -l /usr/local/share/ca-certificates/moex-tinvest-*.crt

openssl s_client \
  -connect sandbox-invest-public-api.tbank.ru:443 \
  -servername sandbox-invest-public-api.tbank.ru \
  -verify_return_error </dev/null 2>&1 | grep 'Verify return code'
```

Ожидаемый результат TLS-проверки: `Verify return code: 0 (ok)`.

## 2. Секреты

```bash
sudoedit /etc/moex-tinvest-bot/bot.env
sudo chown root:moexbot /etc/moex-tinvest-bot/bot.env
sudo chmod 0640 /etc/moex-tinvest-bot/bot.env
```

Заполните нужные значения. Для постоянного текущего shadow обязательны:

```dotenv
MOEX_APIKEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Для песочницы добавьте токен, а ID оставьте пустым — bootstrap заполнит его сам:

```dotenv
T_INVEST_SANDBOX_TOKEN=
T_INVEST_SANDBOX_ACCOUNT_ID=
T_INVEST_SANDBOX_LONG_ACCOUNT_ID=
T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID=
```

Не храните токены в `/opt`, git, shell history, unit-файлах или аргументах процесса.

Создайте/проверьте два отдельных Sandbox-счёта и доведите каждый до 300 000 ₽. Команда запускается
от root только для сохранения ID в защищённый `/etc`-файл; она жёстко использует Sandbox endpoint:

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

Команда создаёт/восстанавливает именованные `moex-tinvest-bot-long` и
`moex-tinvest-bot-intraday`, не создаёт дубликаты при повторном запуске и записывает legacy
`T_INVEST_SANDBOX_ACCOUNT_ID` равным long ID. После записи проверьте права:

```bash
sudo chown root:moexbot /etc/moex-tinvest-bot/bot.env
sudo chmod 0640 /etc/moex-tinvest-bot/bot.env
```

## 3. Проверка до первого запуска

```bash
sudo moex-botctl prelaunch
```

Команда сама загружает защищённый `bot.env`, проверяет права, JSON-конфиги, выбранный
T-Invest-контур, обязательные credentials, risk-конфиг и systemd units. Вывод разбит на
категории; каждая ошибка содержит причину и рекомендуемое действие. При любой ошибке запуск
блокируется и возвращается ненулевой exit code.

## 4. Активация и первый запуск

```bash
sudo moex-botctl start
```

`start` повторяет prelaunch, применяет расписание, запускает первые shadow и intraday cycles,
затем health-check, включает четыре timer (`shadow`, `intraday`, `health`, `daily-report`) и
показывает фактически назначенные следующие запуски. Вне intraday-окна первый intraday service
завершается успешным `SKIP`.

Полностью остановить расписание и текущие циклы, сохранив конфиги, артефакты и Telegram
outbox:

```bash
sudo moex-botctl stop
```

Команда отключает автозапуск `shadow`, `intraday`, `health` и `daily-report` timers. Повторный вызов
безопасен. Для возобновления выполните `sudo moex-botctl start`.

Перед каждым shadow-расчётом runner получает из выбранного контура T-Invest фактические свободные
и заблокированные рубли, broker equity, позиции и активные заявки. Снимок сохраняется как
`/var/lib/moex-tinvest-bot/artifacts/portfolio-<UTC>.json`. Если на счёте есть инструмент вне
`config/universe.json`, цикл останавливается: сначала добавьте и независимо проверьте его UID,
board и lot либо используйте отдельный счёт только для бота.

Получить такой снимок вручную одной командой, без выставления заявок:

```bash
sudo moex-botctl portfolio
```

Oneshot-сервис после успешного завершения будет `inactive (dead)` с кодом `0`; это нормально.
Постоянно активным должен быть timer:

```bash
systemctl list-timers 'moex-tinvest-*'
```

## 5. Расписание

Расписание и контур задаются в `/etc/moex-tinvest-bot/runtime.json`:

Полное описание всех файлов и параметров: [`CONFIG_REFERENCE_RU.md`](CONFIG_REFERENCE_RU.md).
Алгоритм и чтение решений: [`ALGORITHM_RU.md`](ALGORITHM_RU.md).

```json
{
  "t_invest_environment": "sandbox",
  "schedule": {
    "timezone": "Europe/Moscow",
    "shadow_on_calendar": "*-*-* *:05:00",
    "shadow_randomized_delay_seconds": 20,
    "daily_report_on_calendar": "*-*-* 23:20:00",
    "health_on_boot": "10min",
    "health_interval": "15min",
    "diagnostics_interval_seconds": 60
  }
}
```

`shadow_on_calendar` использует синтаксис systemd `OnCalendar`. После изменения выполните
`sudo moex-botctl prelaunch`, затем `sudo moex-botctl start`; второй вызов атомарно создаст
systemd drop-in и покажет итоговое расписание. Секреты в этот JSON добавлять нельзя.

По умолчанию shadow запускается каждый час в `HH:05 Europe/Moscow`. Python дополнительно
пропускает выходные и время вне консервативного окна 07:05–23:05 МСК. `Persistent=true`
запустит пропущенный timer после перезагрузки, но не восстановит утраченный рыночный снимок.

Healthcheck запускается каждые 15 минут и требует shadow artifact не старше двух часов.
Вне консервативного торгового окна freshness artifact не проверяется, поэтому ночь и выходные
не создают ложную тревогу. Конфиги и обязательные credentials проверяются всегда.

Отдельный `moex-tinvest-intraday.timer` запускается по рабочим дням каждые пять минут с 10:00
до 18:55 МСК. Внутренние границы из `config/intraday.json` строже: входы разрешены с 10:15 до
18:25, а с 18:35 — до окончания основной сессии — выполняется принудительное закрытие. Timer
имеет `Persistent=false`: после
перезагрузки старый рыночный интервал не воспроизводится как новый сигнал.

Проверка intraday:

```bash
systemctl status moex-tinvest-intraday.timer
sudo systemctl start moex-tinvest-intraday.service
sudo journalctl -u moex-tinvest-intraday.service -n 100 --no-pager
```

Артефакты находятся в `/var/lib/moex-tinvest-bot/artifacts/intraday-*`, накопленные завершённые
SuperCandles — в `/var/lib/moex-tinvest-bot/data/intraday.sqlite3`. Начало каждого цикла отменяет
оставшиеся активные заявки только выделенного intraday-счёта и повторно читает позиции. Поэтому
long-счёт этим reconciliation не затрагивается.

Telegram не повторяет полный поток циклов. Long отправляет только утренний анализ и вечерний
итог; intraday — только дедуплицированные исполненные broker operations и вечерний
`intraday-daily-performance-YYYY-MM-DD`. Политика задаётся в `config/notifications.json`, а
полные входы, признаки, конфиг, портфель и решения сохраняются независимо от неё.

В `23:20 Europe/Moscow` отдельный timer сопоставляет почасовые снимки broker equity с
исполненными sandbox-операциями. Он сохраняет `daily-performance-YYYY-MM-DD.txt` и отправляет
в Telegram начальный/конечный баланс, общий P&L, просадку и вклад каждой бумаги. Пополнения и
выводы исключаются из результата. В пятницу тем же запуском создаётся
`weekly-performance-MONDAY-FRIDAY.txt` с процессными метриками и консервативным выводом
`COLLECT_MORE`, `REVIEW_DATA`, `REVIEW_RISK`, `OBSERVE` либо `CONTINUE_OOS`.
Рядом с каждым `.txt` сохраняется полный структурированный `.json`; его и соответствующие
`shadow-*.json` следует передавать Codex для недельного разбора причин и проверки гипотез.
Тот же timer создаёт отдельные `.txt/.json` файлы intraday-итога из снимков выделенного счёта,
его broker operations и пятиминутных plan artifacts.

Sandbox не моделирует реальную ликвидность и маржинальные расходы, поэтому недельный вывод не
меняет параметры автоматически. Проверить timer:

```bash
systemctl status moex-tinvest-daily-report.timer
journalctl -u moex-tinvest-daily-report.service -n 50 --no-pager
```

Переключать T-Invest-контур на сервере нужно в сохраняемом `/etc`-конфиге:

```bash
sudo moex-botctl contour sandbox
# либо
sudo moex-botctl contour prod
```

Переключение контура само по себе не включает заявки. Чтобы после shadow-проверок разрешить
виртуальные сделки только на sandbox-счёте:

```bash
sudo moex-botctl sandbox-enable --confirm-sandbox
# немедленно вернуть расчётный режим
sudo moex-botctl sandbox-disable
```

При включении проверяются sandbox credentials. Следующий успешный часовой цикл может отправить
не более `sandbox_max_orders_per_cycle` лимитных заявок. Активная заявка, stale-план, ошибка
данных, несовпадение account ID или неопределённый статус блокируют дальнейшее исполнение.
Production orders отсутствуют.
После изменения снова выполните `prelaunch` и `start`, чтобы проверить соответствующую пару
token/account ID.

## 6. Каталоги

| Путь | Назначение |
| --- | --- |
| `/opt/moex-tinvest-bot` | код и venv, только root может изменять |
| `/etc/moex-tinvest-bot/bot.env` | секреты |
| `/etc/moex-tinvest-bot/runtime.json` | активный контур и расписание без секретов |
| `/var/lib/moex-tinvest-bot/artifacts` | shadow/flow/geo результаты |
| `/var/lib/moex-tinvest-bot/data` | SQLite Telegram outbox |
| `/var/log/moex-tinvest-bot` | отдельные логи циклов |
| `/var/backups/moex-tinvest-bot` | локальные backup-архивы |

## 7. Диагностика

```bash
sudo moex-botctl diagnose
```

Непрерывный диагностический цикл с повтором до `Ctrl+C`:

```bash
sudo moex-botctl diagnose --watch
```

Интервал берётся из `schedule.diagnostics_interval_seconds`. Временное переопределение:

```bash
sudo moex-botctl diagnose --watch --interval 30
```

Диагностика проверяет окружение, credentials, четыре timer, Telegram outbox и свежесть последнего
shadow artifact, а также состояние отдельного intraday timer. При ошибке показывает комментарий
и последние сообщения units.

Историческая проверка текущей стратегии на реальных дневных свечах MOEX запускается одной
командой:

```bash
sudo moex-botctl backtest
```

Результаты сохраняются в
`/var/lib/moex-tinvest-bot/artifacts/historical-backtest/`: исходные OHLCV, сделки, equity CSV,
интерактивный HTML-график, Markdown-отчёт и fail-closed gate допуска к production. Команда ничего
не покупает и не меняет Sandbox-счёт.

Если сервис упал:

1. Не переключайте `LIVE` и не повторяйте неизвестную заявку.
2. Сохраните journal, `/var/log/moex-tinvest-bot` и последний artifact.
3. Проверьте DNS, время, свободное место, права env-файла и entitlement ALGOPACK.
4. После исправления выполните `config-check`, healthcheck и ручной запуск.

## 8. Backup

```bash
sudo /opt/moex-tinvest-bot/scripts/ubuntu/backup.sh
sudo sha256sum -c /var/backups/moex-tinvest-bot/*.sha256
```

Скрипт сохраняет state/SQLite/artifacts, пишет SHA-256 и удаляет локальные архивы старше 30 дней.
Для production нужен ещё зашифрованный off-host backup и проверка восстановления. Секреты из
`/etc` намеренно не входят в архив состояния.

## 9. Обновление

Сначала сохраните backup и скопируйте новую проверенную версию проекта в `/tmp`:

```bash
sudo /opt/moex-tinvest-bot/scripts/ubuntu/backup.sh
cd /tmp/moex-tinvest-bot
sudo bash scripts/ubuntu/update.sh
```

Скрипт останавливает timers, обновляет код и зависимости, валидирует конфиги, выполняет
проверку и установку CA bundle через `update-ca-certificates`, затем `daemon-reload`, возвращает
timers и запускает один shadow smoke-cycle. При ошибке trap пытается вернуть timers в рабочее
состояние. Таким образом, для обновления сертификатов используется тот же `update.sh`; отдельное
ручное скачивание на сервер не требуется.

Повторно установить только сертификаты из уже развёрнутой версии можно командой:

```bash
sudo /opt/moex-tinvest-bot/scripts/ubuntu/install-ca-certificates.sh
```

## 10. Удаление

С сохранением секретов, state и логов:

```bash
sudo /opt/moex-tinvest-bot/scripts/ubuntu/uninstall.sh
```

Полное необратимое удаление:

```bash
sudo /opt/moex-tinvest-bot/scripts/ubuntu/uninstall.sh --purge
```

Перед `--purge` обязательно создайте и проверьте backup.

## 11. Эксплуатационный gate

Серверный shadow считается здоровым, если:

- четыре timers (`shadow`, `intraday`, `health`, `daily-report`) enabled/active;
- последний service result `success`;
- healthcheck проходит;
- новый artifact появляется каждый торговый час;
- Telegram outbox не накапливает `dead` сообщения;

Если health-check показывает `dead`, сначала исправьте доступ к Telegram, затем
повторно поставьте сохранённые сообщения в очередь и запустите доставку:

```bash
sudo moex-botctl telegram-recover
```

Команда последовательно выполняет requeue, доставку и итоговый health-check. Для старой
установленной версии без этой команды используйте эквивалент:

```bash
sudo -u moexbot bash -c '
set -a
source /etc/moex-tinvest-bot/bot.env
set +a
exec /opt/moex-tinvest-bot/.venv/bin/python -m moex_bot.cli outbox-retry-dead \
  --outbox /var/lib/moex-tinvest-bot/data/notifications.sqlite3
'

sudo systemctl start moex-tinvest-shadow.service
sudo systemctl start moex-tinvest-health.service
```

`outbox-health` выводит только служебные ключи, число попыток и безопасную
категорию последней ошибки; текст сообщений и секреты не печатаются.
- нет stale-data, дисковых, временных и permission-инцидентов;
- недельная оценка проводится по правилам `USER_GUIDE_RU.md`.

Материал предназначен для исследования и не является индивидуальной инвестиционной рекомендацией.
