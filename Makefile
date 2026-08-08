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

.PHONY: install-hooks
install-hooks: ## Enable the committed git hooks (pre-commit gate + commit-msg authorship strip) for this clone — run once per contributor
	git config core.hooksPath .githooks
	@echo "Enabled .githooks/{pre-commit,commit-msg} — 'git commit' now runs the pre-commit floor and strips AI co-author trailers. Bypass a commit with --no-verify."

.PHONY: test-hooks
test-hooks: ## Run the mutation-tested suites for .githooks/pre-commit and .githooks/commit-msg
	sh .githooks/test-pre-commit.sh
	sh .githooks/test-commit-msg.sh

# ─── App ──────────────────────────────────────────────────────────────────────
.PHONY: run
run: ## Start the app (uses poetry run → uvicorn via querygate.run)
	poetry run python -m querygate.run

.PHONY: run-dev
run-dev: ## Start with auto-reload (development mode)
	poetry run uvicorn querygate.api.app:app --host 0.0.0.0 --port 8010 --reload

.PHONY: dev
dev: run-dev ## Alias for run-dev

.PHONY: seed-demo-db
seed-demo-db: ## Generate an optional SQLite fixture for tests/inspection (not a runtime connection)
	poetry run python examples/demo_db/seed.py

SEED_SCALE ?= 1.0
.PHONY: seed-large
seed-large: ## Seed the larger real-world demo domain into the running Postgres (compose-up first). Override SEED_SCALE=0.1 for a fast subset, 5 to stress.
	SEED_SCALE=$(SEED_SCALE) poetry run python -m examples.demo_db.generate_large --postgres --drop

.PHONY: validate-config
validate-config: ## Validate connections.yaml/policy.yaml (CONNECTIONS_FILE / POLICY_FILE env vars, or pass ARGS="--connections-file ... --policy-file ...")
	poetry run querygate-validate-config $(ARGS)

.PHONY: scope-catalog
scope-catalog: ## Regenerate docs/SCOPE_CATALOG.md from core/scopes.py (drift-tested)
	poetry run querygate-scope-catalog --output docs/SCOPE_CATALOG.md

.PHONY: worklist-check
worklist-check: ## Verify TODO.md / ROADMAP.md / TODO_ARCHIVE.md reconcile (drift-tested by test_worklist_consistency.py)
	python3 scripts/check_worklist.py

.PHONY: worklist-sync
worklist-sync: ## Regenerate the derived Quick-scan ✅ column + bare ROADMAP checkboxes from the TODO.md headings
	python3 scripts/check_worklist.py --fix

.PHONY: semantic-memory-evaluate
semantic-memory-evaluate: ## Run the fixed offline semantic-memory 32A benchmark
	poetry run python -m querygate.catalog_cli evaluate

.PHONY: adaptive-learning-test
adaptive-learning-test: ## Run the end-to-end usage-learning lifecycle proof (item 37): baseline -> evidence -> learn -> review -> publish -> improved re-run -> staleness/rollback/restart
	poetry run python -m querygate.catalog_cli adaptive-learning-test

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

.PHONY: test-ts-client
test-ts-client: ## Build + test the TypeScript client builder (item 51 phase 2; requires npm install once in clients/typescript)
	cd clients/typescript && npm test

.PHONY: test-verify
test-verify: ## Run only the core-guarantee verification/regression suite (real DB, no mocks)
	poetry run pytest -m verification

.PHONY: test-security
test-security: ## Run the adversarial security regression suite
	poetry run pytest -m security

.PHONY: security-benchmark
security-benchmark: ## Run the reproducible adversarial security benchmark (item 58): catch rate vs. a modeled raw-SQL baseline. Pass ARGS="--json" or ARGS="list".
	poetry run querygate-security-benchmark $(or $(ARGS),run)

.PHONY: anomaly-ui-smoke
anomaly-ui-smoke: ## Render the item-59 anomaly panel with the real admin UI in headless Chromium and assert the visualization (screenshot -> dist/anomaly-ui-smoke.png; SKIPs if no browser)
	poetry run python scripts/anomaly_ui_smoke.py

.PHONY: seed-anomaly-demo
seed-anomaly-demo: ## Append demo spike traffic to AUDIT_JSONL_PATH so the live /admin Observability anomaly panel shows real signals (use --reset to truncate first)
	poetry run python scripts/seed_anomaly_demo.py

