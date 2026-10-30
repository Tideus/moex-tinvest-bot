# Поддомены breakwood.ru — состояние 30.10.2026

## Обновление: DNS и HTTPS включены

Все три A-записи разрешаются в 185.246.152.115. Выпущен SAN-сертификат Let's Encrypt
breakwood-services для s3.breakwood.ru, sync.breakwood.ru, monitoring.breakwood.ru,
действует до 28.01.2027. В sites-enabled/breakwood-services включён TLS site
breakwood-services.tls.pending (несмотря на историческое имя, файл теперь активен).
HTTP перенаправляет на HTTPS, ACME webroot исключён из redirect.

S3 health по новому HTTPS адресу возвращает 200; анонимный корень S3 — 403,
что не является отказом API. Syncthing/Kuma административные адреса сохраняют
loopback-only ACL: публичный 403 намеренный. Kuma остаётся доступной через SSH tunnel.
Для Syncthing передаётся Host 127.0.0.1:8384, чтобы сохранить backend host-check.
Старые адреса S3/console и /syncthing/ пока сохранены.

Certbot.timer включён; deploy-hook /etc/letsencrypt/renewal-hooks/deploy/20-nginx-reload
выполняет nginx -t перед reload. Новая копия старой nginx-конфигурации:
/var/backups/nginx-breakwood/letsencrypt.ctYA1c.
Тест certbot renew --dry-run --cert-name breakwood-services --run-deploy-hooks
успешен, включая выполнение deploy-hook. Локально Syncthing HTTPS и Kuma
Socket.IO polling возвращают 200, снаружи административные адреса возвращают 403.

Ниже — историческое состояние первоначального bootstrap, не текущий статус TLS.

На vivek выполнено:

- Удалён include mdl-static.conf, вместо него /mdl-static и /mdl-static/ возвращают 410.
- Удалены /var/www/mdl-web/mdl-static (~175 MiB), пустой родитель mdl-web и старый snippet.
  Копия удалённых файлов не создавалась. Конфигурация сохранена в
  /var/backups/nginx-breakwood/20261030T103420Z.
- /mdl-games/, данные MinIO, старые адреса S3/console и /syncthing/ сохранены.
- Включён /etc/nginx/sites-available/breakwood-services: только HTTP ACME + 503.
- TLS-конфиг подготовлен как /etc/nginx/sites-available/breakwood-services.tls.pending,
  НЕ включён. Syncthing/Kuma admin ACL пока loopback-only, требуется admin/VPN IP.
- nginx -t прошёл; nginx/Syncthing active, MinIO health 200, удалённый маршрут 410.

## DNS, которые нужно добавить

| Тип | Имя | Значение для размещения на vivek |
|---|---|---|
| A | s3 | 185.246.152.115 |
| A | sync | 185.246.152.115 |
| A | monitoring | 185.246.152.115 |

На момент проверки эти точные FQDN не разрешались. Для первоначального выпуска
сертификатов использовать DNS only. AAAA не добавлять без проверенного IPv6.
Если Kuma будет на отдельной VM, monitoring должен указывать на ту VM; там нужны
свои nginx и сертификат. Размещение на vivek — только пилот, без независимости от VM.

## После появления DNS

Проверить точные FQDN с завершающей точкой. Затем получить SAN сертификат:

```bash
sudo certbot certonly --webroot -w /var/www/letsencrypt \
  --cert-name breakwood-services \
  -d s3.breakwood.ru -d sync.breakwood.ru -d monitoring.breakwood.ru
```

Выбрать admin/VPN ACL в pending-конфиге, проверить GUI authentication Syncthing.
Развернуть Kuma до включения proxy на 3001; не публиковать её setup без ограничений.
Заменить bootstrap symlink на pending TLS site только после наличия сертификата:
nginx -t, reload, HTTPS/SNI/WebSocket проверки. Не включать оба сайта одновременно.
Для Kuma push ACL добавить IP фактического collector, не весь интернет.

S3 / 9000 использует сохранённый Host, без переписывания URI: клиенты обновляются
на https://s3.breakwood.ru отдельно, без HTTP redirect подписанных S3-запросов.
www.breakwood.ru (console / 9001) пока не переносится и остаётся рабочим.

Файлы: deploy/monitoring/nginx/breakwood-services-{http,tls}.conf.
Одноразовый скрипт: scripts/ubuntu/prepare-breakwood-nginx.py, dry-run по умолчанию.
При повторном запуске после миграции намеренно останавливается, а не перезаписывает сайт.
