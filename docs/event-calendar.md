# Календарь инвестора: первый этап

Календарь работает только в режиме наблюдения и НЕ меняет заявки, стопы,
размер позиций и существующий геофильтр. Это отдельный контур от исполнения.

## Настройки config/event_calendar.json

| Параметр | Значение и влияние |
|---|---|
| mode | Только observe; другое значение вызывает ошибку |
| token_env | Имя переменной с отдельным токеном «Только чтение», не сам токен |
| horizon_days | Горизонт отчётов и дивидендов, 1–365 дней |
| exchange | Биржа для TradingSchedules; расписание запрашивается на 7 дней |

Источник: T-Invest InstrumentsService, методы GetAssetReports, GetDividends,
TradingSchedules. Используется production market-data endpoint, но никакие методы
изменения счёта не вызываются. Токены исполнения не подставляются автоматически.

## Ubuntu

Добавить T_INVEST_READONLY_TOKEN через sudoedit /etc/moex-tinvest-bot/bot.env.
После установки/обновления проекта выполнить:

```bash
sudo systemctl daemon-reload
sudo systemctl start moex-tinvest-calendar.service
sudo journalctl -u moex-tinvest-calendar.service -n 80 --no-pager
sudo systemctl enable --now moex-tinvest-calendar.timer
sudo -u moexbot /opt/moex-tinvest-bot/.venv/bin/python -m moex_bot.cli calendar-status --database /var/lib/moex-tinvest-bot/data/events.sqlite3
```

Обновление в 08:30 МСК. Частичный отказ возвращает exit=2 и INCOMPLETE,
старые события не удаляются. Старше 26 часов данные считаются STALE.
Пустой календарь не означает отсутствие событий. Удаление события у поставщика
не считается подтверждённой отменой. Разные даты одного периода сохраняются
как неоднозначные, нужна проверка первоисточника. Время отчёта не следует считать
точным временем публикации, если источник сообщает только дату.

SQLite хранит исходные ответы, время получения, события и изменения содержимого.
Для анализа использовать только записи, полученные до принятия решения.
Дивидендное событие показано по recordDate, НЕ по дате отсечки для покупки.

## Что ещё не подключено

Автоматическая лента News, Telegram-дайджест, календарь ЦБ, санкционные списки,
привязка событий к решениям и event-study пока не реализованы этим этапом.
Существующий geo-refresh продолжает читать RSS ЦБ и MOEX и менять риск по ключевым
словам; он не является полноценным политическим анализом. Подключение News требует
проверки актуального контракта: старый публичный protobuf не содержит этого метода.
Не использовать догаданные REST URL в production.

Контракт календаря:
https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/instruments.proto