.PHONY: test-postgres-live
test-postgres-live: ## Run tests needing a real Postgres (timeout + load guardrails) — run compose-up first
	poetry run pytest -m postgres_live

.PHONY: test-stress
test-stress: ## Differential-correctness + security-at-volume tests on the large demo domain (real Postgres; compose-up first). Override LARGE_STRESS_SCALE=0.3 for more data.
	poetry run pytest -m "postgres_live and not load" tests/integration/test_large_domain_stress.py

LOAD_ROUNDS ?= 3
.PHONY: test-load
test-load: ## Verify concurrency/timeout guardrails under concurrent real-Postgres load
	QUERYGATE_LOAD_ROUNDS=$(LOAD_ROUNDS) poetry run pytest -m load

SOAK_ROUNDS ?= 100
.PHONY: test-soak
test-soak: ## Repeat the real-Postgres guardrail load scenarios (override SOAK_ROUNDS=N)
	QUERYGATE_LOAD_ROUNDS=$(SOAK_ROUNDS) poetry run pytest -m load

.PHONY: test-mssql-live
# Depends on compose-up too, not just compose-up-mssql: the cross-dialect
# differential suite (item 36 phase 2b) is marked BOTH postgres_live and
# mssql_live, so `-m mssql_live` selects it and it needs the demo Postgres up as
# well. Without this it fails with a bare NoSuchTableError that looks like an
# MSSQL problem.
test-mssql-live: compose-up compose-up-mssql ## Run tests needing a real MSSQL server (starts + seeds both databases for you)
	poetry run python tests/integration/setup_mssql_test_db.py
	poetry run pytest -m mssql_live

.PHONY: test-mysql-live
test-mysql-live: compose-up-mysql ## Run tests needing a real MySQL server (starts + seeds the database for you)
	poetry run python tests/integration/setup_mysql_test_db.py
	poetry run pytest -m mysql_live

.PHONY: test-real-db
test-real-db: ## Run every test needing a real database (Postgres + MSSQL + MySQL)
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

# ─── Security scanning ────────────────────────────────────────────────────────
# All scanners are dev/CI-only — none is a runtime dependency of the shipped
# image. Each is deny-by-default with a reviewed allowlist, the same posture as
# the pip-audit dependency gate (security/dependency-audit-allowlist.json).
# Local targets prefer an installed binary and fall back to the tool's official
# container image, so they work without a global install. See TODO.md item 89
# and docs/SECURITY_POSTURE.md.

SCAN_IMAGE ?= querygate:security-scan

# The tag floats because CI installs the current release (`pipx install semgrep`),
# so pinning locally would report different findings than the gate it reproduces.
# `--pull always` below is load-bearing, not belt-and-braces: Docker never
# re-resolves a tag it has already cached, so without it the local gate silently
# freezes at whatever release was current the first time it ran, while CI keeps
# floating. Measured 2026-07-27: a cached `latest` sat at 1.169.0 while PyPI
# served 1.171.0 — and `--disable-version-check` (kept for byte-parity with CI)
# suppresses the upgrade notice that would have revealed it. Override to pin.
SEMGREP_IMAGE ?= semgrep/semgrep:latest
# One definition of the rulesets + flags so the installed-binary and container
# paths agree. .github/workflows/ci.yml's Semgrep step still keeps its own copy of
# the same list, so a change here must be mirrored there —
# tests/unit/test_security_posture_commands.py fails if the two ever drift.
SEMGREP_ARGS ?= --error --disable-version-check \
	--config p/python --config p/security-audit --config p/owasp-top-ten src/

.PHONY: scan-image
scan-image: ## Trivy: scan the built container image for OS+library CVEs, secrets, misconfig (deny-by-default via .trivyignore)
	docker build -t $(SCAN_IMAGE) .
	@if command -v trivy >/dev/null 2>&1; then \
		trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $(SCAN_IMAGE); \
	else \
		echo "trivy not installed; using official aquasec/trivy image"; \
		docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
			-v $(PWD)/.trivyignore:/.trivyignore aquasec/trivy:latest \
			image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $(SCAN_IMAGE); \
	fi

.PHONY: scan-secrets
scan-secrets: ## gitleaks: scan the working tree and full git history for committed secrets (allowlist in .gitleaks.toml)
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks detect --source . --config .gitleaks.toml --redact --verbose; \
	else \
		echo "gitleaks not installed; using official zricethezav/gitleaks image"; \
		docker run --rm -v $(PWD):/repo zricethezav/gitleaks:latest \
			detect --source /repo --config /repo/.gitleaks.toml --redact --verbose; \
	fi

