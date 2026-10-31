# Sandbox replacement и ручной PROD canary — 0.7.0rc1

Исторический срез 30 октября. Актуальные проверки 0.7.0rc2 и ограничения:
[VERIFICATION_2026-10-31_RU.md](VERIFICATION_2026-10-31_RU.md).

Это кандидат релиза. Наличие исполнителя не является разрешением на реальную торговлю.
В репозитории `prod_live.enabled=false`, `prod_long.execution_stage=observer`;
root admission не создан. Автоматический production-таймер не добавлялся.

## Выполненная замена sandbox

30.10.2026 после конкретного подтверждения владельца через серверный CLI:

- Закрыты только настроенные старые long `…e74babaf` и intraday `…44fe3b54`.
- Созданы long `…b6a4669f` и intraday `…1240a2fa`, каждый с 300 000 виртуальных RUB.
- Независимое повторное чтение: на обоих 0 позиций, 0 активных заявок, оборот 0.
- Три sandbox-привязки в `/etc/moex-tinvest-bot/bot.env` обновлены одной заменой файла;
  production-параметры и остальные счета не изменялись.
- Закрытие старых счетов необратимо у брокера; локальная история сохранена в архиве
  `/var/backups/moex-tinvest-bot/moex-history-reset-20261030T144613Z.tar.gz`.
- Журнал операции: `/var/lib/moex-tinvest-bot/execution/sandbox-replacement-20261030.json`.

Бот пока остановлен. Замена выполнена из изолированной копии CLI; установленный релиз
0.6.3 не заменён, rc-код ещё не развёрнут как рабочая служба.

В будущих заменах: `sandbox-replace-preview` показывает ровно два явно настроенных счёта,
обычные и стоп-заявки; `sandbox-replace` требует точный hash подтверждённого preview,
остановленные торговые службы, неизменившиеся остатки и отдельный долговечный state-файл.
Создание, пополнение и закрытие не повторяются вслепую после тайм-аута. Сначала проверяются
оба новых баланса, затем переключаются привязки и закрываются только согласованные счета.
Не запускайте такую замену параллельно с торговлей и не удаляйте журнал операции.

## Что реализовано для PROD

Отдельный `TInvestLiveBroker` использует только `https://invest-public-api.tbank.ru/rest`.
Sandbox URL не подменяется. `execute_live_intent` выполняет одну подтверждённую заявку,
а не включает автономную торговлю всеми стратегиями.

Проверяется непосредственно перед отправкой:

- ID, статус OPEN, реальный тип ИИС и FULL_ACCESS из GetAccounts;
- UID, SECID, класс, лотность, шаг цены, RUB, тип share, доступность API/ИИС и отсутствие
  непройденных тестов; universe защищает issuer/sector/cluster от подмены в плане;
- статус обычной торговой сессии, доступность LIMIT, свежесть и спред стакана ≤ 0,5%;
  лимитная цена не дальше 1% от mid;
- свободные деньги и предварительная стоимость с комиссиями через GetOrderPrice;
- максимум 40 000 ₽ экспозиции canary, 10 000 ₽ на заявку, 20 000 ₽ оборота и 2 попытки
  в день по московской дате. Отменённые/отклонённые попытки также расходуют лимит;
- концентрация: issuer ≤ 25%, sector и risk cluster ≤ 50% canary-капитала;
- новые покупки блокируются дневным убытком, включая ручные позиции счёта. Консервативный
  порог: `min(капитал счёта, canary capital) × max_account_daily_loss_weight`;
  при 40 000 ₽ и 0,015 — 600 ₽. Это не обещание исполнения стопа по указанной цене;
- нет незавершённой заявки в ledger, активных обычных заявок и пересекающегося stop-order;
- продажа не превышает одновременно bot-owned units и свободные units брокера;
  protected SECID никогда не торгуются. Неоднозначность ownership блокирует исполнение.

Первый этап — только RUB-акции, LIMIT DAY, long/reduce-only, без маржи, шортов и derivatives.
Существующий intraday остаётся отдельным sandbox-профилем.

Capture и лимиты сериализованы SQLite-транзакцией. Reservation фиксируется до POST.
Стабильный production request UUID привязан к аккаунту и logical ID. Accepted, partial,
timeout и UNKNOWN блокируют следующие заявки, пока read-only reconciliation не подтвердит
финал. Автоматической отмены/замены реальных заявок нет. Если задержка обнаружена до первого
POST, фиксируется локальное REJECTED/not_submitted; при падении процесса статус остаётся
неразрешённым. Не очищайте ledger для продолжения торговли.

## Команды и границы

Новые команды:

```text
prod-live-preview    читает брокера, выводит конкретную заявку, не отправляет её
prod-live-enroll     явно создаёт первый account-bound ledger, не отправляет заявку
prod-live-execute    исполняет только конкретно подтверждённую заявку
prod-live-reconcile  только читает GetOrderState и обновляет ledger, ничего не отменяет
prod-live-probe      проверяет real read-only API и оценку одного лота, не торгует
```

`prod-live-preview`, `prod-live-enroll`, `prod-live-execute` принимают `--plan` и
`--order-index`. План должен быть свежим (≤ 5 минут), quality-passed, рассчитанным для
именно этого ИИС. Сохраняются и проверяются ID исходного счёта, path и SHA-256 исходного
broker snapshot. Sandbox/file/intraday-планы для production отклоняются.

