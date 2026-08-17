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
- устанавливает systemd units, но не включает timers до заполнения секретов и активации.

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

Не храните токены в `/opt`, git, shell history, unit-файлах или аргументах процесса.

## 3. Проверка до первого запуска

```bash
sudo -u moexbot /opt/moex-tinvest-bot/.venv/bin/python \
  -m moex_bot.cli config-check --root /opt/moex-tinvest-bot

sudo -u moexbot /opt/moex-tinvest-bot/.venv/bin/python \
  -m moex_bot.cli integration-preflight \
  --services /opt/moex-tinvest-bot/config/services.json \
  --require moex_algopack --require telegram

sudo -u moexbot /opt/moex-tinvest-bot/.venv/bin/python \
  -m moex_bot.cli environment-status \
  --runtime /etc/moex-tinvest-bot/runtime.json \
  --services /opt/moex-tinvest-bot/config/services.json

sudo systemd-analyze verify \
  /etc/systemd/system/moex-tinvest-shadow.service \
  /etc/systemd/system/moex-tinvest-shadow.timer \
  /etc/systemd/system/moex-tinvest-health.service \
  /etc/systemd/system/moex-tinvest-health.timer
```

## 4. Активация и первый запуск

```bash
sudo /opt/moex-tinvest-bot/scripts/ubuntu/activate.sh
sudo systemctl status moex-tinvest-shadow.service --no-pager
sudo journalctl -u moex-tinvest-shadow.service -n 100 --no-pager
sudo find /var/lib/moex-tinvest-bot/artifacts -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TT %p\n'
```

Oneshot-сервис после успешного завершения будет `inactive (dead)` с кодом `0`; это нормально.
Постоянно активным должен быть timer:

```bash
systemctl list-timers 'moex-tinvest-*'
```

## 5. Расписание

`moex-tinvest-shadow.timer` запускает цикл каждый час в `HH:05 Europe/Moscow`. Python дополнительно
пропускает выходные и время вне консервативного окна 07:05–23:05 МСК. `Persistent=true`
запустит пропущенный timer после перезагрузки, но не восстановит утраченный рыночный снимок.

Healthcheck запускается каждые 15 минут и требует shadow artifact не старше двух часов.
Вне консервативного торгового окна freshness artifact не проверяется, поэтому ночь и выходные
не создают ложную тревогу. Конфиги и обязательные credentials проверяются всегда.

Переключать T-Invest-контур на сервере нужно в сохраняемом `/etc`-конфиге:

```bash
sudo -u moexbot /opt/moex-tinvest-bot/.venv/bin/python \
  -m moex_bot.cli environment-set --environment sandbox \
  --runtime /etc/moex-tinvest-bot/runtime.json
```

## 6. Каталоги

| Путь | Назначение |
| --- | --- |
| `/opt/moex-tinvest-bot` | код и venv, только root может изменять |
| `/etc/moex-tinvest-bot/bot.env` | секреты |
| `/etc/moex-tinvest-bot/runtime.json` | активный T-Invest контур `sandbox/prod` |
| `/var/lib/moex-tinvest-bot/artifacts` | shadow/flow/geo результаты |
| `/var/lib/moex-tinvest-bot/data` | SQLite Telegram outbox |
| `/var/log/moex-tinvest-bot` | отдельные логи циклов |
| `/var/backups/moex-tinvest-bot` | локальные backup-архивы |

## 7. Диагностика

```bash
sudo systemctl status moex-tinvest-shadow.timer moex-tinvest-health.timer
sudo systemctl status moex-tinvest-health.service
sudo journalctl -u moex-tinvest-shadow.service --since today
sudo journalctl -u moex-tinvest-health.service --since today
sudo -u moexbot /opt/moex-tinvest-bot/scripts/ubuntu/healthcheck.sh
sudo logrotate --debug /etc/logrotate.d/moex-tinvest-bot
```

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
`daemon-reload`, возвращает timers и запускает один shadow smoke-cycle. При ошибке trap пытается
вернуть timers в рабочее состояние.

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
