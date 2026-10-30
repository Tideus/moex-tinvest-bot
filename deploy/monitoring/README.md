# Шаблоны отдельного monitoring-стека

План: [docs/uptime-kuma-migration.md](../../docs/uptime-kuma-migration.md).

- compose.yaml: только Kuma, loopback:3001, отдельный volume, image digest обязателен.
- nginx/00-kuma-websocket.conf: http-context map и upstream.
- nginx/kuma-bootstrap.conf: только HTTP ACME, до получения сертификата.
- nginx/kuma.conf: целевой TLS proxy с ACL UI/push.

Заменить домен и TEST-NET IPs. Не включать bootstrap и TLS site одновременно.
Секреты не добавлять в Git. Alert relay/agent — следующие этапы реализации,
они не создаются этим compose. Рабочий стек бота сейчас не меняется.
