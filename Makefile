.DEFAULT_GOAL := help
SHELL         := /bin/bash

# ─── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' \
		| sort

# ─── Setup ────────────────────────────────────────────────────────────────────
.PHONY: install
install: ## Full first-time setup: venv, deps, .env, demo db compose up
	@bash scripts/setup.sh

.PHONY: deps
deps: ## Install/update Python dependencies via Poetry
	poetry install

.PHONY: deps-update
deps-update: ## Update all dependencies to latest allowed versions
	poetry update

# ─── App ──────────────────────────────────────────────────────────────────────
.PHONY: run
run: ## Start the app (uses poetry run → uvicorn via querygate.run)
	poetry run python -m querygate.run

.PHONY: run-dev
run-dev: ## Start with auto-reload (development mode)
	poetry run uvicorn querygate.api.app:app --host 0.0.0.0 --port 8000 --reload

.PHONY: dev
dev: run-dev ## Alias for run-dev

.PHONY: seed-demo-db
seed-demo-db: ## Generate an optional SQLite fixture for tests/inspection (not a runtime connection)
	poetry run python examples/demo_db/seed.py

.PHONY: validate-config
validate-config: ## Validate connections.yaml/policy.yaml (CONNECTIONS_FILE / POLICY_FILE env vars, or pass ARGS="--connections-file ... --policy-file ...")
	poetry run querygate-validate-config $(ARGS)

# ─── Tests ────────────────────────────────────────────────────────────────────
.PHONY: test
test: ## Run the full test suite
	poetry run pytest

.PHONY: test-unit
test-unit: ## Run only unit tests
	poetry run pytest -m unit

.PHONY: test-integration
test-integration: ## Run only integration tests
	poetry run pytest -m integration

.PHONY: test-verify
test-verify: ## Run only the core-guarantee verification/regression suite (real DB, no mocks)
	poetry run pytest -m verification

.PHONY: test-security
test-security: ## Run the adversarial security regression suite
	poetry run pytest -m security

.PHONY: test-postgres-live
test-postgres-live: ## Run tests needing a real Postgres (timeout + load guardrails) — run compose-up first
	poetry run pytest -m postgres_live

LOAD_ROUNDS ?= 3
.PHONY: test-load
test-load: ## Verify concurrency/timeout guardrails under concurrent real-Postgres load
	QUERYGATE_LOAD_ROUNDS=$(LOAD_ROUNDS) poetry run pytest -m load

SOAK_ROUNDS ?= 100
.PHONY: test-soak
test-soak: ## Repeat the real-Postgres guardrail load scenarios (override SOAK_ROUNDS=N)
	QUERYGATE_LOAD_ROUNDS=$(SOAK_ROUNDS) poetry run pytest -m load

.PHONY: test-mssql-live
test-mssql-live: ## Run tests needing a real MSSQL server — see tests/integration/test_mssql_live.py's module docstring for setup
	poetry run python tests/integration/setup_mssql_test_db.py
	poetry run pytest -m mssql_live

.PHONY: test-real-db
test-real-db: ## Run every test needing a real database (Postgres + MSSQL)
	poetry run pytest -m real_db

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	poetry run pytest --cov=src --cov-report=term-missing --cov-report=html

# ─── Code quality ─────────────────────────────────────────────────────────────
.PHONY: format
format: ## Format code with Black
	poetry run black src/ tests/ examples/

.PHONY: fmt
fmt: format ## Alias for format

.PHONY: format-check
format-check: ## Check formatting without making changes
	poetry run black --check src/ tests/ examples/

.PHONY: lint
lint: format-check ## Alias for format-check (extend with ruff/mypy when added)

# ─── Release ──────────────────────────────────────────────────────────────────
.PHONY: release-check
release-check: ## Run deterministic source/package release gates and build artifacts
	poetry check --lock
	poetry run python scripts/check_release.py
	$(MAKE) format-check
	$(MAKE) test
	QUERYGATE_DEMO_DB_URL=postgresql+asyncpg://user:pass@localhost/demo \
		poetry run querygate-validate-config
	poetry build
	poetry run python scripts/check_release_artifacts.py

.PHONY: release-smoke
release-smoke: ## Build the image and execute a real structured query against Postgres
	bash scripts/release_smoke.sh

# ─── Docker Compose ───────────────────────────────────────────────────────────
.PHONY: compose-up
compose-up: ## Start the local demo Postgres and Redis services (detached and healthy)
	docker compose up -d --wait

.PHONY: compose-down
compose-down: ## Stop and remove the local demo infrastructure containers
	docker compose down

.PHONY: compose-logs
compose-logs: ## Tail logs from the local demo infrastructure
	docker compose logs -f

# ─── Cleanup ──────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove .venv, __pycache__, .pytest_cache, coverage artifacts
	rm -rf .venv
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov .coverage
