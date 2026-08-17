# Источники и готовые компоненты

Актуальность веб-проверки: 14 августа 2026 года.

## Официальные компоненты

- MOEX `moexalgo`: https://github.com/moexalgo/moexalgo
- PyPI `moexalgo`: https://pypi.org/project/moexalgo/
- T-Invest API: https://developer.tbank.ru/invest/api
- Начало работы T-Invest: https://developer.tbank.ru/invest/intro/intro
- Новые официальные SDK T-Bank: https://opensource.tbank.ru/invest
- Сообщение о переносе SDK и карантине старого пакета: https://developer.tbank.ru/invest/release/release1_44
- T-Invest sandbox: https://developer.tbank.ru/invest/intro/developer/sandbox/
- T-Invest API TLS certificates: https://developer.tbank.ru/docs/tls-settings
- T-Bank PC/Linux certificate guide: https://www.tbank.ru/bank/help/certificates/
- Лимиты T-Invest API: https://developer.tbank.ru/invest/intro/intro/limits
- Токены T-Invest: https://developer.tbank.ru/invest/intro/intro/token
- Тарифы T-Invest: https://www.tbank.ru/invest/tariffs/

## Геополитические и регуляторные источники

- OFAC Recent Actions: https://ofac.treasury.gov/recent-actions
- OFAC Russia-related sanctions: https://ofac.treasury.gov/sanctions-programs-and-country-information/russian-harmful-foreign-activities-sanctions
- Совет ЕС, санкционные пресс-релизы: https://www.consilium.europa.eu/en/press/press-releases/?topic=130644
- Банк России: https://www.cbr.ru/
- Календарь для инвесторов Банка России: https://www.cbr.ru/eng/about_br/irp/
- Московская биржа: https://www.moex.com/

Для юридически значимого санкционного события необходимо открыть сам правовой акт, а не ограничиваться агрегатором или заголовком.

## Рассмотренные open-source решения

- `backtrader_moexalgo`: https://github.com/WISEPLAT/backtrader_moexalgo
- `FinLabPy`: https://github.com/cia76/FinLabPy
- `TinvestPy`: https://github.com/cia76/TinvestPy
- конкурсный Python-бот: https://github.com/qwertyo1/tinkoff-trading-bot
- Java-бот: https://github.com/roman-struchev/tinkoff-invest
- пример `invest-bot`: https://github.com/EIDiamond/invest-bot
- связанный backtesting tool: https://github.com/EIDiamond/trade_backtesting
- таблица конкурсных работ T-Invest: https://developer.tbank.ru/invest/intro/intro/robot_contest

Среди рассмотренных проектов не найден независимо подтверждённый production-ready стек, одновременно закрывающий ALGOPACK, исследования, корректный бэктест, T-Invest execution, reconciliation, risk и monitoring. Готовые проекты следует использовать как архитектурные примеры после проверки лицензий, контрактов, зависимостей и безопасности.

## Исследования стратегии и тестирования

- Moskowitz, Ooi, Pedersen, Time Series Momentum: https://pages.stern.nyu.edu/~lpederse/papers/TimeSeriesMomentum.pdf
- Hurst, Ooi, Pedersen, A Century of Evidence on Trend-Following Investing: https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing?aqrPDF=1
- Novy-Marx, Velikov, A Taxonomy of Anomalies and Their Trading Costs: https://www.nber.org/papers/w20721
- Bailey et al., Statistical Overfitting and Backtest Performance: https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf
- Bailey et al., The Probability of Backtest Overfitting: https://ssrn.com/abstract=2326253

Исследования относятся преимущественно к другим рынкам и не доказывают будущую прибыльность стратегии на MOEX. Они обосновывают проверяемую гипотезу и методику контроля переобучения.

## Контракт доказательств проекта

Для каждого материального вывода хранить:

```text
id | класс | значение/единица | период/as_of | источник/dataset
| формула | полнота/задержка | confidence | ограничение
```

Разделять:

- наблюдение;
- расчёт;
- сообщение источника;
- интерпретацию;
- сценарий.

Материал предназначен для исследования и не является индивидуальной инвестиционной рекомендацией.
# Дополнительные первичные источники

- Telegram Bot API: https://core.telegram.org/bots/api
- MOEX ALGOPACK SuperCandles: https://www.moex.com/algopackvisual/supercandles
- MOEX ALGOPACK FUTOI: https://www.moex.com/algopackvisual/futoi
- Банк России, статистика ПИФ/АИФ: https://www.cbr.ru/statistics/RSCI/activity_uk_if/stat_pif_aif/
