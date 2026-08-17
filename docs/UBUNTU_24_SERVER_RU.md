# Установка MOEX/T-Invest bot на Ubuntu Server 24.04 LTS

## Граница готовности

Этот пакет предназначен для постоянного `replay/shadow` и наблюдения. Он не выставляет реальные
заявки: `LIVE` запрещён конфигурацией и кодом. Выбор T-Invest `prod` лишь выбирает сервер и
production credentials для будущих read-only/preflight-интеграций.

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
```

Не храните токены в `/opt`, git, shell history, unit-файлах или аргументах процесса.

Создайте/проверьте sandbox-счёт и интерактивно задайте виртуальное пополнение. Команда запускается
от root только для сохранения ID в защищённый `/etc`-файл; она жёстко использует sandbox endpoint:

```bash
sudo bash -c 'set -a; source /etc/moex-tinvest-bot/bot.env; set +a; \
  exec /opt/moex-tinvest-bot/.venv/bin/python -m moex_bot.cli sandbox-bootstrap \
  --env-file /etc/moex-tinvest-bot/bot.env'
```

Для автоматической проверки без ожидания ввода используйте `--no-prompt`. Для явно заданного
виртуального пополнения используйте `--top-up 300000`. После записи проверьте права:

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

`start` повторяет prelaunch, применяет расписание, запускает первый shadow cycle и health-check,
включает оба timer и показывает фактически назначенные следующие запуски.

Oneshot-сервис после успешного завершения будет `inactive (dead)` с кодом `0`; это нормально.
Постоянно активным должен быть timer:

```bash
systemctl list-timers 'moex-tinvest-*'
```

## 5. Расписание

Расписание и контур задаются в `/etc/moex-tinvest-bot/runtime.json`:

Полное описание всех файлов и параметров: [`CONFIG_REFERENCE_RU.md`](CONFIG_REFERENCE_RU.md).

```json
{
  "t_invest_environment": "sandbox",
  "schedule": {
    "timezone": "Europe/Moscow",
    "shadow_on_calendar": "*-*-* *:05:00",
    "shadow_randomized_delay_seconds": 20,
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

Переключать T-Invest-контур на сервере нужно в сохраняемом `/etc`-конфиге:

```bash
sudo moex-botctl contour sandbox
# либо
sudo moex-botctl contour prod
```

Переключение контура не включает live orders. После изменения снова выполните `prelaunch` и
`start`, чтобы проверить соответствующую пару token/account ID.

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

Диагностика проверяет окружение, credentials, оба timer, Telegram outbox и свежесть последнего
shadow artifact. При ошибке показывает комментарий и последние сообщения units.

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

- оба timers enabled/active;
- последний service result `success`;
- healthcheck проходит;
- новый artifact появляется каждый торговый час;
- Telegram outbox не накапливает `dead` сообщения;
- нет stale-data, дисковых, временных и permission-инцидентов;
- недельная оценка проводится по правилам `USER_GUIDE_RU.md`.

Материал предназначен для исследования и не является индивидуальной инвестиционной рекомендацией.