.PHONY: sast
sast: ## Bandit static security analysis over src/ (config in pyproject.toml [tool.bandit]). Semgrep is the separate `semgrep` target — run both, or `security-scan`, to reproduce the whole CI SAST job.
	poetry run bandit -c pyproject.toml -r src/

.PHONY: semgrep
semgrep: ## Semgrep OSS rulesets (p/python, p/security-audit, p/owasp-top-ten) over src/ — the other half of the CI SAST job. Scans git-TRACKED files only, so `git add` new sources first. No login/token; needs network, and Docker unless semgrep is installed.
	@if command -v semgrep >/dev/null 2>&1; then \
		semgrep $(SEMGREP_ARGS); \
	else \
		echo "semgrep not installed; using official $(SEMGREP_IMAGE) image"; \
		docker run --rm --pull always -v "$(CURDIR):/src:ro" -w /src $(SEMGREP_IMAGE) \
			semgrep $(SEMGREP_ARGS); \
	fi

.PHONY: test-dast
test-dast: ## Schemathesis: fuzz the OpenAPI surface to prove only the validated AST is accepted (no raw-SQL path)
	poetry run python scripts/run_dast.py

.PHONY: security-scan
security-scan: ## Run the batchable local security gates (Bandit + Semgrep SAST, secrets, dependency audit, DAST) — `scan-image` and `test-security` run separately
	$(MAKE) sast
	$(MAKE) semgrep
	$(MAKE) scan-secrets
	$(MAKE) sbom
	$(MAKE) test-dast

# ─── Docs ─────────────────────────────────────────────────────────────────────
.PHONY: product-guide-html
product-guide-html: ## Render docs/PRODUCT_GUIDE.md into the browsable docs/product-guide.html
	poetry run python scripts/generate_product_guide_html.py

.PHONY: trust-page
trust-page: ## Regenerate docs/TRUST_EVIDENCE.md, the composed procurement evidence page (item 147)
	poetry run python scripts/generate_trust_page.py

.PHONY: mcp-extension-schema
mcp-extension-schema: ## Regenerate the io.github.agitmit/structured-query-ast MCP extension schema (item 131)
	poetry run python scripts/generate_mcp_extension_schema.py

# ─── Release ──────────────────────────────────────────────────────────────────
.PHONY: sbom
sbom: ## Generate a CycloneDX SBOM, dependency vulnerability report, and SHA256SUMS from dist/ (run `poetry build` first)
	poetry run python scripts/generate_sbom.py

.PHONY: verify-release
verify-release: ## Verify dist/ artifact integrity against dist/SHA256SUMS (the check a consumer runs after download). Pass ARGS="--dist-dir path".
	poetry run python scripts/verify_release.py $(ARGS)

.PHONY: release-check
release-check: ## Run deterministic source/package release gates and build artifacts
	poetry check --lock
	poetry run python scripts/check_release.py
	$(MAKE) format-check
	$(MAKE) sast
	$(MAKE) test
	QUERYGATE_DEMO_DB_URL=postgresql+asyncpg://user:pass@localhost/demo \
		poetry run querygate-validate-config
	poetry run python -m querygate.catalog_cli evaluate
	poetry run python -m querygate.catalog_cli adaptive-learning-test
	poetry build
	poetry run python scripts/check_release_artifacts.py
	$(MAKE) sbom

.PHONY: release-smoke
release-smoke: ## Build the image and execute a real structured query against Postgres
	bash scripts/release_smoke.sh

# ─── Docker Compose ───────────────────────────────────────────────────────────
.PHONY: compose-up
compose-up: ## Start the local demo Postgres and Redis services (detached and healthy)
	docker compose up -d --wait

.PHONY: compose-up-mssql
compose-up-mssql: ## Start the real SQL Server used by the mssql_live suite (profile-gated; amd64 emulation on Apple Silicon, allow ~1 min to boot)
	docker compose --profile mssql up -d --wait querygate-mssql

.PHONY: compose-up-mysql
compose-up-mysql: ## Start the real MySQL server used by the mysql_live suite (profile-gated; multi-arch, no emulation needed)
	docker compose --profile mysql up -d --wait querygate-mysql

.PHONY: compose-down
compose-down: ## Stop and remove the local demo infrastructure containers (including the mssql/mysql profiles)
	docker compose --profile mssql --profile mysql down

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
