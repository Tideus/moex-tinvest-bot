.PHONY: install integrations lint typecheck test verify replay preflight integration-preflight config-check hourly-shadow

install:
	python -m pip install -e ".[dev]"

integrations:
	python -m pip install -e ".[integrations]"

lint:
	python -m ruff check .

typecheck:
	python -m mypy src

test:
	python -m pytest --cov=moex_bot --cov-report=term-missing --cov-fail-under=75

verify: lint typecheck test

replay:
	python -m moex_bot.cli replay --config config/replay.json --input examples/replay_snapshot.json

preflight:
	python -m moex_bot.cli preflight --config config/shadow.json

integration-preflight:
	python -m moex_bot.cli integration-preflight

config-check:
	python -m moex_bot.cli config-check

hourly-shadow:
	python -m moex_bot.cli hourly-shadow
