# Mawjood developer commands.
#
# `make check` is exactly what CI runs. If it passes locally, CI passes.
# `make simulate` is the fastest way to see Mawjood work: no credentials needed.

.DEFAULT_GOAL := help
.PHONY: help install run simulate seed seed-all-down qr test test-integration \
        lint format typecheck check migrate revision secrets-scan hooks up down \
        logs clean

PY := uv run

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install dependencies
	uv sync --extra dev

run: ## Run the API locally with reload
	$(PY) uvicorn mawjood.main:create_app --factory --reload --port 8000

simulate: ## Chat with Mawjood in the terminal (no WhatsApp credentials needed)
	$(PY) python tools/chat_sim.py --audit

seed: ## Create the default tenant, merchants and routing config
	$(PY) python tools/seed_dev.py --routing

seed-all-down: ## Reconfigure salon routing so every aggregator fails (invariant demo)
	$(PY) python tools/seed_dev.py --all-down

qr: ## Generate an attribution QR: make qr src=SRC12
	$(PY) python tools/make_qr.py --source $(src)

test: ## Run the test suite (no network, ever)
	$(PY) pytest -q

test-integration: ## Run tests against a live PostgreSQL (needs MAWJOOD_TEST_DATABASE_URL)
	$(PY) pytest -q -m integration

lint: ## Lint and check formatting
	$(PY) ruff check .
	$(PY) ruff format --check .

format: ## Apply formatting and safe autofixes
	$(PY) ruff check --fix .
	$(PY) ruff format .

typecheck: ## Type check (strict on mawjood/core/)
	$(PY) mypy

secrets-scan: ## Run the pre-commit hooks, including the secret scan
	$(PY) pre-commit run --all-files

check: lint typecheck test ## Everything CI runs

migrate: ## Apply migrations
	$(PY) alembic upgrade head

revision: ## Create a migration: make revision m="add bookings"
	$(PY) alembic revision --autogenerate -m "$(m)"

hooks: ## Install git pre-commit hooks
	$(PY) pre-commit install

up: ## Start the local stack (app + postgres)
	docker compose up --build -d
	@echo "readiness: curl -s localhost:8000/readyz"

down: ## Stop the local stack
	docker compose down

logs: ## Tail the app logs
	docker compose logs -f app

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
