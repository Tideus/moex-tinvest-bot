# Архитектура и данные

## 1. Логическая схема

```text
MOEX ISS/ALGOPACK ─→ Market Data ─→ Quality Gate ─→ Storage
                                                      ↓
Official sources ─→ News Collector ─→ Geo Classifier ─→ GeoRisk
                                                      ↓
                                           Strategy → Targets
                                                      ↓
T-Invest Account ─→ Reconciliation ───────────────→ Risk Engine
                                                      ↓
                                              Execution Plan
                                                      ↓
                                             T-Invest Orders
                                                      ↓
                                      OrderStateStream → Audit
```

## 2. Правила разделения ответственности

- `market_data` не знает о заявках.
- `strategy` не знает токенов и брокерских методов.
- `geo` не создаёт торгового направления напрямую.
- `risk` может уменьшить или запретить цель, но не расширить её сверх стратегии.
- `execution` исполняет только утверждённый план.
- `reconciliation` использует T-Invest как источник истины по брокерскому состоянию.
- `monitoring` не должен изменять позиции, кроме заранее определённого kill switch.

## 3. Предлагаемая структура репозитория

```text
bot/
  config/
  domain/
    instruments.py
    money.py
    orders.py
    portfolio.py
    risk.py
  adapters/
    moex.py
    algopack.py
    tinvest_market.py
    tinvest_orders.py
    news_sources.py
    database.py
  services/
    market_data.py
    strategy.py
    geo_risk.py
    risk_engine.py
    execution.py
    reconciliation.py
  jobs/
    hourly_cycle.py
    daily_report.py
    continuous_watchdog.py
  tests/
  migrations/
  dashboards/
```

## 4. Модель времени

- В БД хранить UTC и исходный timezone.
- В отчётах использовать Europe/Moscow.
- Для каждой свечи хранить `begin`, `end`, `received_at`, `source` и `is_complete`.
- Сигнал рассчитывается только по завершённому интервалу.
- Время события, публикации, получения и подтверждения хранится раздельно.
- Неполный текущий интервал нельзя сравнивать с полным прошлым интервалом.

## 5. Модель инструмента

Минимальные поля:

```text
secid, board, ticker, class_code, isin,
instrument_uid, figi, currency,
lot_size, min_price_increment,
trading_status, first_trade_date, last_trade_date,
verified_at, source_versions
```

Перед торговлей выполнить двухстороннюю проверку MOEX и T-Invest. Изменение lot size, class code или UID переводит инструмент в `QUARANTINED`.

## 6. Наборы данных

### Базовые

- MOEX ISS: метаданные, календарь, история, свечи, индексы.
- T-Invest: доступность инструмента, торговые статусы, брокерский стакан/цена, позиции, заявки и исполнения.
- Корпоративные действия: дивиденды, сплиты, реорганизации и смены тикеров.

### ALGOPACK

- TradeStats: характеристики исполненных сделок.
- OrderStats: размещение и отмена заявок.
- OBStats: видимая ликвидность, глубина и спред.
- Mega Alerts: статистически редкие наблюдённые события.
- HI2: концентрация рынка.

ALGOPACK поставляет данные и аналитику, но не исполняет заявки.

## 7. Quality Gate

Блокирующие ошибки:

- отсутствует обязательная свеча;
- timestamp идёт назад;
- имеются конфликтующие дубликаты;
- текущая свеча не завершена;
- цена, лот или шаг равны нулю;
- торговый статус неизвестен;
- данные старше допустимого SLA;
- MOEX/T-Invest существенно расходятся без объяснимой задержки;
- неизвестно состояние существующей заявки;
- база данных недоступна для фиксации идемпотентности.

Неблокирующие предупреждения должны сохраняться и влиять на отчёт качества.

## 8. Хранение и аудит

Основные таблицы:

- `instruments` и `instrument_versions`;
- `candles`;
- `algopack_metrics`;
- `news_documents`;
- `geo_events` и `geo_event_versions`;
- `strategy_runs`;
- `target_positions`;
- `risk_decisions`;
- `execution_plans`;
- `orders` и `order_events`;
- `fills`;
- `portfolio_snapshots`;
- `reconciliation_runs`;
- `incidents`;
- `config_versions`.

Каждое решение должно воспроизводиться по версии кода, конфигурации и входному снимку.

## 9. Надёжность

- transactional outbox для важных событий;
- уникальный индекс на `(account_id, order_id)`;
- сохранение заявки до сетевого вызова;
- повторяемые миграции;
- резервные копии и point-in-time recovery;
- health/readiness endpoints;
- экспоненциальный backoff с jitter;
- circuit breaker;
- ограничение частоты запросов ниже лимитов T-Invest;
- автоматическая сверка после каждого рестарта.

## 10. Конфигурация

Конфигурация должна быть:

- типизированной;
- версионированной;
- проверяемой схемой;
- неизменяемой внутри одного запуска;
- разделённой на `research`, `sandbox`, `shadow`, `live`;
- снабжённой безопасными значениями по умолчанию.

Production не должен запускаться, если явно не установлен режим `live` и не пройдена preflight-проверка.

Материал предназначен для исследования и не является индивидуальной инвестиционной рекомендацией.
# Реализованное расширение 2026-08-14

- Часовой контур: completed candles → quality/geo/risk → shadow intents → SQLite outbox.
- Доставка Telegram вынесена в отдельный one-way sender; входящие команды запрещены.
- TradeStats, FUTOI и HI2 читаются через отдельный read-only ALGOPACK adapter.
- Именованные крупные держатели поступают только из датированных раскрытий и не смешиваются
  с анонимной микроструктурой.
