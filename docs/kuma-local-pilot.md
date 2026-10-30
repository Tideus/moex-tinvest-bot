# Локальный пилот Uptime Kuma на vivek

Отдельный стек: /opt/moex-monitoring/compose.yaml, persistent data: ./data.
Официальный image 2.5.8 при установке закрепляется на RepoDigest в .env.
Порт публикуется только как 127.0.0.1:3001; docker.sock и файлы бота не монтируются.
Старый мониторинг и торговые уведомления пока остаются работающими.

## Первоначальный доступ с ноутбука

В Git Bash или PowerShell:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:13001:127.0.0.1:3001 vivek
```

Не закрывая терминал, открыть http://127.0.0.1:13001. Создать администратора,
уникальный пароль сохранить в менеджере паролей; включить MFA, если доступна.
Пароль в чат не отправлять. Завершить туннель: Ctrl+C. Если локальный 13001 занят,
заменить только левый порт в -L и адресе браузера.

Не открывать 3001 в firewall и не публиковать setup через nginx без ACL.
Kuma не поддерживает штатный подпуть /monitoring/; после DNS используется
monitoring.breakwood.ru с TLS и ограниченным доступом администратора.

## Проверки на сервере

```bash
cd /opt/moex-monitoring
sudo docker compose ps
curl -I http://127.0.0.1:3001/
sudo docker compose logs --tail 50
```

Не размещать в логах и Git Telegram/SMTP пароли, Push URLs или backup data.
При настройке каналов алертов использовать TELEGRAM_ALERT_CHAT_ID=-1004295022477,
не адрес торговых отчётов. Мониторы и каналы ещё требуется настроить после admin setup.
SMTP fallback, relay, Push agent и внешнее наблюдение — следующие этапы плана.

## Эксплуатация

restart=unless-stopped восстанавливает контейнер после перезапуска Docker;
намеренный docker compose stop оставит его остановленным. Обновление — отдельно
от scripts/ubuntu/update.sh бота, с согласованной версией/digest и предварительным
backup data при остановленной Kuma. Не обновлять image tag автоматически.

На том же vivek мониторинг не обнаружит выключение всей VM собственными силами.
После пилота нужен перенос на отдельный monitoring-хост/внешний watchdog.
TLS/DNS порядок: nginx-breakwood-migration.md; полный план: uptime-kuma-migration.md.
