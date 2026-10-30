# Очистка 30.10.2026

На vivek выполнена выборочная очистка, без остановки торговли и без удаления рабочих БД.

- Свободно: примерно 5.1 → 6.6 GiB; занято 73% → 65%.
- Journald: 1.9 GiB → 502 MiB. /etc/systemd/journald.conf.d/90-moex-retention.conf:
  SystemMaxUse=512M, SystemKeepFree=2G, SystemMaxFileSize=64M, MaxRetentionSec=30day.
  Лимит объёма может сократить фактический срок хранения при интенсивных логах.
- Bot backups: 69 → 29 MiB, оставлены 5 последних moex-tinvest и 2 moex-disaster.
  Перед удалением остальных проверены SHA256 и полная читаемость tar/gzip сохраняемых копий.
  Это не полноценное испытание восстановления БД/сервера.
- Bot logs: 31 → 8.9 MiB, удалены файлы старше 14 дней.
- Артефакты стратегии сохраняются 90 дней. Уникальный архив отчёта за
  07.09–30.10.2026 не удалён. Рабочие SQLite, env, сертификаты, MinIO volumes,
  Syncthing, Kuma data и nginx rollback copies не удалялись.
- Удалено 10526 файлов, суммарный logical size 47448224 bytes, плюс старые журналы
  journald. Манифест: /var/log/moex-tinvest-bot/cleanup-summary-20261030T133749Z.json.

Скрипт scripts/ubuntu/cleanup-service-history.py выполняет dry-run по умолчанию,
удаляет только при --apply и только внутри трёх фиксированных history roots.
Файлы удаляются окончательно, без корзины; некоторые данные могут присутствовать
в оставленных резервных копиях, полного возврата удалённых журналов не гарантируется.

Journald drop-in хранится в deploy/ubuntu/moex-journald-retention.conf. На новой VM
его надо установить как /etc/systemd/journald.conf.d/90-moex-retention.conf,
проверить настройки и перезапустить systemd-journald. Это системная политика,
а не автоматическое действие обычного update.sh бота.

Docker build cache, images/volumes и auth/btmp logs не очищались: это отдельная
политика, они могут понадобиться для пересборки и аудита безопасности.
