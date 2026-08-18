# Дорожная карта и чек-листы

Целевая long/short мультиактивная архитектура и недельная оценка описаны в
[../MULTI_ASSET_STRATEGY_RU.md](../MULTI_ASSET_STRATEGY_RU.md). Этапы ниже относятся к безопасному
фундаменту. Подключение short, futures и options выполняется последовательно после broker truth,
forecast ledger и weekly scorecard, а не параллельно с первым live-запуском.

## Этап 0. Спецификация — 2–4 дня

- [ ] Зафиксирована вселенная.
- [ ] Выбран тариф и модель издержек.
- [ ] Определены лимиты риска.
- [ ] Описаны режимы GeoRisk.
- [ ] Описан kill switch.
- [ ] Определены критерии допуска.

Definition of Done: правила однозначны и версионированы.

## Этап 1. Каркас и инфраструктура — 1 неделя

- [ ] Репозиторий и CI.
- [ ] Python 3.12 и dependency lock.
- [ ] PostgreSQL и миграции.
- [ ] Docker Compose.
- [ ] Secret store.
- [ ] Структурированные логи.
- [ ] Метрики и health checks.

DoD: сервисы запускаются повторяемо, секрет-сканирование проходит.

## Этап 2. Данные и реестр — 1–2 недели

- [ ] MOEX metadata/history/candles.
- [ ] ALGOPACK entitlement и загрузка.
- [ ] T-Invest instrument mapping.
- [ ] Торговый календарь.
- [ ] Quality gate.
- [ ] Point-in-time хранение.
- [ ] Корпоративные действия.

DoD: любой входной снимок воспроизводим, stale/conflict данные блокируются.

## Этап 3. Стратегия и бэктест — 1–2 недели

- [ ] Простая baseline-модель.
- [ ] Momentum/trend.
- [ ] Position sizing.
- [ ] Комиссии, spread, slippage и лоты.
- [ ] Бенчмарки.
- [ ] Walk-forward/OOS.
- [ ] Стресс-тесты.
- [ ] Журнал всех вариантов.

DoD: результат не зависит от одной точки параметров и выдерживает повышенные расходы.

## Этап 4. Геополитический модуль — 1–2 недели

- [ ] Реестр источников.
- [ ] Дедупликация.
- [ ] Entity mapping.
- [ ] GeoEvent/GeoRisk.
- [ ] Rule-based fallback.
- [ ] Плановый календарь.
- [ ] Исторический event catalog.
- [ ] Replay и ложные сообщения.

DoD: фильтр воспроизводим и не использует будущие публикации.

## Этап 5. Risk и execution — 1–2 недели

- [ ] Pre-trade checks.
- [ ] Idempotency persistence.
- [ ] Order state machine.
- [ ] Partial fills.
- [ ] Reconciliation.
- [ ] Restart recovery.
- [ ] Kill switch.
- [ ] Rate limiter/circuit breaker.

DoD: fault/replay тесты не создают дубликаты и заканчиваются известным состоянием.

## Этап 6. Sandbox — 1 неделя

- [ ] Read-only portfolio/positions/orders/operations.
- [ ] Forecast snapshot связан с broker snapshot.
- [ ] Все операции sandbox.
- [ ] Reconnect и restart.
- [ ] Отмена/замена.
- [ ] Rate limit.
- [ ] Дневное завершение.
- [ ] Recovery drill.

DoD: механика API стабильна; результат не интерпретируется как доходность.

## Этап 6A. Недельная оценка — до любых новых классов активов

- [ ] Forecast ledger с data cutoff, horizon и config hash.
- [ ] Directional/return/probability score по каждому горизонту.
- [ ] MAE/MFE, gross/net P&L и издержки.
- [ ] Attribution data/model/risk/execution/event/process.
- [ ] Counterfactual hedge только по доступным тогда данным и ценам.
- [ ] Equity/cash/margin utilization и blocked amounts.
- [ ] Issuer/sector/underlying/correlation concentration.
- [ ] Marginal contribution to risk и причины урезания размера.
- [ ] Автоматический отчёт не меняет параметры стратегии.

