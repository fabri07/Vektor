.PHONY: dev test migrate migrate-down lint format typecheck clean build help seed-vertical-fields

# ── Configuration ─────────────────────────────────────────────────────────────
DOCKER_COMPOSE = docker compose
PYTHON         = python
APP_MODULE     = app.main:app

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Development ───────────────────────────────────────────────────────────────
dev: ## Start all services with docker-compose (hot reload)
	$(DOCKER_COMPOSE) up --build

dev-bg: ## Start all services in background
	$(DOCKER_COMPOSE) up --build -d

stop: ## Stop all docker-compose services
	$(DOCKER_COMPOSE) down

logs: ## Tail logs from all services
	$(DOCKER_COMPOSE) logs -f

shell: ## Open a shell in the backend container
	$(DOCKER_COMPOSE) exec backend bash

# ── Database & Migrations ─────────────────────────────────────────────────────
migrate: ## Run pending Alembic migrations (local Docker Postgres)
	alembic upgrade head

migrate-neon: ## Run pending Alembic migrations against Neon (production)
	@if [ -z "$$DATABASE_URL_SYNC" ] && [ -z "$$DATABASE_URL" ]; then \
		echo "ERROR: Exportá DATABASE_URL_SYNC o DATABASE_URL con la URL de Neon antes de correr."; \
		echo "  export DATABASE_URL_SYNC='postgresql+psycopg2://user:pass@...neon.tech/vektor'"; \
		exit 1; \
	fi
	alembic upgrade head

migrate-down: ## Rollback last Alembic migration
	alembic downgrade -1

migrate-create: ## Create a new migration (use: make migrate-create MSG="your message")
	alembic revision --autogenerate -m "$(MSG)"

migrate-history: ## Show migration history
	alembic history --verbose

db-reset: ## Drop and recreate the database (DANGER: destroys all data)
	$(DOCKER_COMPOSE) exec postgres psql -U vektor -c "DROP DATABASE IF EXISTS vektor;"
	$(DOCKER_COMPOSE) exec postgres psql -U vektor -c "CREATE DATABASE vektor;"
	alembic upgrade head

seed-demo: ## Seed 3 demo tenants (kiosco/limpieza/deco) with calibrated data
	$(PYTHON) scripts/seed_demo_data.py

reset-demo: ## Drop and re-seed all 3 demo tenants (useful between investor demos)
	$(PYTHON) scripts/reset_demo_data.py

# URL asyncpg del Postgres LOCAL de Docker (servicio `postgres`). El seed script
# no normaliza el esquema, por eso se pasa +asyncpg inline. Requiere el stack
# levantado (make dev-bg) + docker-compose.override.yml (DATABASE_URL → local).
LOCAL_DB_URL = postgresql+asyncpg://vektor:vektor@postgres:5432/vektor

seed-demo-local: ## Seed demo tenants against the LOCAL Docker Postgres (safe, never touches prod)
	$(DOCKER_COMPOSE) exec -e DATABASE_URL=$(LOCAL_DB_URL) backend python scripts/seed_demo_data.py

reset-demo-local: ## Drop and re-seed demo tenants against the LOCAL Docker Postgres (safe)
	$(DOCKER_COMPOSE) exec -e DATABASE_URL=$(LOCAL_DB_URL) backend python scripts/reset_demo_data.py

seed-vertical-fields: ## Upsert vertical field definitions from JSON files (idempotent)
	$(PYTHON) scripts/seed_vertical_fields.py

# ── Testing ───────────────────────────────────────────────────────────────────
test: ## Run all tests with coverage
	pytest

test-fast: ## Run tests without coverage (faster feedback)
	pytest -o addopts="" -p no:cov

test-watch: ## Run tests in watch mode
	watchfiles "pytest -o addopts='' -p no:cov" app

test-file: ## Run a specific test file (use: make test-file FILE=app/tests/api/v1/test_auth.py)
	pytest $(FILE) -v

# ── Linting & Formatting ──────────────────────────────────────────────────────
lint: ## Run ruff linter
	ruff check app

format: ## Auto-format with ruff
	ruff format app
	ruff check --fix app

typecheck: ## Run mypy type checker
	mypy app --ignore-missing-imports

check: lint typecheck ## Run all checks (lint + typecheck)

# ── Build ─────────────────────────────────────────────────────────────────────
build: ## Build the Docker image
	$(DOCKER_COMPOSE) build

# ── Utilities ─────────────────────────────────────────────────────────────────
clean: ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage .mypy_cache

install: ## Install production dependencies
	pip install -r requirements.txt

install-dev: ## Install development dependencies
	pip install -r requirements-dev.txt

setup: install-dev ## Initial project setup
	cp -n .env.example .env || true
	@echo "✓ Dependencies installed. Edit .env before running 'make dev'."
