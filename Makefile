.PHONY: up down test test-unit lint format typecheck

up:
	docker compose up -d

down:
	docker compose down

test:
	python -m pytest

test-unit:
	python -m pytest tests/unit

lint:
	python -m ruff check .

format:
	python -m ruff format .

typecheck:
	python -m mypy app/domain
