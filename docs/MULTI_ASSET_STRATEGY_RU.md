# Целевая long/short мультиактивная модель и недельная оценка

## 1. Назначение

Целевая система прогнозирует распределение изменения цены российских биржевых инструментов и
выражает прогноз через акции, фьючерсы и опционы. Текущий ограниченный Sandbox long/short —
второй контрольный
baseline, а не конечная стратегия.

Система должна поддерживать:

- long и short;
- докупку, частичное сокращение и полное закрытие;
- направленные и хеджирующие фьючерсные позиции;
- опционы с заранее ограниченным риском;
- краткосрочные горизонты до месяца;
- долгосрочные горизонты до года;
- еженедельный разбор прогнозов, решений, сделок, ошибок и возможных страховок.

Под «инсайтами» понимаются только законно полученные публичные данные: официальные раскрытия,
новости, отчётность, сделки инсайдеров, опубликованные через разрешённые источники, консенсус и
наблюдаемая рыночная микроструктура. Непубличная существенная информация не используется.

## 2. Что прогнозирует модель

Модель не должна возвращать только `BUY` или `SELL`. Для каждого инструмента, времени сигнала и
горизонта сохраняется неизменяемый `ForecastSnapshot`:

```text
instrument_uid / secid / instrument_type
signal_time / data_cutoff / forecast_horizon
expected_return
probability_up / probability_down
return_quantiles: p05 / p25 / p50 / p75 / p95
expected_volatility
confidence / uncertainty
market_regime
signal_components и их версии
invalidation_conditions
code_version / config_hash / data_versions
```

Базовые горизонты исследования:

| Класс | Горизонты | Назначение |
| --- | --- | --- |
| intraday/короткий | 1 час, 1 день | реакция на поток, аномалии, новости и смену режима |
| swing | 5, 10 и 20 торговых дней | движение до месяца |
| среднесрочный | 3 и 6 месяцев | тренд, отчётность, макро и корпоративные катализаторы |
| долгосрочный | 12 месяцев | фундаментальная и режимная гипотеза |

Для каждого горизонта обучается и оценивается отдельная модель. Нельзя одной и той же оценкой
уверенности смешивать часовой сигнал и прогноз на год.

## 3. Семейства сигналов

### Рыночные и технические

- доходности и momentum на нескольких окнах;
- moving averages, breakout, mean-reversion и режим тренда;
- realized volatility, gap, drawdown и корреляции;
- объём, оборот, ликвидность, spread и глубина;
- относительная сила к IMOEX, отрасли и связанным инструментам.

### MOEX/ALGOPACK

- TradeStats: buyer-/seller-initiated исполненный объём;
- OrderStats: постановка, снятие и изменение заявок;
- OBStats: spread, глубина и дисбаланс видимого стакана;
- FUTOI: long/short и участники по группам для фьючерсов;
- HI2: анонимная концентрация;
- Mega Alerts: наблюдённые ценовые и объёмные аномалии.

Каждый источник остаётся наблюдением, а не готовым торговым приказом. Например, TradeStats не
показывает открытые шорты, а HI2 не раскрывает имена держателей.

### Новости, геополитика и корпоративные события

- время события, публикации, получения и подтверждения хранятся отдельно;
- первичные источники имеют приоритет над перепечатками;
- события связываются с эмитентом, отраслью, валютой, ставкой и товаром;
- модель различает ожидаемое событие и неожиданный шок;
- календарь отчётности, дивидендов, экспираций и заседаний входит в snapshot.

LLM может классифицировать и резюмировать публичные документы, но не выставляет заявки и не
обходит детерминированный risk-gate.

### Фундаментальные и публичные аналитические данные

- отчётность и динамика показателей;
- дивиденды и корпоративные действия;
- прогнозы инвестдомов и консенсус;
- опубликованные сделки инсайдеров;
- ставки риска, доступность short и торговые статусы;
- именованные держатели только из датированных публичных раскрытий.

## 4. Преобразование прогноза в инструмент

Прогноз базового актива и способ его реализации — разные решения.

### Акции

