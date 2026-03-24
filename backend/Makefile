.PHONY: dev test migrate migrate-down lint format typecheck clean build help

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
migrate: ## Run pending Alembic migrations
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

# ── Testing ───────────────────────────────────────────────────────────────────
test: ## Run all tests with coverage
	pytest

test-fast: ## Run tests without coverage (faster feedback)
	pytest -p no:cov

test-watch: ## Run tests in watch mode
	watchfiles "pytest -p no:cov" app

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
