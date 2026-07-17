FROM python:3.11-slim AS builder
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

FROM python:3.11-slim AS production
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# unixodbc + the Microsoft ODBC driver are only needed for MSSQL connections;
# skip this layer if you only connect to Postgres.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg unixodbc \
    && curl https://packages.microsoft.com/keys/microsoft.asc | tee /etc/apt/trusted.gpg.d/microsoft.asc \
    && curl https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
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