После установки rc доступен безопасный preview; он сам по себе не даёт торгового допуска:

```bash
sudo -u moexbot bash -c '
set -a
source /etc/moex-tinvest-bot/bot.env
set +a
cd /opt/moex-tinvest-bot
exec .venv/bin/python -m moex_bot.cli prod-live-preview \
  --settings /etc/moex-tinvest-bot/prod-live.json \
  --policy /etc/moex-tinvest-bot/prod-long.json \
  --runtime config/prod_runtime.json --universe config/universe.json \
  --plan /var/lib/moex-tinvest-bot/artifacts/PROD_PLAN.json \
  --order-index 0 --output /var/lib/moex-tinvest-bot/artifacts/NEW_CONFIRMATION.json
'
```

Имена с заглавными буквами — placeholders; нужен реально сформированный свежий PROD-план.
Output должен быть новым отдельным JSON-файлом, не существующим конфигом или ledger.
В preview указан masked IIS, BUY/SELL, LIMIT, лоты и units, цена, сумма и оценка комиссии.
Согласие на «включить prod» не заменяет подтверждение этой конкретной заявки.

`execute` дополнительно требует `--confirmation` (файл preview) и
`--confirm-intent-sha256` (точный digest показанной заявки). Подтверждение действует ≤ 5 минут
и связано с полным plan SHA, аккаунтом, политикой, версией кода/configs и UUID журнала.
Смена любого связанного параметра требует нового preview/подтверждения.

## Конфиги и допуск

Installer/update создают `/etc/moex-tinvest-bot/prod-live.json` и `prod-long.json` только
если их нет; существующие значения не перезаписываются. Это отдельные root-owned 0640
операторские конфиги, чтобы не редактировать `/opt` и не ломать build manifest.

Параметры `prod_live.json`:

| Ключ | Влияние |
| --- | --- |
| `version` | Версия схемы, сейчас 1 |
| `enabled` | По умолчанию false; enroll/execute немедленно блокируются |
| `account_id_env` | Только T_INVEST_PROD_ACCOUNT_ID; sandbox fallback запрещён |
| `journal` | Постоянный отдельный PROD SQLite, не зависит от output |
| `admission` | Root-owned файл независимого допуска; сам не создаётся установщиком |

Для допуска нужны все независимые reviewer attestations: текущая стратегия прошла promotion,
брокерская сверка выполнена, защитные приказы и восстановление испытаны, независимый code
review пройден. `decision=APPROVED_CANARY` связан с exact account, policy SHA и release SHA;
срок ≤ 7 дней. Проверяются UID владельца root, права и отсутствие symlink у файла **и всех
родительских каталогов**. Файл 0600, принадлежащий moexbot, не считается независимым допуском.

Это явные утверждения проверяющего, а не автоматическое доказательство доходности.
Нельзя заполнять их `true` вместо получения доказательств. Соответствующий файл допуска
в рамках разработки не создавался, критерии promotion не ослаблялись.

Первый ledger создаётся только отдельным enroll после проверки исходных ручных позиций.
Execute/reconcile никогда не создают отсутствующий ledger. Его потеря, другой аккаунт,
другой UUID или попытка подставить sandbox DB ведут к блокировке.

## Что ещё не является выполненным

- Автоматическое выставление защитного stop при каждом BUY не реализовано и не испытано.
  Reviewer не должен отмечать protective_orders_tested до испытания выбранной схемы.
- Не выполнены актуальная статистическая проверка прибыльности и operational promotion.
- Реальный PostOrder на ИИС не вызывался; сетевые контракты проверены mock-транспортом.
- Дополнительно реальный read-only probe выполнен на ИИС `…2550`: account type подтверждён,
  instrument/portfolio/positions/withdraw/orders/stops/operations/price/status/book методы
  отработали, capture занял 2,20 с, market_tradable=true. Оценка одного лота SBER — технический
  запрос, не торговый сигнал; PostOrder не вызывался.
- Новая сборка пока не установлена как служба; production timers не добавлены.

## Миграции и восстановление

`execution/` не является очищаемой историей и сохраняется reset-history. Старые sandbox
артефакты можно разбирать `sandbox-journal-prepare` из архивной копии: это quarantine
candidate, не активная БД. Закрытые sandbox-счета не переносятся на новые под чужими ID.
Будущая миграция между версиями обязана сохранять logical/request UUID, pending/UNKNOWN,
ownership, daily counters, account enrollment и непрерывность истории.

При восстановлении PROD сначала остановить исполнение и отозвать admission, восстановить
ledger вместе с конфигом, сверить операции/позиции/заявки у брокера, затем получить новый
независимый допуск. Root admission не следует автоматически восстанавливать как активный.
Повторный enroll вместо утраченного ledger не является процедурой восстановления.

Официальные контракты: [песочница](https://developer.tbank.ru/invest/intro/developer/sandbox),
[Orders](https://developer.tbank.ru/invest/services/orders/methods),
[Operations](https://developer.tbank.ru/invest/services/operations/methods),
[MarketData](https://developer.tbank.ru/invest/services/quotes/marketdata).