DoD: каждое решение воспроизводимо, а недельный отчёт отделяет ошибку прогноза от sizing,
execution и внешнего шока.

## Этап 6B. Акции long/short sandbox

- [ ] Long BUY/SELL и reconciliation.
- [ ] Short availability и margin policy.
- [ ] Отдельные gross/net/borrow/stress limits.
- [ ] Sizing от broker equity/free cash/free margin с обязательным cash reserve.
- [ ] Issuer/sector/correlation diversification limits.
- [ ] Short включается отдельным config gate.

DoD: рестарт, partial fills и неизвестные заявки не создают непреднамеренную short-позицию.

## Этап 6C. Фьючерсы и опционы

- [ ] Futures multiplier, ГО, expiry и rollover.
- [ ] Option chain, underlying UID, strike, expiry и style.
- [ ] IV surface и Greeks snapshots.
- [ ] Defined-risk option strategies.
- [ ] Portfolio delta/gamma/vega/theta и stress loss.
- [ ] Отдельный sandbox/shadow gate на каждый класс активов.

DoD: derivatives P&L и риск воспроизводимы; naked short options запрещены.

## Этап 7. Shadow — 4–8 недель

- [ ] Часовой цикл.
- [ ] Виртуальные исполнения.
- [ ] Real spread/slippage.
- [ ] Георежимы.
- [ ] Реконнекты.
- [ ] Ежедневная сверка.
- [ ] Нет реальных заявок.

DoD: выполнены заранее определённые SLA и пределы расхождений.

## Этап 8. Ограниченный live

- [ ] Отдельный небольшой счёт.
- [ ] Без плеча.
- [ ] 1–3 бумаги.
- [ ] Низкие лимиты.
- [ ] Лимитные заявки.
- [ ] Ручное включение.
- [ ] Ежедневное review.
- [ ] 30–50 исполнений до масштабирования.
- [ ] Каждый asset class разрешается отдельно; short/options/futures не включаются одновременно.

## Preflight каждого live-запуска

- [ ] Версия кода утверждена.
- [ ] Конфигурация подписана/проверена.
- [ ] Время синхронизировано.
- [ ] БД и backup доступны.
- [ ] MOEX/ALGOPACK свежие.
- [ ] T-Invest доступен.
- [ ] Портфель и заявки сверены.
- [ ] Торговый статус известен.
- [ ] GeoRisk не CRITICAL.
- [ ] Kill switch работает.
- [ ] Дневные лимиты сброшены корректно.
- [ ] Оператор получает уведомления.

## Чек-лист изменения стратегии

- [ ] Изменение оформлено как новая гипотеза.
- [ ] Не использован final holdout.
- [ ] Зарегистрировано число вариантов.
- [ ] Пройдены unit/backtest/OOS/replay.
- [ ] Проведён stress test издержек.
- [ ] Shadow запущен заново или обосновано иначе.
- [ ] Изменение не развёрнуто одновременно с масштабированием капитала.

## Чек-лист инцидента

- [ ] Новые заявки заблокированы.
- [ ] Broker truth получен.
- [ ] Unknown orders сверены.
- [ ] Позиции сверены.
- [ ] Токен отозван при подозрении на утечку.
- [ ] Снимок логов сохранён.
- [ ] Причина найдена.
- [ ] Регрессионный тест добавлен.
- [ ] Postmortem завершён.
- [ ] Возврат одобрен вручную.

## Реалистичная длительность

- Инженерный MVP до sandbox: примерно 6–10 недель.
- Shadow: ещё 4–8 недель.
- Осторожный live-пилот: ориентировочно через 2,5–4 месяца после начала, если все gates пройдены.

Срок зависит от качества данных, опыта команды и количества интеграций. Ускорение не должно исключать shadow или recovery-тестирование.

Материал предназначен для исследования и не является индивидуальной инвестиционной рекомендацией.
