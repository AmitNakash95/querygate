FROM python:3.11-slim-bookworm AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip poetry
WORKDIR /app
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache
COPY pyproject.toml poetry.lock ./
COPY src ./src
COPY examples ./examples
RUN poetry install --no-root --only main && rm -rf $POETRY_CACHE_DIR

FROM python:3.11-slim-bookworm AS production
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# unixodbc + the Microsoft ODBC driver are only needed for MSSQL connections;
# skip this layer if you only connect to Postgres.
#
# Base image is pinned to -bookworm rather than the floating python:3.11-slim
# tag: that tag moved to Debian 13 (trixie) and Microsoft's debian/13 apt
# repo is signed with a key (EE4D7792F748182B) their own microsoft.asc key
# file doesn't contain — a known upstream issue as of 2026
# (microsoft/linux-package-repositories#305, #253), not something fixable
# from here. bookworm + msodbcsql18 is a supported, working combination.
#
# The key must be dearmored into a binary keyring, not piped straight into
# trusted.gpg.d/*.asc — Debian's apt (via sqv) rejects an ASCII-armored key
# there. /usr/share/keyrings/microsoft-prod.gpg is the exact path
# Microsoft's own prod.list already references via signed-by=.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg unixodbc \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN addgroup --system querygate && adduser --system --ingroup querygate querygate
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY examples ./examples
RUN chown -R querygate:querygate /app
USER querygate

EXPOSE 8000
CMD ["python3", "-m", "querygate.run"]