- long при положительном ожидаемом движении и достаточной уверенности;
- short при отрицательном прогнозе, доступности займа и допустимой стоимости short;
- размер зависит от риска, ликвидности, корреляции и уже существующей экспозиции.

### Фьючерсы

- направленная позиция либо hedge систематического, валютного или товарного риска;
- отдельно учитываются ГО, variation margin, multiplier, expiry и rollover;
- лимит задаётся через notional и stress loss, а не только стоимость ГО.

### Опционы

- используются implied volatility, skew, term structure, delta/gamma/vega/theta, strike и expiry;
- прогноз направления отделяется от прогноза volatility;
- на первых этапах разрешены только стратегии с ограниченным максимальным убытком: покупка
  call/put и дебетовые вертикальные спреды;
- naked short option и неограниченный short gamma запрещены до отдельного risk review;
- размер ограничивается полной премией/максимальным убытком и портфельными Greeks.

Call выражает право купить базовый актив, put — право продать. Покупка опциона и продажа опциона
имеют принципиально разные профили риска.

## 5. Портфельный risk engine

### 5.1 Broker balance как источник sizing

Перед расчётом каждой цели система получает согласованный point-in-time снимок счёта:

```text
cash_total
cash_available
cash_blocked
portfolio_equity / NAV
positions_market_value
unrealized_pnl
realized_pnl_today
initial / maintenance margin
free_margin
active_orders и reserved amounts
```

Источником истины является выбранный T‑Invest account. Локальный snapshot используется только
после reconciliation. Если баланс, позиции, blocked amounts или active orders неизвестны, новые
BUY/short запрещаются.

Размер позиции считается не от суммы пополнения и не от свободных рублей отдельно, а от
актуального equity и risk budget:

```text
signal_target
  ∩ лимит риска на сделку
  ∩ свободный cash/free margin
  ∩ лимит позиции/эмитента/сектора/класса активов
  ∩ liquidity/market-impact limit
  ∩ portfolio stress limit
  = допустимый размер
```

При покупке акций учитывается свободный cash и уже заблокированные суммы. Для short учитываются
доступность займа, ставка риска, стоимость переноса и free margin. Для фьючерсов лимит считается
по полному notional и stress loss, а не по небольшому ГО. Для опционов учитываются premium,
максимальный убыток и Greeks.

Cash reserve задаётся явно. Стратегия не имеет права автоматически потратить весь баланс: резерв
нужен для комиссий, variation margin, gap, исполнения hedge и изменения требований по обеспечению.

### 5.2 Диверсификация

Диверсификация контролируется на нескольких уровнях:

- один инструмент;
- один базовый актив/эмитент;
- группа связанных инструментов одного underlying;
- сектор;
- фактор/макроэкспозиция: индекс, ставка, RUB, нефть, золото и т. п.;
- класс активов: акции, фьючерсы, опционы;
- горизонт и expiry bucket;
- корреляционный кластер;
- long, short, gross и net exposure.

Акция SBER, фьючерс на SBER и опционы на SBER не считаются тремя независимыми позициями. Они
агрегируются по underlying и delta-equivalent. Аналогично несколько нефтегазовых акций и фьючерс
на нефть могут создавать одну скрытую факторную ставку.

Минимальный набор портфельных ограничений:

```text
max_position_weight
max_underlying_exposure
max_issuer_exposure
max_sector_exposure
max_correlated_cluster_exposure
max_asset_class_exposure
max_gross_exposure
max_net_long / max_net_short
max_margin_utilization
min_cash_reserve
max_portfolio_delta/gamma/vega
max_single_event_loss
max_portfolio_stress_loss
```

Первые sandbox-значения являются консервативными исследовательскими лимитами, а не обещанием
оптимальности. Они утверждаются отдельно для размера счёта и не повышаются автоматически после
прибыльной недели.

При выборе между одинаково сильными сигналами предпочтение получает позиция, которая меньше
увеличивает marginal contribution to portfolio risk. Корреляции оцениваются point-in-time и
проходят стресс: в кризис обычно диверсификация хуже, чем в спокойной выборке.

### 5.3 Порядок портфельного расчёта

