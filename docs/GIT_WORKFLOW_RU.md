# Git: настройка, commit и push

Репозиторий проекта:

```text
https://github.com/Tideus/moex-tinvest-bot.git
```

## Что хранится в Git

В репозиторий включаются исходный код, тесты, документация, примеры конфигурации и JSON-файлы
стратегии. Секреты, локальные базы, логи, виртуальное окружение и рабочие артефакты исключены
через `.gitignore`.

Никогда не добавляйте `/etc/moex-tinvest-bot/bot.env`, локальный `.env`, токены T-Invest,
MOEX API key или Telegram bot token.

## Однократное восстановление настроек на Windows

Выполнять в Git Bash. Команда `safe.directory` нужна только при ошибке `dubious ownership`.

```bash
cd /c/Users/mtide/Documents/Codex/2026-08-14/z/outputs/moex-tinvest-bot

git config --global --add safe.directory C:/Users/mtide/Documents/Codex/2026-08-14/z/outputs/moex-tinvest-bot
git config --global user.name "m.tideus"
git config --global user.email "m.tideus@gmail.com"

git remote get-url origin >/dev/null 2>&1 \
  && git remote set-url origin https://github.com/Tideus/moex-tinvest-bot.git \
  || git remote add origin https://github.com/Tideus/moex-tinvest-bot.git

git branch -M main
git branch --set-upstream-to=origin/main main 2>/dev/null || true
```

`safe.directory`, `user.name` и `user.email` являются настройками конкретного компьютера и
поэтому не могут автоматически храниться внутри клонируемого репозитория. Адрес `origin`
автоматически появляется при обычном `git clone`.

## Обычная публикация изменений

Сначала убедитесь, что в списке нет секретов:

```bash
cd /c/Users/mtide/Documents/Codex/2026-08-14/z/outputs/moex-tinvest-bot

git status --short
git diff --check
git diff --stat
```

Запустите проверки проекта:

```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check src tests
./.venv/Scripts/python.exe -m mypy src
./.venv/Scripts/python.exe -m moex_bot.cli config-check --root .
```

Создайте commit и отправьте его:

```bash
git add -A
git status --short
git commit -m "Add bounded sandbox long-short trading"
git push -u origin main
```

При первом push GitHub может запросить вход через браузер или Personal Access Token. Пароль
аккаунта GitHub для HTTPS push не используется.

## Обновление Ubuntu-сервера после push

Перед `git pull` рабочая копия сервера должна быть чистой. Файл
`/etc/moex-tinvest-bot/bot.env` находится вне репозитория и при обновлении не заменяется.

```bash
cd ~/moex-tinvest-bot
git status --short
git pull --ff-only
sudo bash scripts/ubuntu/update.sh
sudo moex-botctl prelaunch
sudo moex-botctl start
sudo moex-botctl diagnose --once
```

Если `git status --short` на сервере показывает локальные изменения, не выполняйте
`git reset --hard`. Сначала сохраните diff:

```bash
cd ~/moex-tinvest-bot
git diff > ~/moex-tinvest-bot-server-changes.patch
git status --short
```

После этого нужно отдельно решить, какие серверные изменения перенести в основной репозиторий.

