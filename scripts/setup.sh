#!/usr/bin/env bash
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[setup]${NC} $*"; }
success() { echo -e "${GREEN}[setup]${NC} $*"; }
warn()    { echo -e "${YELLOW}[setup]${NC} $*"; }
die()     { echo -e "${RED}[setup]${NC} $*" >&2; exit 1; }

# ─── Guards ───────────────────────────────────────────────────────────────────
command -v poetry >/dev/null 2>&1 || die "poetry not found — install from https://python-poetry.org/docs/"

# ─── 1. Virtual environment ───────────────────────────────────────────────────
PYTHON_BIN=""
for candidate in python3.11 python3.12 python3.13; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN=$(command -v "$candidate")
        break
    fi
done
[ -n "$PYTHON_BIN" ] || die "No compatible Python (3.11-3.13) found. Install one and re-run."
info "Using Python interpreter: $PYTHON_BIN ($(${PYTHON_BIN} --version))"

info "Configuring Poetry to create .venv inside the project..."
poetry config virtualenvs.in-project true
poetry env use "$PYTHON_BIN"

info "Installing dependencies (including dev)..."
poetry install

VENV_PATH=$(poetry env info --path)
success "Virtual environment ready at: $VENV_PATH"

# ─── 2. .env file ─────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        warn ".env created from .env.example — review it before running the app."
    else
        warn "No .env.example found. Create a .env file manually before running the app."
    fi
else
    info ".env already exists, skipping."
fi

# ─── 3. Demo database (optional — QueryGate itself needs no database of its own) ──
if command -v docker >/dev/null 2>&1; then
    info "Starting the example demo Postgres database via Docker Compose..."
    docker compose up -d --wait
    success "Demo Postgres and Redis are healthy; Postgres was seeded automatically."
else
    warn "docker not found — skipping the Postgres/Redis demo infrastructure."
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
success "Setup complete. Run 'make run' to start QueryGate."