1. Получить broker snapshot и выполнить reconciliation.
2. Перевести все позиции в RUB notional и underlying/factor exposures.
3. Пересчитать option Greeks и futures multiplier/ГО.
4. Зарезервировать cash buffer и обеспечение active orders.
5. Сформировать прогнозы и unconstrained targets.
6. Применить issuer/sector/correlation/asset-class limits.
7. Минимизировать turnover и лишние издержки через no-trade bands.
8. Провести base/adverse/extreme stress scenarios.
9. Сформировать order plan только в пределах свободного cash/margin.
10. Повторить risk-check перед каждой отправкой, потому что баланс мог измениться.

Weekly report показывает среднюю и максимальную концентрацию, cash/margin utilization, marginal
risk contribution и ситуации, когда диверсификационный лимит уменьшил или запретил сделку.

### 5.4 Общие блокирующие проверки

До любой sandbox или live заявки проверяются:

- broker truth: cash, позиции, active/stop orders и доступный лимит;
- instrument identity, lot, tick, multiplier, expiry и trading status;
- gross/net exposure, issuer/sector/asset-class limits;
- beta, currency и commodity exposure;
- margin/ГО и stress loss;
- option delta, gamma, vega, theta и worst-case loss;
- liquidity, spread, market impact и max participation;
- дневной loss, turnover, drawdown и число заявок;
- short availability и стоимость займа, если применимо;
- GeoRisk и календарь событий;
- отсутствие `UNKNOWN` orders и успешная reconciliation.

Неизвестное критическое состояние блокирует новые позиции. Допускается только заранее
определённое уменьшение риска.

## 6. Почему песочница не доказывает прибыльность

T‑Invest Sandbox подходит для проверки portfolio/order API, идемпотентности, отмены, замены,
restart recovery и long/short механики. Но её исполнение и маржа упрощены: market orders используют
last price, объём не создаёт market impact, комиссия фиксирована, дивиденды/налоги отсутствуют.

Поэтому используются два независимых доказательства:

1. cost-aware replay/shadow оценивает прогноз и экономику на исторических/наблюдённых данных;
2. sandbox проверяет техническую корректность состояний портфеля и заявок.

## 7. Недельный разбор

Недельный отчёт строится по зафиксированным до исхода прогнозам. Изменять старый forecast после
получения результата запрещено.

### 7.1 Forecast scorecard

Для каждого инструмента и горизонта:

- прогноз направления и ожидаемой доходности;
- фактическая доходность на том же timestamp/price convention;
- directional hit/miss;
- forecast error;
- Brier/log loss для вероятностного прогноза;
- calibration: например, как часто реализовался рост при `probability_up ≈ 70%`;
- MAE/MFE после сигнала;
- режим рынка и доступность данных.

### 7.2 Deal scorecard

Для каждой sandbox/shadow/live сделки:

- forecast ID и причина входа;
- planned/actual entry и exit;
- лоты, multiplier, premium/ГО;
- комиссия, spread, slippage, borrow и rollover;
- gross и net P&L;
- holding period;
- максимальная неблагоприятная и благоприятная экскурсия;
- соответствие исполненного размера risk plan;
- качество выхода отдельно от качества входа.
- equity/free cash/free margin до и после решения;
- issuer/sector/cluster concentration и marginal risk contribution;
- причина уменьшения размера: баланс, liquidity, diversification или stress limit.

### 7.3 Attribution ошибок

Ошибки помечаются без переписывания истории:

| Категория | Примеры | Можно было повлиять |
| --- | --- | --- |
| data | stale/missing, неверный mapping, corporate action | да: quality gate, резервный источник |
| model | неверный знак, горизонт или regime | да: признаки, модель, calibration |
| sizing/risk | слишком большая позиция, корреляция, leverage | да: лимиты и stress tests |
| execution | spread, slippage, partial fill, late order | частично: тип/время/размер заявки |
| event shock | внезапное решение, санкция, авария | предсказать не всегда; ограничить ущерб можно |
| process | конфиг, restart, reconciliation, ручная ошибка | да: автоматизация и checklist |

