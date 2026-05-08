.PHONY: up down test test-unit test-integration migrate serve load-test lint format typecheck

up:
	docker compose up -d

down:
	docker compose down

# Run unit + integration. test-integration brings up Postgres if it
# isn't already running, so this works from a cold start.
test: test-unit test-integration

test-unit:
	# -p no:unraisableexception: see tests/integration/conftest.py
	# header. Same Windows-specific GC-finalizer cleanup applies to
	# aiosqlite in unit tier as it does to asyncpg in integration tier.
	python -m pytest tests/unit tests/property -p no:unraisableexception

test-integration:
	docker compose up -d --wait postgres
	python -m pytest tests/integration -p no:unraisableexception

migrate:
	python -m alembic upgrade head

serve:
	python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

load-test:
	python scripts/load_test.py --customers 10 --quotes-per-customer 5

lint:
	python -m ruff check .

format:
	python -m ruff format .

typecheck:
	python -m mypy app/domain