Фраза «не могли повлиять» допустима только для возникновения внешнего события. На размер потерь
часто можно было повлиять диверсификацией, лимитом позиции, option hedge, stop/exit policy или
уменьшением риска перед известным календарным событием.

### 7.4 Анализ возможной страховки

Counterfactual hedge рассчитывается только по инструментам и ценам, доступным на момент решения:

- меньший размер или отказ от сделки;
- hedge фьючерсом на индекс/валюту/товар;
- protective put;
- put spread или collar;
- временное снижение gross/net exposure;
- диверсификация и ограничение коррелированных позиций.

Отчёт показывает стоимость hedge, снижение tail loss и влияние на обычную доходность. Нельзя
выбирать идеальный hedge задним числом по уже известному исходу.

### 7.5 Итог недели

```text
данные и покрытие
→ score прогнозов по горизонтам
→ P&L и execution
→ ошибки и attribution
→ counterfactual hedges
→ нарушения risk/process
→ решения: оставить / исследовать / отключить
```

Автоматически менять production-параметры по одной неделе запрещено. Изменение оформляется как
новая гипотеза и снова проходит backtest, walk-forward, holdout и shadow.

## 8. Поэтапная реализация

### Этап A — broker truth и недельный журнал

- read-only portfolio/positions/orders/operations для sandbox;
- point-in-time snapshots и mapping instrument UID;
- equity, available/blocked cash, free margin и active-order reserves;
- forecast ledger и неизменяемые версии данных/конфига;
- exposure aggregation по issuer/underlying/sector/correlation cluster;
- первый weekly report без отправки заявок.

### Этап B — акции long/short в sandbox

- long/short BUY/SELL/COVER и reconciliation;
- затем отдельный short policy, borrow/margin limits и confirm-margin gate;
- sandbox orders остаются выключенными по умолчанию до ручного включения.

### Этап C — качественная модель до месяца

- multi-horizon technical/ALGOPACK/news features;
- cost-aware replay и rolling OOS;
- probability calibration и сравнение с текущим momentum baseline.

### Этап D — фьючерсы

- metadata, multiplier, ГО, expiry/roll;
- направленная и hedge-книга;
- отдельный notional/stress risk.

### Этап E — опционы

- option chain, strikes/expiry, underlying mapping;
- IV/Greeks и surface snapshots;
- только defined-risk strategies;
- сценарный P&L и expiry/assignment controls.

### Этап F — горизонты 3–12 месяцев

- fundamentals, corporate actions, macro/commodity/FX regimes;
- отдельная модель и капитал от краткосрочной книги;
- более редкий rebalance и собственные benchmark/OOS.

### Этап G — ограниченный live

- только после успешного sandbox, shadow и weekly review;
- разрешения по asset class включаются отдельно;
- short, futures и options не получают live-доступ одновременно.

## 9. Текущий реализованный инкремент

Этап A реализован для cash/equity/positions/orders/operations, дневного оборота и дневного/
недельного P&L. Начата ограниченная часть Этапа B: после явной команды оператора проверенный
часовой план может отправить лимитные заявки на sandbox-счёт. Доступны long BUY и SELL только
для уменьшения имеющейся позиции; naked short, margin, фьючерсы и опционы по-прежнему закрыты.

Ближайший следующий gate — поштучное reconciliation частично исполненных/активных заявок,
восстановление состояния после рестарта и накопление не менее 4–8 недель OOS-отчётов. Только
после этого исследуется отдельная short policy с borrow/margin limits.

## 10. Официальные технические источники

- T‑Invest InstrumentsService: https://developer.tbank.ru/invest/api/instruments-service
- Options/underlying mapping: https://developer.tbank.ru/invest/services/instruments/more-instrument
- Futures and margin: https://developer.tbank.ru/invest/services/instruments/head-instruments
- SandboxService: https://developer.tbank.ru/invest/api/sandbox-service
- Sandbox limitations: https://developer.tbank.ru/invest/intro/developer/sandbox

Документ задаёт исследовательскую и инженерную архитектуру, а не обещание доходности и не
индивидуальную инвестиционную рекомендацию.
